"""Thin wrapper over the Hindsight Cloud Python SDK.

Design rules for this module:
  1. It is the ONLY place that talks to the Hindsight SDK. Everything else in
     the project calls these functions.
  2. It does not invent API surface. Every call below appears in current
     Hindsight Cloud documentation. `verify_sdk()` proves the installed client
     actually exposes them before we depend on it.
  3. No secret is ever returned, logged, or placed in an HTTP response.

Docs verified: https://docs.hindsight.vectorize.io/python-sdk
"""

from __future__ import annotations

import asyncio
import inspect
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from hindsight_client import Hindsight

from . import config

_client: Hindsight | None = None
_client_loop: asyncio.AbstractEventLoop | None = None

# Every SDK call runs on this one worker thread.
#
# The SDK resolves an event loop in the *calling* thread and reuses it, so the
# client's aiohttp session binds to the loop of whichever thread first used it.
# FastAPI serves these routes as sync `def` handlers on an AnyIO threadpool, so
# without this a request landing on a different thread drives the session from a
# foreign loop and aiohttp fails with "Timeout context manager should be used
# inside a task", surfacing as an intermittent 502. One worker means one loop,
# so the session is always used from the loop it is bound to.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hindsight-sdk")

T = TypeVar("T")

# Operations this project depends on. If any is missing, we fail at setup
# rather than mid-investigation.
REQUIRED_CLIENT_METHODS = (
    "create_bank",
    "retain",
    "retain_batch",
    "recall",
    "reflect",
    "list_memories",
    "close",
)


class HindsightError(RuntimeError):
    """Raised when a Hindsight call fails."""


# --------------------------------------------------------------------------
# Client lifecycle
# --------------------------------------------------------------------------


def _sdk_loop() -> asyncio.AbstractEventLoop | None:
    """The loop the SDK's synchronous wrappers will drive requests on.

    The SDK resolves the loop in the calling thread and reuses it, so the
    aiohttp session ends up bound to *that* loop - not to whichever loop the
    caller happens to be on. Resolving it the same way here is what lets the
    close target the right loop.
    """
    try:
        with warnings.catch_warnings():
            # Resolving the loop this way mirrors the SDK's own per-call
            # resolution, which carries the same deprecation on 3.13.
            warnings.simplefilter("ignore", DeprecationWarning)
            return asyncio.get_event_loop()
    except RuntimeError:
        return None


def get_client() -> Hindsight:
    """Return the process-wide client. Reuse it; do not construct per request."""
    global _client, _client_loop
    if _client is None:
        config.require_hindsight_config()
        _client = Hindsight(
            base_url=config.HINDSIGHT_BASE_URL,
            api_key=config.HINDSIGHT_API_KEY,
            timeout=120.0,
        )
    if _client_loop is None:
        _client_loop = _sdk_loop()
    return _client


def _sdk_call(fn: Callable[[], T]) -> T:
    """Run one SDK operation on the dedicated worker thread.

    Exceptions raised in the worker are re-raised here unchanged, so each
    caller's existing try/except still sees the original error type.
    """
    return _EXECUTOR.submit(fn).result()


def _close_now() -> None:
    """Close the session. Must run on the worker thread that owns its loop.

    Pinning the owning loop keeps the close correct even if this is ever
    reached from somewhere other than the worker.
    """
    global _client, _client_loop
    if _client is None:
        return
    client, owner = _client, _client_loop
    _client = None
    _client_loop = None
    if owner is not None and not owner.is_closed() and not owner.is_running():
        asyncio.set_event_loop(owner)
    client.close()


def close_client() -> None:
    """Close synchronously and stop the worker. Used by the CLI scripts."""
    if _client is not None:
        _sdk_call(_close_now)
    _EXECUTOR.shutdown(wait=True)


