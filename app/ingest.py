"""Markdown -> Hindsight retain items.

The representation question ("rigid incident schema vs natural language") is
answered here, and it is a hybrid with a specific rationale:

  - A small YAML-style front matter block carries IDENTITY ONLY
    (incident_id, date, service, environment, severity). These become
    `metadata`, `tags`, `timestamp` and `document_id`, which are exact and
    machine-readable.

  - The incident body stays NATURAL-LANGUAGE PROSE, split on `##` headings.
    Each section becomes one retain item. Hindsight's extraction LLM reads prose
    and pulls out facts; it does not read JSON. Forcing the content into a rigid
    serialized record would degrade extraction, not improve it.

Three properties of this design matter more than the field list:

1. ATTRIBUTION. Every item's content opens with the incident identity, so the
   facts Hindsight extracts carry "INC-001" in the text itself. That is what
   lets a future reflect say "restarting the API was attempted during INC-001
   and did not help" with the identifier attached.

2. ATOMIC IDEMPOTENCY. Every section of one incident shares
   `document_id = incident_id`. Per Hindsight docs, retaining a document_id
   that already exists deletes the old document and all its memories first.
   Re-ingesting an edited incident therefore replaces it atomically instead of
   accumulating duplicates. (Omit document_id and you get a random UUID per
   request, i.e. duplicates every time.)

3. PHRASING IS A MEMORY DECISION. Extraction is LLM-judgment, so how a section
   is worded determines what memory exists. A section headed "Failed actions"
   containing "restart the API servers" extracts as an action taken, not as a
   failure. Our data files must phrase non-events as explicit non-events. Use
   `python scripts/smoke.py --stage dryrun` to check extraction before
   committing data to the bank.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config

_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)

# Front matter keys we understand. Unknown keys are preserved in metadata so
# nothing is silently dropped.
_KNOWN_KEYS = frozenset(
    {"incident_id", "title", "date", "service", "environment", "severity", "kind", "entities"}
)


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Return (front matter dict, remaining markdown)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip().strip('"').strip("'")

    return fields, text[match.end() :]


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split on `##` headings. Content before the first heading is dropped
    because front matter is the only preamble we accept."""
    matches = list(_HEADING_RE.finditer(markdown))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if body:
            sections.append((match.group("title").strip(), body))
    return sections


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _incident_prefix(meta: dict[str, str], section: str) -> str:
    """The identity line that opens every incident item.

    This is what makes source attribution survive fact extraction.
    """
    parts = [
        f"Production incident {meta.get('incident_id', 'UNKNOWN')}",
        f"occurred {meta.get('date', 'an unknown date')}",
    ]
    if meta.get("environment"):
        parts.append(f"environment {meta['environment']}")
    if meta.get("severity"):
        parts.append(f"severity {meta['severity']}")
    if meta.get("service"):
        parts.append(f"primary service {meta['service']}")
    parts.append(f"section: {section}")
    return ". ".join(parts) + ".\n\n"


