"""The investigation flow.

Three Hindsight calls, in this order, for one incident:

  1. recall  (world + experience)  -> the historical evidence record
  2. recall  (tagged kind:runbook) -> the applicable runbook
  3. reflect (with response_schema)-> the reasoning, cited and structured

Why both recall and reflect rather than reflect alone:

  reflect reasons well but returns prose. recall returns ranked facts with
  IDs, types, entities, dates and metadata, at no LLM cost. The grounding
  panel and the "Historical Matches" grouping need that structure; the
  recommendations need reflect's synthesis. Calling both is cheaper than
  parsing prose back into structure, and it gives us a visible evidence
  trail even when reflect produces something we do not trust.

The response_schema is what produces the UI panels as typed JSON without a
second LLM call. Hindsight returns both `text` (readable) and
`structured_output` (typed) from one reflect.
"""

from __future__ import annotations

from typing import Any

from . import hindsight

# Panels returned by reflect. Keys match the UI sections in frontend/index.html.
# Hindsight rejects a schema with an empty `properties` map, so every panel
# here is real and required.
INCIDENT_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "current_understanding": {
            "type": "string",
            "description": "What the engineer reported, restated. No speculation.",
        },
        "historical_matches": {
            "type": "array",
            "description": "Past incidents resembling the current one.",
            "items": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "relevance": {
                        "type": "string",
                        "enum": ["high", "moderate", "low"],
                    },
                    "signals_in_common": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "historical_root_cause": {"type": "string"},
                    "historical_impact": {"type": "array", "items": {"type": "string"}},
                    "successful_actions": {"type": "array", "items": {"type": "string"}},
                    "failed_actions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "incident_id",
                    "relevance",
                    "signals_in_common",
                    "historical_root_cause",
                ],
            },
        },
        "what_worked": {
            "type": "array",
            "description": "Actions that historically resolved an incident, "
            "each attributed to its incident ID.",
            "items": {"type": "string"},
        },
        "what_failed": {
            "type": "array",
            "description": "Actions historically attempted that did NOT resolve "
            "the incident, each attributed to its incident ID.",
            "items": {"type": "string"},
        },
        "relevant_runbook": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "why_relevant": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "why_relevant"],
        },
        "investigation_suggestions": {
            "type": "array",
            "description": "Next steps to investigate, most informative first.",
            "items": {"type": "string"},
        },
        "evidence_and_reasoning": {
            "type": "string",
            "description": "Why the historical incidents are considered relevant, "
            "naming the specific signals that overlap.",
        },
        "confidence_and_uncertainty": {
            "type": "string",
            "description": "How strong the historical match is, and what "
            "evidence is missing that would change the conclusion.",
        },
    },
    "required": [
        "current_understanding",
        "historical_matches",
        "what_worked",
        "what_failed",
        "investigation_suggestions",
        "evidence_and_reasoning",
        "confidence_and_uncertainty",
    ],
}

# The query. Written as a single question carrying both the engineer's report
# and the epistemic rules, because reflect's reasoning quality depends on the
# question. The same rules are additionally installed as bank directives,
# which Hindsight enforces on every reflect regardless of query wording.
_INVESTIGATE_PROMPT = """\
An on-call engineer is investigating a production incident right now.

CURRENT INCIDENT REPORT (unverified, from the engineer):
\"\"\"
{description}
\"\"\"

Using only what this organization has actually experienced before, answer:

1. Which past incidents resemble this one, and on what specific signals?
2. What were the historical root causes, and how did those incidents actually
   get resolved?
3. Which remediation actions were tried and FAILED historically? Name the
   incident each came from. These matter as much as the successful ones,
   because repeating a known dead end costs recovery time.
4. What is the impact profile of the most similar past incidents?
5. Which runbook applies, and why does the history say it applies?
6. What should the engineer investigate next, given the historical evidence?

RULES OF EVIDENCE - these are not stylistic preferences:
- A historical similarity is NOT proof of the current root cause. Use
  "resembles", "historically associated with", "evidence suggests",
  "worth investigating". Never state that a past root cause IS the current one.
- Label every claim as one of: CURRENT OBSERVATION, HISTORICAL FACT,
  AGENT INFERENCE, or RECOMMENDATION.
- Every historical claim must name the incident it came from.
- Never recommend executing a production-changing action as though it were
  approved. Rollbacks, restarts, config changes and database commands are
  proposals that require explicit human approval.
- If the stored history does not cover this situation, say so plainly and name
  the evidence you would need. Do not fill the gap with generic advice.
"""