async def aclose_client() -> None:
    """Close from inside a running loop, e.g. the FastAPI shutdown hook.

    The SDK's sync close() finds a running loop and does
    ``loop.create_task(self._api_client.close())`` without awaiting it, which
    produced "Task was destroyed but it is pending!". Awaiting the worker's
    close avoids both that fire-and-forget and the cross-loop connector error,
    and it keeps uvicorn's loop free while the close runs.
    """
    if _client is not None:
        await asyncio.wrap_future(_EXECUTOR.submit(_close_now))
    _EXECUTOR.shutdown(wait=True)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def verify_sdk() -> dict[str, Any]:
    """Introspect the installed SDK.

    This exists because the Cloud docs and the developer API reference are not
    always in agreement about parameter names. Rather than guessing, we print
    what is actually installed. Returns a report; raises on a missing method.
    """
    report: dict[str, Any] = {
        "client_class": f"{Hindsight.__module__}.{Hindsight.__name__}",
        "methods": {},
        "missing": [],
    }

    for name in REQUIRED_CLIENT_METHODS:
        method = getattr(Hindsight, name, None)
        if method is None:
            report["missing"].append(name)
            continue
        try:
            sig = str(inspect.signature(method))
        except (TypeError, ValueError):
            sig = "<signature unavailable>"
        report["methods"][name] = sig

    if report["missing"]:
        raise HindsightError(
            "Installed hindsight-client is missing required methods: "
            f"{', '.join(report['missing'])}. "
            "Run: pip install --upgrade hindsight-client"
        )
    return report


# --------------------------------------------------------------------------
# Bank
# --------------------------------------------------------------------------


def ensure_bank() -> dict[str, Any]:
    """Create the memory bank if it does not exist.

    Per Hindsight docs, only writes create a bank and reads against a missing
    bank return 404. create_bank against an existing bank is therefore treated
    as success, and we say so explicitly rather than swallowing the error.
    """
    def _create() -> dict[str, Any]:
        bank = get_client().create_bank(
            bank_id=config.HINDSIGHT_BANK_ID,
            name="Production Incident Memory",
            background=(
                "Organizational memory of production incidents, their root causes, "
                "impact, failed and successful remediation attempts, and the runbooks "
                "used to resolve them."
            ),
            disposition={"skepticism": 4, "literalism": 4, "empathy": 1},
        )
        return {"bank_id": getattr(bank, "bank_id", config.HINDSIGHT_BANK_ID), "created": True}

    try:
        return _sdk_call(_create)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
        message = str(exc).lower()
        if "exist" in message or "409" in message or "conflict" in message:
            return {"bank_id": config.HINDSIGHT_BANK_ID, "created": False}
        raise HindsightError(f"create_bank failed: {exc}") from exc


def list_memories(limit: int = 1, offset: int = 0) -> Any:
    """List memories in the bank. Read-only; runs on the SDK worker thread."""
    return _sdk_call(
        lambda: get_client().list_memories(
            bank_id=config.HINDSIGHT_BANK_ID, limit=limit, offset=offset
        )
    )


def check_connection() -> dict[str, Any]:
    """Prove server reachability, auth, and bank existence. Writes nothing
    beyond the bank itself. This is the first test command in the build."""
    report: dict[str, Any] = {"base_url": config.HINDSIGHT_BASE_URL}
    report["sdk"] = verify_sdk()
    report["bank"] = ensure_bank()

    try:
        listing = list_memories(limit=1, offset=0)
        report["read_path"] = {
            "ok": True,
            "total": getattr(listing, "total", None),
        }
    except Exception as exc:  # noqa: BLE001
        raise HindsightError(f"list_memories failed: {exc}") from exc

    return report


# --------------------------------------------------------------------------
# Retain
# --------------------------------------------------------------------------


