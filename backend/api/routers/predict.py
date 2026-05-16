"""Prediction API endpoints."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from backend.ashare.symbol import normalize

router = APIRouter()

MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "ml" / "models"


@router.get("/{symbol}")
def get_prediction(symbol: str, horizon: str = Query("t1", regex="^t[15]$")):
    """Get direction prediction for a symbol."""
    from backend.ml.model import predict

    sym = _norm(symbol)
    result = predict(sym, horizon)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/{symbol}/backtest")
def get_backtest(symbol: str, horizon: str = Query("t1", regex="^t[15]$")):
    """Get backtest results for a symbol."""
    sym = _norm(symbol)
    path = MODELS_DIR / f"{sym}_{horizon}_backtest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No backtest for {sym}/{horizon}. Run training with --backtest.")
    return json.loads(path.read_text())


@router.get("/{symbol}/forecast")
def get_forecast(symbol: str, window: int = Query(7, ge=3, le=60)):
    """Generate forecast based on recent news window (7d or 30d)."""
    from backend.ml.inference import generate_forecast

    result = generate_forecast(_norm(symbol), window)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/{symbol}/similar-days")
def get_similar_days(symbol: str, date: str = Query(...), top_k: int = Query(10, ge=1, le=30)):
    """Find historically similar trading days based on ML features."""
    from backend.ml.similar import find_similar_days

    result = find_similar_days(_norm(symbol), date, top_k)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


def _norm(symbol: str) -> str:
    s = symbol.strip()
    if s.lower().startswith(("sh", "sz", "bj")) or (len(s) == 6 and s.isdigit()):
        return normalize(s)
    return s.upper()
