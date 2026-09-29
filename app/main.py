"""FastAPI application.

Five endpoints and a static page. The browser never sees an API key: it only
ever talks to this local backend, and this backend is the only thing that
holds HINDSIGHT_API_KEY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import agent, config, hindsight, ingest

app = FastAPI(title="Incident Response Agent", version="0.1.0")


class InvestigateRequest(BaseModel):
    description: str = Field(
        ...,
        min_length=10,
        description="What is happening in production right now.",
    )


class IngestRequest(BaseModel):
    """Post-mortem learning. Accepts markdown directly, or a filename already
    present in data/postmortems/."""

    markdown: str | None = None
    filename: str | None = None
    persist: bool = Field(
        default=True,
        description="False previews the parsed retain items without writing.",
    )


class PreviewExtractRequest(BaseModel):
    """Same source options as ingest, but this only previews extraction."""

    markdown: str | None = None
    filename: str | None = None
    max_sections: int = Field(
        default=hindsight.DRY_RUN_DEFAULT_SECTIONS,
        ge=1,
        le=len(ingest.POSTMORTEM_SECTIONS),
        description="Extraction is one LLM call per section, so this bounds the cost.",
    )


@app.on_event("startup")
def _startup() -> None:
    """Fail fast on a missing key rather than at the first request."""
    config.require_hindsight_config()


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Await the SDK's async close; the sync path cannot finish under uvicorn."""
    await hindsight.aclose_client()


@app.get("/")
def index() -> FileResponse:
    path: Path = config.FRONTEND_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="frontend/index.html missing")
    return FileResponse(path)


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Config check plus a live round trip. Does not write."""
    try:
        report = hindsight.check_connection()
    except hindsight.HindsightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "config": config.public_config(), "hindsight": report}


@app.post("/api/investigate")
def investigate(request: InvestigateRequest) -> dict[str, Any]:
    """Recall organizational experience, then reason over it. Advisory only:
    this endpoint never executes anything against production."""
    try:
        return agent.investigate(request.description)
    except hindsight.HindsightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Learning loop
# --------------------------------------------------------------------------

# Front matter is optional in the parser (a post-mortem without it still
# parses), but these are what make a memory attributable and retrievable, so a
# missing one is worth saying out loud before anything is written.
_EXPECTED_META = ("incident_id", "title", "date", "service", "environment", "severity")


def _build_items(
    markdown: str | None, filename: str | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Turn a request into retain items.

    Shared by ingest and extraction-preview so the two can never disagree
    about what the markdown parses to. Pasted markdown goes through a temp
    file because the parser reads a path, and the file is always unlinked.
    """
    if markdown:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(markdown)
            source = Path(handle.name)
    elif filename:
        source = config.POSTMORTEMS_DIR / Path(filename).name
        if not source.exists():
            raise HTTPException(
                status_code=404, detail=f"{filename} not found in data/postmortems/"
            )
    else:
        raise HTTPException(status_code=400, detail="supply markdown or filename")

    try:
        return ingest.build_incident_items(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if markdown:
            source.unlink(missing_ok=True)


def _reserved_ids(filename: str | None) -> set[str]:
    """Incident ids already taken, excluding the file being edited.

    Re-ingesting your own post-mortem to fix a typo is the documented
    upsert path, so an engineer editing data/postmortems/INC-007.md keeps
    INS-007. Pasting markdown that claims someone else's id does not: there
    is no owning file, so every existing id is reserved and the retain is
    refused. That asymmetry is the whole point of the guard.
    """
    reserved = ingest.known_incident_ids()
    if filename:
        owner = config.POSTMORTEMS_DIR / Path(filename).name
        if owner.exists():
            reserved.discard(owner.stem.upper())
    return reserved


def _item_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": item.get("document_id"),
        "context": item.get("context"),
        "timestamp": item.get("timestamp"),
        "tags": item.get("tags"),
        "metadata": item.get("metadata"),
        "content_preview": item.get("content", "")[:280],
    }