def _to_sdk_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the items with `entities` in the shape the SDK actually accepts.

    The installed client types entities as `list[dict[str, str]]` and does
    `EntityInput(text=e["text"], type=e.get("type"))` per entry, so a bare
    string raises "string indices must be integers, not 'str'". Ingest emits
    entity names, which is the readable form for the parser and for local
    inspection; this is the boundary where that becomes the SDK's wire form.

    Entries already given as dicts are passed through untouched, and the
    caller's items are copied rather than mutated.
    """
    prepared: list[dict[str, Any]] = []
    for item in items:
        entities = item.get("entities")
        if entities and any(isinstance(entry, str) for entry in entities):
            item = {
                **item,
                "entities": [
                    {"text": entry} if isinstance(entry, str) else entry
                    for entry in entities
                ],
            }
        prepared.append(item)
    return prepared


def retain_items(items: list[dict[str, Any]], retain_async: bool = False) -> dict[str, Any]:
    """Store memory items.

    `items` is a list of dicts using the documented retain item fields:
    content, context, timestamp, document_id, metadata, entities, tags.

    document_id makes the write idempotent: re-retaining the same document_id
    replaces the previous document and its memories rather than duplicating.
    That property is what makes the post-mortem learning loop safe to re-run.
    """
    if not items:
        raise HindsightError("retain_items called with an empty list.")

    prepared = _to_sdk_items(items)

    def _write() -> Any:
        client = get_client()
        if len(prepared) == 1:
            return client.retain(
                bank_id=config.HINDSIGHT_BANK_ID,
                retain_async=retain_async,
                **prepared[0],
            )
        return client.retain_batch(
            bank_id=config.HINDSIGHT_BANK_ID,
            items=prepared,
            retain_async=retain_async,
        )

    try:
        return _sdk_call(_write)
    except Exception as exc:  # noqa: BLE001
        raise HindsightError(f"retain failed: {exc}") from exc


# --------------------------------------------------------------------------
# Dry-run extraction
# --------------------------------------------------------------------------

# One dry-run is a real extraction LLM call, so an unbounded preview over a
# 12-section post-mortem would be 12 sequential LLM round trips. Callers that
# only need a quality signal get the first few sections by default.
DRY_RUN_TIMEOUT_S = 180
DRY_RUN_DEFAULT_SECTIONS = 3


def _dry_run_body(item: dict[str, Any]) -> dict[str, Any]:
    """Build the flat dry-run request for one retain item.

    Two details are load-bearing and were learned the hard way in
    scripts/smoke.py:

    - The endpoint takes ONE flat body, not a batch. There is no "items" key,
      and an unrecognised key fails validation because the required "content"
      is then missing (HTTP 422, loc ["body", "content"]).
    - `timestamp` must be omitted entirely when it is the sentinel "unset"
      (runbooks). It is not a datetime, so sending it as a string fails.

    The body is built from the SDK's own model so the shape is validated
    against the installed contract rather than hand-assembled.
    """
    from hindsight_client_api.models.dry_run_extract_request import DryRunExtractRequest

    fields: dict[str, Any] = {
        "content": item.get("content", ""),
        "context": item.get("context", ""),
    }
    stamp = item.get("timestamp")
    if stamp and stamp != "unset":
        fields["timestamp"] = stamp

    return DryRunExtractRequest(**fields).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def dry_run_extract(
    items: list[dict[str, Any]],
    max_sections: int = DRY_RUN_DEFAULT_SECTIONS,
) -> list[dict[str, Any]]:
    """Report the facts Hindsight WOULD extract from these items. Writes nothing.

    This is the quality gate for the learning loop. The parser's own docstring
    is blunt about why it matters: extraction is LLM-judgment, so how a section
    is *worded* decides what memory exists. A "Failed actions" section reading
    "restart the API servers" extracts as an action taken, not as a failure. A
    preview is how you find that out before the memory is written.

    Read-only by construction: the endpoint runs fact extraction only, with no
    entity resolution, links, embeddings or persistence, so this cannot write to
    the bank even if called with the real item dicts.
    """
    if not items:
        raise HindsightError("dry_run_extract called with an empty list.")

    selected = items if max_sections is None else items[:max_sections]
    url = (
        f"{config.HINDSIGHT_BASE_URL}/v1/default/banks/"
        f"{config.HINDSIGHT_BANK_ID}/memories/dry-run-extract"
    )

    def _post(payload: dict[str, Any]) -> dict[str, Any]:
        # urllib, not the SDK session, on purpose. It is stateless per call, so
        # it cannot inherit the loop affinity that forced the SDK onto a
        # dedicated worker thread. Submitting to the same worker anyway keeps
        # every network call for this process off the event loop.
        import json
        import urllib.request

        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {config.HINDSIGHT_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=DRY_RUN_TIMEOUT_S) as response:
            return json.loads(response.read().decode())

    from hindsight_client_api.models.dry_run_extraction_result import DryRunExtractionResult

    results: list[dict[str, Any]] = []
    for item in selected:
        try:
            raw = _sdk_call(lambda item=item: _post(_dry_run_body(item)))
            parsed = DryRunExtractionResult.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            raise HindsightError(f"dry-run-extract failed: {exc}") from exc

        facts = [
            {
                "fact_type": getattr(fact, "fact_type", None),
                "text": getattr(fact, "text", None),
                "entities": list(getattr(fact, "entities", None) or []),
            }
            for fact in (parsed.facts or [])
        ]
        usage = getattr(parsed, "usage", None)
        results.append(
            {
                "section": (item.get("metadata") or {}).get("section"),
                "source_file": (item.get("metadata") or {}).get("source_file"),
                "fact_count": len(facts),
                "facts": facts,
                "usage": {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
                if usage is not None
                else None,
            }
        )

    return results


# --------------------------------------------------------------------------
# Recall
# --------------------------------------------------------------------------


def recall(
    query: str,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    max_tokens: int | None = None,
    include_chunks: bool = False,
) -> Any:
    """Retrieve memories. No LLM call, so this is the cheap high-volume path."""
    kwargs: dict[str, Any] = {
        "bank_id": config.HINDSIGHT_BANK_ID,
        "query": query,
        "budget": config.HINDSIGHT_RECALL_BUDGET,
        "max_tokens": max_tokens or config.RECALL_MAX_TOKENS,
    }
    if types:
        kwargs["types"] = types
    if tags:
        kwargs["tags"] = tags
    if include_chunks:
        kwargs["include_chunks"] = True

    try:
        return _sdk_call(lambda: get_client().recall(**kwargs))
    except TypeError as exc:
        # A parameter the installed SDK does not accept. Report it precisely
        # instead of silently degrading or rewriting the call.
        raise HindsightError(
            f"recall rejected parameters: {exc}. Run "
            "'python scripts/smoke.py --stage introspect' to see the real "
            "signature of the installed client."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HindsightError(f"recall failed: {exc}") from exc


# --------------------------------------------------------------------------
# Reflect
# --------------------------------------------------------------------------


def reflect(
    query: str,
    response_schema: dict[str, Any] | None = None,
    budget: str | None = None,
    include_facts: bool = True,
) -> Any:
    """Reason over the bank and return a synthesized, cited answer.

    `include_facts` requests `based_on` — the facts reflect actually read —
    which the SDK otherwise omits. The reflect request models it as
    `include.facts`, and the client exposes it as `include_facts=True`
    (default False upstream), so without this the response carries no
    machine-readable provenance for the claims it makes.
    """
    kwargs: dict[str, Any] = {
        "bank_id": config.HINDSIGHT_BANK_ID,
        "query": query,
        "budget": budget or config.HINDSIGHT_REFLECT_BUDGET,
        "max_tokens": config.REFLECT_MAX_TOKENS,
        "include_facts": include_facts,
    }
    if response_schema:
        kwargs["response_schema"] = response_schema

    try:
        return _sdk_call(lambda: get_client().reflect(**kwargs))
    except TypeError as exc:
        raise HindsightError(
            f"reflect rejected parameters: {exc}. Run "
            "'python scripts/smoke.py --stage introspect' to see the real "
            "signature of the installed client."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HindsightError(f"reflect failed: {exc}") from exc
