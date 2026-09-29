# Incident Response Agent — Hindsight-first organizational memory

A proof-of-concept that demonstrates persistent organizational memory for
incident response. Hindsight Cloud is the memory layer. The LLM is never
retrained; the agent improves because the memory gets richer.

The point is not "chat with your incident documents." It is: *the agent
remembers what happened in production, what caused it, what engineers tried,
what failed, and what actually worked.*

---

## Architecture

```
                       ENGINEER
                          |
                    simple web UI
                          |
                   INCIDENT AGENT
                          |
        +-----------------+------------------+
        |                                    |
   RECALL (evidence)                   REFLECT (reasoning)
   world + experience                  response_schema -> panels
   tag-scoped runbook recall           returns: text + structured_output
        |                                    |          + based_on (citations)
        +-----------------+------------------+
                          |
              HINDSIGHT CLOUD  (one memory bank)
                          |
   retain --> fact extraction --> entities --> graph/temporal links
          --> observation consolidation (background) --> recurring patterns
```

**One bank, not three.** A runbook is only useful *because* it is connected
to the incidents that justify it. Split banks would sever the graph and
temporal links. Scoping comes from `tags` and `metadata` instead.

---

## Setup

```powershell
cd C:\hello\incident-response-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env     # then paste your hsk_... key into .env
```

`.env` is gitignored. The API key is read only by `app/config.py`, used only
by `app/hindsight.py`, and never returned in any HTTP response. The browser
only ever talks to the local FastAPI backend.

---

## Build sequence

Run these in order. Do not skip ahead — each one proves the layer beneath it.

| Step | Command | Proves |
|------|---------|--------|
| 1 | `python scripts/smoke.py --stage introspect` | what the installed SDK actually exposes |
| 2 | `python scripts/smoke.py --stage connect` | reachability, auth, bank, read path |
| 3 | `python scripts/smoke.py --stage retain` | one memory stored |
| 4 | `python scripts/smoke.py --stage recall` | that memory retrievable |
| 5 | `python scripts/smoke.py --stage reflect` | reasoning over it works |
| 6 | `python scripts/load_history.py` | dry run — parses data/, writes nothing |
| 7 | `python scripts/load_history.py --commit` | history loaded |
| 8 | `uvicorn app.main:app --reload` | UI at http://127.0.0.1:8000 |

If any stage fails, it prints the exact exception. Fix that one thing. Do not
rewrite the project.

---

## Data format

`data/incidents/*.md`, `data/postmortems/*.md`, `data/runbooks/*.md`

Identity lives in a small front matter block. Everything else stays prose.

```markdown
---
incident_id: INC-001
title: Database connection exhaustion after deployment
date: 2025-11-14T14:32:00Z
service: payments-api
environment: production
severity: SEV-1
---

## Symptoms
...

## Failed actions
...
```

Front matter maps to: `document_id` (atomic idempotent upsert), `timestamp`
(real event time, so temporal retrieval works), `metadata` (exact,
client-side filtering), `tags` (server-side scoping), `entities` (guaranteed
graph links). The `##` sections each become one retain item, and each opens
with the incident identity so extracted facts carry `INC-001` attribution.

**Phrasing is a memory decision.** Extraction is LLM-judgment, so how a
section is worded determines what memory exists. A "Failed actions" section
saying "restart the API servers" extracts as an action taken, not as a
failure. Write non-events as explicit non-events:

> Restarting the API servers was attempted at 14:38. It did not resolve the
> incident. The cause was a misconfigured connection pool, which a restart
> cannot correct.

Verify with `python scripts/smoke.py --stage dryrun` — it previews extraction
against the real endpoint and persists nothing.

---

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | config + live Hindsight round trip |
| POST | `/api/investigate` | recall + reflect for a reported incident |
| POST | `/api/ingest` | post-mortem learning (`persist: false` previews) |
| GET | `/api/memory/summary` | how much the agent currently remembers |

---

## Safety

The agent is advisory. It has no production credentials and executes nothing.
Directives installed on the bank are enforced by Hindsight on every reflect:
a historical similarity is never stated as the current root cause, every
historical claim names its incident, and every production-changing action is
framed as a proposal requiring human approval. Rollbacks, restarts, config
changes and database commands are recommendations, never instructions.

---

## Cost

Retain, recall and reflect are token-metered. Recall is the cheap path
(evidence, no LLM). Reflect runs an agentic loop and dominates cost. Tune
`HINDSIGHT_REFLECT_BUDGET`, `HINDSIGHT_RECALL_MAX_TOKENS` and
`HINDSIGHT_REFLECT_MAX_TOKENS` in `.env`. A `402` means the account is out of
credits, which is a billing problem, not a code problem.
