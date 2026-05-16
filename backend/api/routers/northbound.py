"""Northbound capital flow (沪深港通) API endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, Query

from backend.database import get_conn
from backend.ashare.client import fetch_northbound_flow

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/flow")
def get_northbound_flow(days: int = Query(30, ge=1, le=365)):
    """Get daily northbound capital net flow."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM northbound_flow ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    conn.close()

    if rows:
        return [dict(r) for r in reversed(rows)]

    # If no cached data, try fetching live
    try:
        data = fetch_northbound_flow(days=days)
        if data:
            conn = get_conn()
            for row in data:
                conn.execute(
                    """INSERT OR REPLACE INTO northbound_flow
                       (date, sh_net_flow, sz_net_flow, total_flow)
                       VALUES (?, ?, ?, ?)""",
                    (row["date"], row["sh_net_flow"], row["sz_net_flow"], row["total_flow"]),
                )
            conn.commit()
            conn.close()
            return data[-days:]
    except Exception:
        logger.warning("Failed to fetch northbound flow data")

    return []


@router.get("/holding/{symbol}")
def get_northbound_holding(symbol: str, days: int = Query(30, ge=1, le=365)):
    """Get northbound holding changes for a specific stock."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM northbound_holding WHERE symbol = ? ORDER BY date DESC LIMIT ?",
        (symbol, days),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]
