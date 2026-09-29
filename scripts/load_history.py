"""Idempotently load everything in data/ into the Hindsight bank.

    python scripts/load_history.py            # parse and report, write nothing
    python scripts/load_history.py --commit   # actually retain
    python scripts/load_history.py --commit --async

Every item carries a document_id, so re-running this after editing an
incident replaces that incident's memories instead of duplicating them.
That is what makes the load safe to repeat and the demo reproducible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import hindsight, ingest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Load incident data into Hindsight")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write. Without this flag the script only parses and reports.",
    )
    parser.add_argument(
        "--async",
        dest="use_async",
        action="store_true",
        help="Background the retain. Faster for bulk loads; errors surface later.",
    )
    args = parser.parse_args()

    # A commit builds the process-wide client through hindsight.get_client().
    # Nothing released it, so the aiohttp session and connector were still open
    # at interpreter exit and aiohttp printed "Unclosed client session" /
    # "Unclosed connector" after a successful load. finally covers both the
    # success path and the early `return 1` on a failed file, plus an
    # unexpected exception or Ctrl-C mid-load. close_client() is the existing
    # lifecycle and is a no-op when no client was ever built (dry run).
    try:
        return _load(args)
    finally:
        try:
            hindsight.close_client()
        except Exception as exc:  # noqa: BLE001
            # Cleanup must never mask the real exit code or the real error.
            print(f"[warn] client cleanup failed: {type(exc).__name__}: {exc}")


def _load(args: argparse.Namespace) -> int:
    sources = ingest.load_everything()
    if not sources:
        print("No markdown found under data/. Nothing to do.")
        print("Expected: data/incidents/*.md, data/postmortems/*.md, data/runbooks/*.md")
        return 0

    total_items = 0
    for kind, path, items in sources:
        ids = {item.get("document_id") for item in items}
        print(f"{kind:11s} {path.name:34s} {len(items):2d} items  document_id={sorted(ids)}")
        total_items += len(items)

    print(f"\n{total_items} retain items across {len(sources)} files.")
    if not args.commit:
        print("\nDry run. Nothing written. Re-run with --commit to retain.")
        return 0

    print("\nRetaining...")
    for kind, path, items in sources:
        try:
            result = hindsight.retain_items(items, retain_async=args.use_async)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {path.name}: {type(exc).__name__}: {exc}")
            return 1
        usage = getattr(result, "usage", None)
        print(f"  [ok] {path.name:34s} usage={usage}")

    print(
        "\nDone. Observation consolidation runs in the background after retain, "
        "so newly synthesized patterns may take a short time to appear."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