def _fact_to_dict(fact: Any) -> dict[str, Any]:
    """Normalize a recalled fact. Uses getattr throughout because the exact
    result object shape is verified at runtime by smoke.py, not assumed."""
    scores = getattr(fact, "scores", None)
    metadata = getattr(fact, "metadata", None) or {}
    return {
        "id": getattr(fact, "id", None),
        "text": getattr(fact, "text", ""),
        "type": getattr(fact, "type", None),
        "context": getattr(fact, "context", None),
        "incident_id": metadata.get("incident_id"),
        "service": metadata.get("service") or None,
        "severity": metadata.get("severity") or None,
        "section": metadata.get("section"),
        "occurred_start": getattr(fact, "occurred_start", None),
        "occurred_end": getattr(fact, "occurred_end", None),
        "entities": getattr(fact, "entities", None) or [],
        "score": getattr(scores, "final", None) if scores is not None else None,
    }


def gather_evidence(description: str) -> list[dict[str, Any]]:
    """Step 1: the historical record. Grounded, cheap, no LLM."""
    result = hindsight.recall(
        query=description,
        types=["world", "experience"],
        include_chunks=True,
    )
    facts = getattr(result, "results", None) or []
    return [_fact_to_dict(fact) for fact in facts]


def gather_runbooks(description: str) -> list[dict[str, Any]]:
    """Step 2: runbooks, filtered server-side by tag so operational procedure
    does not compete with incident evidence in the same result list."""
    result = hindsight.recall(
        query=description,
        tags=["kind:runbook"],
    )
    facts = getattr(result, "results", None) or []
    return [_fact_to_dict(fact) for fact in facts]


def group_by_incident(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a flat fact list into per-incident groups for the UI."""
    grouped: dict[str, dict[str, Any]] = {}
    for fact in facts:
        key = fact.get("incident_id") or "unattributed"
        entry = grouped.setdefault(
            key,
            {
                "incident_id": key,
                "service": fact.get("service"),
                "severity": fact.get("severity"),
                "fact_count": 0,
                "facts": [],
            },
        )
        entry["fact_count"] += 1
        entry["facts"].append(
            {
                "section": fact.get("section"),
                "text": fact.get("text"),
                "type": fact.get("type"),
                "score": fact.get("score"),
            }
        )
    return sorted(grouped.values(), key=lambda g: -g["fact_count"])


def investigate(description: str) -> dict[str, Any]:
    """Full investigation for one reported incident."""
    description = (description or "").strip()
    if not description:
        raise ValueError("description is required")

    evidence = gather_evidence(description)
    runbook_facts = gather_runbooks(description)

    response = hindsight.reflect(
        query=_INVESTIGATE_PROMPT.format(description=description),
        response_schema=INCIDENT_REPORT_SCHEMA,
    )

    based_on = getattr(response, "based_on", None) or {}
    cited = []
    for memory in getattr(based_on, "memories", None) or []:
        # ReflectFact exposes id, text, type, context, occurred_start and
        # occurred_end. It carries no metadata or document_id, so the citation
        # is the fact itself plus its date range.
        cited.append(
            {
                "id": getattr(memory, "id", None),
                "text": getattr(memory, "text", ""),
                "type": getattr(memory, "type", None),
                "context": getattr(memory, "context", None),
                "occurred_start": getattr(memory, "occurred_start", None),
                "occurred_end": getattr(memory, "occurred_end", None),
            }
        )

    return {
        "current_incident": description,
        "report": getattr(response, "structured_output", None),
        "narrative": getattr(response, "text", ""),
        "evidence": evidence,
        "evidence_by_incident": group_by_incident(evidence),
        "runbook_facts": runbook_facts,
        "cited_memories": cited,
        "mental_models_used": [
            {"id": getattr(m, "id", None), "text": getattr(m, "text", "")}
            for m in getattr(response, "mental_models", None) or []
        ],
        "usage": getattr(response, "usage", None),
    }
