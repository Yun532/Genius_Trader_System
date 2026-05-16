import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from backend.config import settings
from backend.database import get_conn
from backend.llm import configured as llm_configured


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def analysis_cache_key(cache_type: str, parts: dict) -> str:
    return f"{cache_type}:{stable_hash(parts)}"


def get_analysis_cache(cache_type: str, parts: dict) -> Optional[dict]:
    if not settings.llm_cache_enabled:
        return None
    key = analysis_cache_key(cache_type, parts)
    now = datetime.now().isoformat()
    conn = get_conn()
    row = conn.execute(
        """SELECT payload_json, llm_used, created_at, expires_at, meta_json
           FROM analysis_cache
           WHERE cache_key = ?
             AND (expires_at IS NULL OR expires_at >= ?)""",
        (key, now),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        payload.setdefault("llm_used", bool(row["llm_used"]))
        payload["cached"] = True
        payload["cache"] = {
            "hit": True,
            "type": cache_type,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "meta": json.loads(row["meta_json"] or "{}"),
        }
    return payload


def store_analysis_cache(
    cache_type: str,
    parts: dict,
    payload: dict,
    *,
    ttl_hours: Optional[int],
    llm_used: bool,
    meta: Optional[dict] = None,
) -> None:
    if not settings.llm_cache_enabled:
        return
    now = datetime.now()
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours and ttl_hours > 0 else None
    to_store = dict(payload)
    to_store["cached"] = False
    to_store.pop("cache", None)
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO analysis_cache
           (cache_key, cache_type, payload_json, llm_used, created_at, expires_at, meta_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            analysis_cache_key(cache_type, parts),
            cache_type,
            json.dumps(to_store, ensure_ascii=False),
            1 if llm_used else 0,
            now.isoformat(),
            expires_at,
            json.dumps(meta or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def llm_date_policy(
    date_text: Optional[str],
    *,
    use_llm: Optional[bool] = None,
    force_llm: bool = False,
) -> dict:
    if use_llm is False:
        return {"use_llm": False, "reason": "request_disabled"}
    if not llm_configured():
        return {"use_llm": False, "reason": "llm_not_configured"}
    if force_llm or use_llm is True:
        return {"use_llm": True, "reason": "forced" if force_llm else "request_enabled"}
    try:
        target = datetime.fromisoformat(str(date_text)).date()
    except (TypeError, ValueError):
        return {"use_llm": False, "reason": "invalid_or_missing_date"}
    recent_days = max(0, int(settings.llm_recent_auto_days))
    cutoff = datetime.now().date() - timedelta(days=recent_days)
    if target < cutoff:
        return {
            "use_llm": False,
            "reason": "historical_local_only",
            "recent_auto_days": recent_days,
            "cutoff": cutoff.isoformat(),
        }
    return {
        "use_llm": True,
        "reason": "recent_auto_allowed",
        "recent_auto_days": recent_days,
        "cutoff": cutoff.isoformat(),
    }
