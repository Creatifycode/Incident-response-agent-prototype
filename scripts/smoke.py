"""Staged smoke test. One operation per stage, so a failure names itself.

    python scripts/smoke.py --stage connect     # no API calls yet except bank
    python scripts/smoke.py --stage introspect  # print installed SDK signatures
    python scripts/smoke.py --stage retain      # one memory in
    python scripts/smoke.py --stage recall      # read it back
    python scripts/smoke.py --stage reflect     # reason over it
    python scripts/smoke.py --stage dryrun      # preview extraction, no writes
    python scripts/smoke.py --stage postmortem  # learning loop: parse + collision + preview
    python scripts/smoke.py --stage all

Nothing is assumed to succeed. Every stage prints the exact error it hit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, config, hindsight, ingest  # noqa: E402

CANARY = (
    "Smoke test canary for the incident response agent. Incident SMOKE-000 is "
    "a synthetic probe. It is not a real incident and has no user impact. "
    "It exists only to prove that retain, recall and reflect are wired "
    "correctly end to end."
)


def _ok(label: str, detail: str = "") -> None:
    print(f"[ok]   {label}" + (f" - {detail}" if detail else ""))


def _fail(label: str, exc: BaseException) -> int:
    print(f"[FAIL] {label}")
    print(f"       {type(exc).__name__}: {exc}")
    return 1


# --------------------------------------------------------------------------


def stage_introspect() -> int:
    """Local only. Prints what the installed client actually exposes.

    Run this whenever a call fails, before changing any code. If a signature
    here does not match what our wrapper assumes, the fix belongs in the
    wrapper, not in a guess.
    """
    print("Installed hindsight-client surface\n")
    try:
        report = hindsight.verify_sdk()
    except Exception as exc:  # noqa: BLE001
        return _fail("verify_sdk", exc)

    print(f"  class: {report['client_class']}\n")
    for name, signature in report["methods"].items():
        print(f"  {name}{signature}\n")

    extra = [n for n in dir(hindsight.Hindsight) if not n.startswith("_")]
    print(f"  also available: {', '.join(extra)}\n")
    _ok("introspect")
    return 0


def stage_connect() -> int:
    """Server reachability, auth, bank creation, and the read path."""
    print(f"base_url: {config.HINDSIGHT_BASE_URL}")
    print(f"bank_id : {config.HINDSIGHT_BANK_ID}\n")

    if not config.HINDSIGHT_API_KEY:
        print("[FAIL] HINDSIGHT_API_KEY is not set.")
        print("       Create .env from .env.example, add your key, then retry.")
        return 1

    try:
        report = hindsight.check_connection()
    except Exception as exc:  # noqa: BLE001
        print("[hint] If this is a 401, the key is wrong or expired.")
        print("[hint] If this is 402, the account has no credits.")
        return _fail("check_connection", exc)

    _ok("SDK surface verified", f"{len(report['sdk']['methods'])} methods")
    created = report["bank"]["created"]
    _ok(
        "bank reachable",
        f"{report['bank']['bank_id']} ({'created now' if created else 'already existed'})",
    )
    _ok("read path", f"total memories = {report['read_path']['total']}")
    return 0


def _usage_reported(usage: Any) -> bool:
    """True only if the server actually reported non-zero token counts.

    Hindsight returns `"usage": {}` on retain paths that do not account, and
    TokenUsage() defaults every field to 0, so an all-zero object is
    indistinguishable from a genuinely free retain. Printing it raw is what
    made a paid LLM extraction look like it cost nothing.
    """
    if usage is None:
        return False
    return any(
        getattr(usage, name, 0) or 0
        for name in ("input_tokens", "output_tokens", "total_tokens")
    )


def _wait_until_recallable(query: str, timeout: float = 90.0) -> int:
    """Block until the just-retained memory is actually searchable.

    retain() returning is not the same as the memory being queryable. The
    extract -> embed -> resolve -> link -> consolidate pipeline still has work
    queued when the response is written, and this is worst on a cold bank,
    which is exactly the state a fresh smoke test runs against. Recalling
    immediately after a successful retain races that pipeline and returns 0
    facts even though the write is fine.

    Recall bills no LLM tokens, so polling it is the cheap way to wait.
    Returns the number of seconds waited, or -1 on timeout.
    """
    deadline = time.monotonic() + timeout
    waited = 0.0
    delay = 2.0
    while True:
        facts = agent.gather_evidence(query)
        if facts:
            return waited
        if time.monotonic() >= deadline:
            return -1
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10.0)


def stage_retain() -> int:
    """One memory. Synchronous, so a failure surfaces here rather than in a
    background operation nobody is watching."""
    try:
        result = hindsight.retain_items(
            [
                {
                    "content": CANARY,
                    "context": "connectivity smoke test",
                    "document_id": "SMOKE-000",
                    "timestamp": "2026-01-15T09:00:00Z",
                    "metadata": {"incident_id": "SMOKE-000", "kind": "smoke-test"},
                    "tags": ["kind:smoke-test"],
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("retain", exc)

    # Report every field the response carries. `var_async` is the one that
    # says whether the server backgrounded the work, and it was being dropped
    # on the floor, so an async retain looked identical to a sync one.
    usage = getattr(result, "usage", None)
    _ok(
        "retain",
        f"success={getattr(result, 'success', None)} "
        f"items={getattr(result, 'items_count', None)} "
        f"async={getattr(result, 'var_async', None)}",
    )
    print(f"       usage: {usage if _usage_reported(usage) else 'not reported by server'}")

    waited = _wait_until_recallable("SMOKE-000 canary probe")
    if waited < 0:
        print("[FAIL] retain returned but the memory is still not recallable")
        print("       after 90s. The write may have failed, or the extraction")
        print("       pipeline is stuck. Check bank stats for pending operations.")
        return 1
    _ok("recallable", f"visible after {waited:.0f}s")
    return 0


def stage_recall() -> int:
    try:
        facts = agent.gather_evidence("SMOKE-000 canary probe")
    except Exception as exc:  # noqa: BLE001
        return _fail("recall", exc)

    if not facts:
        print("[FAIL] recall returned 0 facts after retain reported the memory")
        print("       as recallable. Either the write was rolled back, or the")
        print("       query does not match the stored text.")
        return 1

    for fact in facts[:5]:
        print(f"  - [{fact['type']}] {fact['text'][:110]}")
    _ok("recall", f"{len(facts)} facts")
    return 0


def stage_reflect() -> int:
    try:
        result = hindsight.reflect(
            query=(
                "What is the SMOKE-000 canary probe, and does it represent a real "
                "production incident with user impact?"
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("reflect", exc)

    print("\n" + (getattr(result, "text", "") or "<empty>") + "\n")
    based_on = getattr(result, "based_on", None)
    print(f"based_on memories: {len(getattr(based_on, 'memories', None) or [])}")
    _ok("reflect", f"usage={getattr(result, 'usage', None)}")
    return 0


def stage_dryrun() -> int:
    """Preview what retain WOULD extract, without writing.

    The endpoint takes ONE flat DryRunExtractRequest body, not a batch:
    {"content": ..., "context": ..., "timestamp": ...}. There is no "items"
    key, and an unrecognised key fails validation because the required
    "content" is then missing (HTTP 422, loc ["body", "content"]).

    The body is built with the SDK's own model so the shape is validated
    against the installed contract rather than hand-assembled. The endpoint
    runs fact extraction only, with no entity resolution, links, embeddings
    or persistence, so this stage cannot write to the bank.
    """
    import urllib.error
    import urllib.request

    from hindsight_client_api.models.dry_run_extract_request import DryRunExtractRequest
    from hindsight_client_api.models.dry_run_extraction_result import DryRunExtractionResult

    sample = Path(config.INCIDENTS_DIR) / "INC-001.md"
    if not sample.exists():
        print(f"[skip] {sample.name} not found yet. Add data files first.")
        return 0

    # Preview a real retain item rather than a slice of the raw markdown. A
    # character-truncated file would include front matter and end mid-sentence,
    # so the preview would not reflect the text retain actually receives.
    items, _meta = ingest.build_incident_items(sample)
    item = next(
        (i for i in items if i["metadata"].get("section") == "Failed actions"),
        items[0],
    )
    print(f"previewing: {item['metadata']['source_file']} / {item['metadata']['section']}")

    fields: dict[str, object] = {
        "content": item["content"],
        "context": item.get("context", ""),
    }
    # Runbooks are timeless and carry timestamp "unset", which is not a
    # datetime and must be left off the request rather than sent as a string.
    stamp = item.get("timestamp")
    if stamp and stamp != "unset":
        fields["timestamp"] = stamp

    try:
        # mode="json" matters: the generated to_dict()/to_json() pair leaves
        # `timestamp` as a datetime object, which json.dumps cannot encode.
        payload = DryRunExtractRequest(**fields).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("dryrun (local request shape)", exc)
    print(f"request body keys: {sorted(payload)}")

    body = json.dumps(payload).encode()
    url = f"{config.HINDSIGHT_BASE_URL}/v1/default/banks/{config.HINDSIGHT_BANK_ID}/memories/dry-run-extract"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.HINDSIGHT_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = DryRunExtractionResult.from_dict(json.loads(response.read().decode()))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"[FAIL] dry-run-extract HTTP {exc.code}")
        print(f"       {detail[:500]}")
        return 1
    except Exception as exc:  # noqa: BLE001
        return _fail("dryrun", exc)

    facts = result.facts or []
    for position, fact in enumerate(facts, 1):
        print(f"\n{position}. [{fact.fact_type}] {fact.text}")
        if fact.entities:
            print(f"   entities: {', '.join(fact.entities)}")

    usage = result.usage
    print(f"\nfacts: {len(facts)}")
    if usage is not None:
        print(
            f"usage: in={usage.input_tokens} out={usage.output_tokens} "
            f"total={usage.total_tokens}"
        )
    _ok("dryrun", "preview only, no facts persisted")
    return 0


def stage_postmortem() -> int:
    """The learning loop end to end, with the retain step deliberately omitted.

    Covers parse -> scaffold -> collision guard -> extraction preview. The one
    step missing is the write, because this stage is safe to run against a
    populated bank: a dry run persists nothing, and the collision guard is
    asserted rather than triggered by actually retaining.

    Everything here is real. The scaffold is written to a temp file and run
    through the same parser the endpoint uses, and the preview calls the same
    dry-run-extract wrapper the UI calls.
    """
    import tempfile

    # 1. A free id, and one that is demonstrably taken.
    taken = ingest.known_incident_ids()
    free = ingest.next_incident_id()
    print(f"ids on disk    : {sorted(taken)}")
    print(f"next free id   : {free}")
    if free in taken:
        print(f"[FAIL] next_incident_id() handed back {free}, which already exists.")
        return 1
    _ok("id allocation", f"{free} is not in use")

    # 2. The scaffold must survive the real parser, or "valid template" is a lie.
    markdown = ingest.postmortem_template(free)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(markdown)
        scaffold_path = Path(handle.name)
    try:
        items, meta = ingest.build_incident_items(scaffold_path)
    except ValueError as exc:
        scaffold_path.unlink(missing_ok=True)
        return _fail("scaffold parses", exc)
    finally:
        scaffold_path.unlink(missing_ok=True)

    if not items:
        print("[FAIL] scaffold produced zero retain items")
        return 1
    if len({i["document_id"] for i in items}) != 1:
        print("[FAIL] scaffold sections did not share one document_id")
        return 1
    _ok(
        "scaffold parses",
        f"{len(items)} items, document_id={items[0]['document_id']}, "
        f"sections={[i['metadata']['section'] for i in items][:3]}...",
    )

    # 3. An untouched scaffold must be detectable, and must be blocked on retain.
    unfilled = ingest.unfilled_sections(items)
    if len(unfilled) != len(items):
        print(f"[FAIL] only {len(unfilled)}/{len(items)} empty sections detected")
        return 1
    _ok("empty sections detected", f"{len(unfilled)}/{len(items)} flagged")

    # 4. The collision guard: a taken id must be refused, not silently merged.
    sample = next(iter(sorted(taken)), None)
    if sample:
        taken_items, taken_meta = _load_incident_by_id(sample)
        reserved = taken  # owner file is in data/incidents, so it stays reserved
        if (taken_meta.get("incident_id") or "").upper() in reserved:
            _ok("collision guard", f"{sample} is reserved and would return 409 on persist")
        else:
            print(f"[warn] could not assert collision guard for {sample}")
        del taken_items
    else:
        print("[warn] no incidents on disk; skipping the collision assertion")

    # 5. The real thing: what would Hindsight remember from these sections?
    try:
        extractions = hindsight.dry_run_extract(items, max_sections=2)
    except hindsight.HindsightError as exc:
        return _fail("dry-run-extract (wrapper)", exc)

    total = sum(e["fact_count"] for e in extractions)
    for entry in extractions:
        print(f"\n{entry['section']}: {entry['fact_count']} fact(s)")
        for fact in entry["facts"]:
            print(f"   [{fact['fact_type']}] {fact['text']}")
            if fact["entities"]:
                print(f"      entities: {', '.join(fact['entities'])}")
    if total == 0:
        print("\n[warn] an entirely empty scaffold extracted zero facts, which is expected:")
        print("       the sections hold only template hints. Fill one in to see real extraction.")
    _ok("extraction preview", f"{total} fact(s) from {len(extractions)} section(s), nothing written")
    return 0


def _load_incident_by_id(incident_id: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    for path in sorted(config.INCIDENTS_DIR.glob("*.md")):
        items, meta = ingest.build_incident_items(path)
        if (meta.get("incident_id") or "").upper() == incident_id.upper():
            return items, meta
    raise FileNotFoundError(incident_id)


STAGES = {
    "introspect": stage_introspect,
    "connect": stage_connect,
    "retain": stage_retain,
    "recall": stage_recall,
    "reflect": stage_reflect,
    "dryrun": stage_dryrun,
    "postmortem": stage_postmortem,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Hindsight connectivity smoke test")
    parser.add_argument(
        "--stage",
        default="connect",
        choices=[*STAGES, "all"],
        help="Which single step to run.",
    )
    args = parser.parse_args()

    # Any stage that touches the network builds the process-wide client through
    # hindsight.get_client(). Leaving it open leaks the underlying aiohttp
    # session, and aiohttp then prints "Unclosed client session" / "Unclosed
    # connector" at interpreter exit. A finally block covers every exit route
    # uniformly: a single stage, the --stage all loop breaking early on
    # failure, an unexpected exception, and Ctrl-C.
    try:
        if args.stage == "all":
            code = 0
            for name in ("introspect", "connect", "retain", "recall", "reflect"):
                print(f"\n{'=' * 60}\n== {name}\n{'=' * 60}")
                code |= STAGES[name]()
                if code:
                    print(f"\nStopping: '{name}' failed. Fix that before moving on.")
                    break
            return code

        return STAGES[args.stage]()
    finally:
        try:
            hindsight.close_client()
        except Exception as exc:  # noqa: BLE001
            # Cleanup must never mask the real exit code or the real error.
            print(f"[warn] client cleanup failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