def _parse_warnings(
    items: list[dict[str, Any]], meta: dict[str, str], reserved: set[str]
) -> list[str]:
    """Everything worth telling the engineer before they write to the bank."""
    warnings: list[str] = []

    incident_id = (meta.get("incident_id") or "").strip().upper()
    if incident_id and incident_id in reserved:
        warnings.append(
            f"{incident_id} already exists. Retaining it REPLACES that document and all "
            "of its memories rather than adding to them."
        )

    missing = [key for key in _EXPECTED_META if not (meta.get(key) or "").strip()]
    if missing:
        warnings.append(
            "Front matter is missing " + ", ".join(missing) + ". Unattributed memories are "
            "much harder to recall correctly later."
        )

    unfilled = ingest.unfilled_sections(items)
    if unfilled:
        warnings.append(
            f"{len(unfilled)} section(s) still hold only the template hint: "
            + ", ".join(unfilled[:4])
            + ("..." if len(unfilled) > 4 else "")
        )

    return warnings


@app.get("/api/postmortem/scaffold")
def postmortem_scaffold() -> dict[str, Any]:
    """A valid, fully-parseable post-mortem skeleton with a free incident id.

    Auto-incrementing is the primary defence against clobbering memory: a
    post-mortem that is never told to reuse an existing id cannot replace one.
    """
    incident_id = ingest.next_incident_id()
    return {
        "incident_id": incident_id,
        "markdown": ingest.postmortem_template(incident_id),
        "sections": [title for title, _hint in ingest.POSTMORTEM_SECTIONS],
        "reserved_ids": sorted(ingest.known_incident_ids()),
    }


@app.post("/api/preview-extract")
def preview_extract(request: PreviewExtractRequest) -> dict[str, Any]:
    """What Hindsight WOULD remember from this post-mortem. Writes nothing.

    This is the quality gate the parser's docstring keeps pointing at:
    extraction is LLM-judgment, so how a section is worded decides what memory
    exists. Preview before you retain, or the phrasing is already permanent.
    """
    items, meta = _build_items(request.markdown, request.filename)
    reserved = _reserved_ids(request.filename)
    warnings = _parse_warnings(items, meta, reserved)

    try:
        extractions = hindsight.dry_run_extract(items, max_sections=request.max_sections)
    except hindsight.HindsightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    incident_id = (meta.get("incident_id") or "").strip().upper()
    return {
        "incident_id": incident_id,
        "items_count": len(items),
        "previewed_sections": len(extractions),
        "skipped_sections": len(items) - len(extractions),
        "unfilled_sections": ingest.unfilled_sections(items),
        "would_collide": bool(incident_id and incident_id in reserved),
        "warnings": warnings,
        "extractions": extractions,
    }


@app.post("/api/ingest")
def ingest_postmortem(request: IngestRequest) -> dict[str, Any]:
    """The learning loop: turn a resolved incident into organizational memory."""
    items, meta = _build_items(request.markdown, request.filename)
    reserved = _reserved_ids(request.filename)
    warnings = _parse_warnings(items, meta, reserved)

    incident_id = (meta.get("incident_id") or "").strip().upper()
    unfilled = ingest.unfilled_sections(items)
    preview = [_item_preview(item) for item in items]

    if not request.persist:
        return {
            "persisted": False,
            "incident_id": incident_id,
            "items_count": len(items),
            "sections": [(item.get("metadata") or {}).get("section") for item in items],
            "unfilled_sections": unfilled,
            "warnings": warnings,
            "items": preview,
        }

    # Hard block: this id belongs to another document, and retain replaces
    # rather than merges. Refusing beats silently deleting an incident's
    # memory, which is what the investigation flow reads from.
    if incident_id and incident_id in reserved:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{incident_id} already exists. Retaining it would delete that incident's "
                "existing memories. Use the next free id, or edit that incident's own file "
                "and re-ingest it by filename."
            ),
        )

    # Only block the case nobody could have meant. A partly-filled post-mortem
    # is a real draft worth keeping.
    if items and len(unfilled) == len(items):
        raise HTTPException(
            status_code=400,
            detail="Every section is still an empty template. Fill in at least one "
            "section, or preview the extraction first.",
        )

    try:
        result = hindsight.retain_items(items)
    except hindsight.HindsightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "persisted": True,
        "incident_id": incident_id,
        "items_count": len(items),
        "warnings": warnings,
        "usage": getattr(result, "usage", None),
        "items": preview,
    }


@app.get("/api/memory/summary")
def memory_summary() -> dict[str, Any]:
    """What the agent currently remembers. Useful for the demo: this number
    should grow after the learning loop runs."""
    try:
        listing = hindsight.list_memories(limit=1, offset=0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"bank_id": config.HINDSIGHT_BANK_ID, "total_memories": getattr(listing, "total", None)}