def build_incident_items(path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Turn one incident markdown file into a list of retain items."""
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(text)
    sections = _split_sections(body)
    if not sections:
        raise ValueError(f"{path.name}: no '##' sections found.")

    incident_id = meta.get("incident_id") or path.stem.upper()
    meta.setdefault("incident_id", incident_id)

    base_metadata = {
        "incident_id": incident_id,
        "title": meta.get("title", ""),
        "service": meta.get("service", ""),
        "environment": meta.get("environment", ""),
        "severity": meta.get("severity", ""),
        "kind": "incident",
        "source_file": path.name,
    }

    tags = ["kind:incident"]
    if meta.get("environment"):
        tags.append(f"env:{meta['environment'].lower()}")
    if meta.get("severity"):
        tags.append(f"sev:{meta['severity'].lower()}")

    # Supplying entities guarantees graph links exist. The graph retrieval arm
    # is what connects an incident to a service and to a runbook later.
    # Order is preserved and duplicates are dropped, since the service is often
    # also listed explicitly in `entities`.
    ordered: list[str] = [incident_id]
    for candidate in [meta.get("service"), *meta.get("entities", "").split(",")]:
        name = (candidate or "").strip()
        if name and name not in ordered:
            ordered.append(name)
    entities = ordered

    timestamp = meta.get("date") or None

    items: list[dict[str, Any]] = []
    for section, section_body in sections:
        items.append(
            {
                "content": _incident_prefix(meta, section) + section_body,
                "context": (
                    f"production incident {incident_id} "
                    f"({meta.get('title', 'untitled')})"
                ),
                # Shared across every section: the whole incident is one
                # upsertable document.
                "document_id": incident_id,
                "timestamp": timestamp,
                "metadata": {**base_metadata, "section": section},
                "tags": tags,
                "entities": entities,
            }
        )

    return items, meta


def build_runbook_items(path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Turn one runbook markdown file into a list of retain items.

    Runbooks are timeless reference material, so per the Hindsight retain docs
    we pass timestamp="unset" rather than letting them be dated at ingestion
    time. Dating a runbook "now" would make temporal retrieval think it was
    written during the current incident.
    """
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(text)
    sections = _split_sections(body)
    if not sections:
        raise ValueError(f"{path.name}: no '##' sections found.")

    runbook_id = meta.get("incident_id") or _slug(path.stem)
    title = meta.get("title", path.stem.replace("-", " ").title())

    tags = ["kind:runbook"]
    if meta.get("service"):
        tags.append(f"service:{meta['service'].lower()}")
    if meta.get("environment"):
        tags.append(f"env:{meta['environment'].lower()}")

    entities = [title, runbook_id]
    for extra in meta.get("entities", "").split(","):
        if extra.strip():
            entities.append(extra.strip())

    items: list[dict[str, Any]] = []
    for section, section_body in sections:
        items.append(
            {
                "content": (
                    f"Operational runbook {title} ({runbook_id}). "
                    f"Section: {section}.\n\n{section_body}"
                ),
                "context": f"operational runbook {title}",
                "document_id": f"runbook-{runbook_id}",
                "timestamp": "unset",
                "metadata": {
                    "runbook_id": runbook_id,
                    "title": title,
                    "kind": "runbook",
                    "service": meta.get("service", ""),
                    "section": section,
                    "source_file": path.name,
                },
                "tags": tags,
                "entities": entities,
            }
        )

    return items, meta


def _collect(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.md") if p.is_file())


# --------------------------------------------------------------------------
# Post-mortem scaffolding
# --------------------------------------------------------------------------
#
# Retaining an incident_id that already exists REPLACES that document and all
# of its memories (see the atomic idempotency note above). An accidental
# collision therefore does not duplicate, it destroys. These helpers exist so
# the UI never hands an engineer a default that can clobber seeded history.
#
# The guard is filesystem-based. It is free, deterministic, and covers the
# realistic case (reusing INC-001..INC-006, which ship as files). It cannot see
# an incident retained from some other tool, so the auto-incremented default
# and the explicit confirmation in the UI remain load-bearing.

POSTMORTEM_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "What broke, when it started, when it ended, and how it was resolved."),
    ("Trigger and recent changes", "The deploy or change that preceded it, with versions and times."),
    ("Symptoms", "What was observed, by whom, and how it was detected."),
    ("Root cause", "The confirmed mechanism, phrased as an established fact."),
    ("Impact and blast radius", "Who and what was affected, for how long, in what numbers."),
    ("Timeline", "Key events in UTC, one per line, prefixed with the time."),
    ("Attempted actions", "What was tried while the incident was open."),
    (
        "Failed actions",
        "What was tried and did NOT work. State non-events explicitly: "
        "'Restarting the API servers did not restore service.' Never list a "
        "failed action as a bare instruction, or it extracts as an action taken.",
    ),
    ("Successful actions", "What resolved it. These are the memory worth reusing."),
    ("Relevant runbook", "Which runbook applied, or that none existed."),
    ("Lessons learned", "What was learned, stated so it generalises beyond this incident."),
    ("Related incidents", "Other incident ids sharing a cause or a signal."),
    ("Prevention and follow-up", "Concrete follow-up actions and their owners."),
)

