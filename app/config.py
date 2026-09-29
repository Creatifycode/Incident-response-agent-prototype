"""Environment and path configuration.

This is the only module that reads the environment. Nothing else in the
project touches os.environ, and no secret is ever returned to a caller.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
INCIDENTS_DIR = DATA_DIR / "incidents"
POSTMORTEMS_DIR = DATA_DIR / "postmortems"
RUNBOOKS_DIR = DATA_DIR / "runbooks"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

load_dotenv(PROJECT_ROOT / ".env")

HINDSIGHT_BASE_URL = os.environ.get(
    "HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io"
).rstrip("/")

# Secret. Server-side only. Never logged, never returned over HTTP.
HINDSIGHT_API_KEY = os.environ.get("HINDSIGHT_API_KEY", "")

HINDSIGHT_BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "prod-incident-memory")

# Reflect is the expensive operation. Exposed as a knob so a credit-constrained
# run can drop to "low" without a code change.
HINDSIGHT_REFLECT_BUDGET = os.environ.get("HINDSIGHT_REFLECT_BUDGET", "mid")
HINDSIGHT_RECALL_BUDGET = os.environ.get("HINDSIGHT_RECALL_BUDGET", "mid")

# Token budgets handed to recall. Recall bills on retrieved content, so these
# are the main lever on cost for the grounding step.
RECALL_MAX_TOKENS = int(os.environ.get("HINDSIGHT_RECALL_MAX_TOKENS", "4096"))
REFLECT_MAX_TOKENS = int(os.environ.get("HINDSIGHT_REFLECT_MAX_TOKENS", "4096"))


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def require_hindsight_config() -> None:
    """Fail loudly and early rather than issuing a request that returns 401."""
    if not HINDSIGHT_API_KEY:
        raise ConfigError(
            "HINDSIGHT_API_KEY is not set. Copy .env.example to .env and add "
            "your key, or export HINDSIGHT_API_KEY in this shell."
        )
    if not HINDSIGHT_BANK_ID:
        raise ConfigError("HINDSIGHT_BANK_ID is not set.")


def public_config() -> dict:
    """Config that is safe to expose to the browser. Contains no secrets."""
    return {
        "base_url": HINDSIGHT_BASE_URL,
        "bank_id": HINDSIGHT_BANK_ID,
        "reflect_budget": HINDSIGHT_REFLECT_BUDGET,
        "recall_budget": HINDSIGHT_RECALL_BUDGET,
    }
