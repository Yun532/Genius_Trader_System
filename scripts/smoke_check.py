"""Local reproducibility checks for a freshly cloned workspace.

By default this script avoids external network calls and API costs. Pass
``--llm`` to make one tiny JSON request through the configured DeepSeek/OpenAI
provider when at least one API key is present.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import llm
from backend.config import settings
from backend.database import get_conn, init_db


def check_database() -> dict:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tickers'").fetchone()
    return {"ok": bool(row), "path": settings.database_path}


def check_fastapi_import() -> dict:
    from backend.api.main import app

    routes = sorted(route.path for route in app.routes)
    return {
        "ok": "/api/health" in routes,
        "routes": len(routes),
    }


def check_llm_config(run_live: bool) -> dict:
    primary = (settings.llm_provider or "").lower()
    fallback = (settings.llm_fallback_provider or "").lower()
    has_primary_key = llm.has_api_key(primary)
    has_fallback_key = llm.has_api_key(fallback)
    result = {
        "ok": has_primary_key or has_fallback_key or not run_live,
        "primary": primary,
        "primary_model": settings.llm_model,
        "primary_key_present": has_primary_key,
        "fallback": fallback,
        "fallback_model": settings.llm_fallback_model,
        "fallback_key_present": has_fallback_key,
        "live_test": "skipped",
    }
    if not run_live:
        return result
    if not (has_primary_key or has_fallback_key):
        result["ok"] = False
        result["live_test"] = "missing_key"
        return result

    payload = llm.chat_json(
        [
            {
                "role": "user",
                "content": 'Return JSON only: {"status":"ok","provider":"configured"}',
            }
        ],
        max_tokens=80,
    )
    result["ok"] = payload.get("status") == "ok"
    result["live_test"] = payload
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local project smoke checks.")
    parser.add_argument("--llm", action="store_true", help="also make one live LLM API call")
    args = parser.parse_args()

    checks = {
        "database": check_database(),
        "fastapi": check_fastapi_import(),
        "llm": check_llm_config(args.llm),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