_COMMENT_ONLY_RE = re.compile(r"\A(?:\s*<!--.*?-->\s*)+\Z", re.DOTALL)


def known_incident_ids() -> set[str]:
    """Every incident id already on disk, uppercased.

    Both the filename stem and the front-matter id are collected, so a file
    named `2026-02-11-outage.md` declaring `incident_id: INC-004` still
    reserves INC-004.
    """
    ids: set[str] = set()
    for directory in (config.INCIDENTS_DIR, config.POSTMORTEMS_DIR):
        for path in _collect(directory):
            ids.add(path.stem.upper())
            meta, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
            if meta.get("incident_id"):
                ids.add(meta["incident_id"].strip().upper())
    return ids


def next_incident_id(existing: set[str] | None = None) -> str:
    """The lowest unused `INC-NNN`.

    Lowest-free rather than highest+1, so filling a gap in the sequence does
    not require renumbering anything that already exists.
    """
    taken = known_incident_ids() if existing is None else existing
    number = 1
    while f"INC-{number:03d}" in taken:
        number += 1
    return f"INC-{number:03d}"


def postmortem_template(incident_id: str) -> str:
    """A valid, fully-parseable post-mortem skeleton.

    Hints live in HTML comments so the markdown renders as clean headings. A
    comment-only section still parses, so an unfilled section is detectable
    (see `unfilled_sections`) rather than silently retaining the hint.
    """
    import datetime

    lines = [
        "---",
        f"incident_id: {incident_id}",
        "title: TODO one line describing the incident",
        f"date: {datetime.date.today().isoformat()}T00:00:00Z",
        "service: TODO primary service, e.g. payments-api",
        "environment: production",
        "severity: TODO e.g. SEV-2",
        "entities: TODO comma separated, including the service",
        "---",
        "",
    ]
    for title, hint in POSTMORTEM_SECTIONS:
        lines += [f"## {title}", "", f"<!-- {hint} -->", ""]
    return "\n".join(lines)


def unfilled_sections(items: list[dict[str, Any]]) -> list[str]:
    """Sections whose body is still nothing but the template hint.

    These would otherwise be retained as memory containing our own prompt
    text, which is the worst kind of noise: it looks like a real lesson.
    """
    out: list[str] = []
    for item in items:
        body = (item.get("content") or "").split("\n\n", 1)[-1]
        if body.strip() and _COMMENT_ONLY_RE.match(body):
            out.append(str((item.get("metadata") or {}).get("section", "?")))
    return out


def load_incidents() -> list[tuple[Path, list[dict[str, Any]]]]:
    """Parse every incident file. Pure filesystem work, no network."""
    results = []
    for path in _collect(config.INCIDENTS_DIR):
        items, _ = build_incident_items(path)
        results.append((path, items))
    return results


def load_postmortems() -> list[tuple[Path, list[dict[str, Any]]]]:
    """Parse post-mortems with the same shape as incidents.

    A post-mortem is an incident record that also carries the post-resolution
    knowledge, so it is ingested through the identical path. That is what makes
    the learning loop a single code path rather than a special case.
    """
    results = []
    for path in _collect(config.POSTMORTEMS_DIR):
        items, _ = build_incident_items(path)
        results.append((path, items))
    return results


def load_runbooks() -> list[tuple[Path, list[dict[str, Any]]]]:
    results = []
    for path in _collect(config.RUNBOOKS_DIR):
        items, _ = build_runbook_items(path)
        results.append((path, items))
    return results


def load_everything() -> list[tuple[str, Path, list[dict[str, Any]]]]:
    """All sources, tagged by kind. Used by scripts/load_history.py."""
    out: list[tuple[str, Path, list[dict[str, Any]]]] = []
    for path, items in load_runbooks():
        out.append(("runbook", path, items))
    for path, items in load_incidents():
        out.append(("incident", path, items))
    for path, items in load_postmortems():
        out.append(("postmortem", path, items))
    return out
