import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

from backend.database import get_conn
from backend.pipeline.layer0 import run_layer0
from backend.pipeline.layer1 import get_pending_articles, run_layer1, check_batch_status, collect_batch_results
from backend.pipeline.alignment import align_news_for_symbol
from backend.api.routers.stocks import sync_symbol
from backend.ashare.symbol import normalize

import json

router = APIRouter()


class FetchRequest(BaseModel):
    symbol: str
    start: Optional[str] = None
    end: Optional[str] = None


class ProcessRequest(BaseModel):
    symbol: str
    batch_size: int = 1000


@router.post("/fetch")
def trigger_fetch(req: FetchRequest, background_tasks: BackgroundTasks):
    """Trigger A-share data fetch for a symbol."""
    symbol = _norm(req.symbol)
    today = datetime.now(timezone.utc).date()
    start = req.start or (today - timedelta(days=2 * 366)).isoformat()
    end = req.end or today.isoformat()

    background_tasks.add_task(_do_fetch, symbol, start, end)
    return {"symbol": symbol, "status": "fetch_started", "start": start, "end": end}


def _do_fetch(symbol: str, start: str, end: str):
    """Background fetch of OHLC + news data."""
    try:
        sync_symbol(symbol, start, end)
    except Exception:
        logger.exception("Fetch error for %s", symbol)


@router.post("/process")
def trigger_process(req: ProcessRequest):
    """Run Layer 0 filter, then submit Layer 1 batch for remaining articles."""
    symbol = _norm(req.symbol)

    # Step 1: Alignment
    align_result = align_news_for_symbol(symbol)

    # Step 2: Layer 0
    l0_stats = run_layer0(symbol)

    # Step 3: Run Layer 1 (50 articles per API call)
    l1_stats = run_layer1(symbol, max_articles=req.batch_size)

    return {
        "symbol": symbol,
        "alignment": align_result,
        "layer0": l0_stats,
        "layer1": l1_stats,
    }


@router.get("/batch/{batch_id}")
def get_batch_status(batch_id: str):
    """Check status of a batch job."""
    status = check_batch_status(batch_id)

    # If ended, collect results
    if status["status"] == "ended":
        collect_stats = collect_batch_results(batch_id)
        status["collect_stats"] = collect_stats

    return status


def _norm(symbol: str) -> str:
    s = symbol.strip()
    if s.lower().startswith(("sh", "sz", "bj")) or (len(s) == 6 and s.isdigit()):
        return normalize(s)
    return s.upper()
