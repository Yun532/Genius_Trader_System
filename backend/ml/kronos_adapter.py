"""Optional Kronos daily K-line forecast adapter.

Kronos is kept as an optional dependency so the normal research app can run
without PyTorch or downloaded Hugging Face weights.
"""

from __future__ import annotations

import importlib
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.config import settings
from backend.database import get_conn


class KronosUnavailable(RuntimeError):
    """Raised when Kronos cannot be loaded in the current environment."""


def _load_recent_ohlcv(symbol: str, lookback: int) -> pd.DataFrame:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM ohlc
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (symbol, lookback),
    ).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(row) for row in rows])
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0)
    df["amount"] = 0.0
    return df


def _append_repo_path() -> None:
    if not settings.kronos_repo_path:
        return
    path = Path(settings.kronos_repo_path).expanduser().resolve()
    if not path.exists():
        raise KronosUnavailable(f"KRONOS_REPO_PATH does not exist: {path}")
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


@lru_cache(maxsize=1)
def _load_predictor() -> Any:
    if not settings.kronos_enabled:
        raise KronosUnavailable("Kronos is disabled. Set KRONOS_ENABLED=true to enable it.")

    _append_repo_path()

    try:
        model_module = importlib.import_module("model")
    except Exception as exc:  # pragma: no cover - depends on optional third-party install
        raise KronosUnavailable(
            "Kronos code is not importable. Clone https://github.com/shiyu-coder/Kronos "
            "and set KRONOS_REPO_PATH, or install it in the Python environment."
        ) from exc

    try:
        Kronos = getattr(model_module, "Kronos")
        KronosTokenizer = getattr(model_module, "KronosTokenizer")
        KronosPredictor = getattr(model_module, "KronosPredictor")
    except AttributeError as exc:  # pragma: no cover
        raise KronosUnavailable("Imported `model` module is not the Kronos model package.") from exc

    try:
        tokenizer = KronosTokenizer.from_pretrained(settings.kronos_tokenizer_name)
        model = Kronos.from_pretrained(settings.kronos_model_name)
        return KronosPredictor(
            model,
            tokenizer,
            device=settings.kronos_device,
            max_context=settings.kronos_max_context,
        )
    except Exception as exc:  # pragma: no cover
        raise KronosUnavailable(
            "Kronos model/tokenizer could not be loaded. Check Hugging Face access, "
            "local cache, PyTorch installation, and KRONOS_* settings."
        ) from exc


def _future_business_days(last_date: pd.Timestamp, pred_len: int) -> pd.Series:
    start = last_date + pd.offsets.BDay(1)
    return pd.Series(pd.bdate_range(start=start, periods=pred_len), name="date")


def _to_prediction_records(pred_df: pd.DataFrame, fallback_dates: pd.Series | None = None) -> list[dict[str, Any]]:
    records = []
    rows = pred_df.reset_index().to_dict("records")
    for index, row in enumerate(rows):
        item: dict[str, Any] = {}
        for key, value in row.items():
            if key in {"date", "timestamp", "timestamps", "index"}:
                if hasattr(value, "strftime"):
                    item["date"] = value.strftime("%Y-%m-%d")
                else:
                    item["date"] = str(value)[:10]
            elif isinstance(value, (int, float, np.integer, np.floating)) and np.isfinite(value):
                item[key] = round(float(value), 4)
        if "date" not in item and fallback_dates is not None and index < len(fallback_dates):
            value = fallback_dates.iloc[index]
            item["date"] = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]
        records.append(item)
    return records


def _sanitize_ohlc(pred_df: pd.DataFrame) -> pd.DataFrame:
    pred_df = pred_df.copy()
    cols = [col for col in ["open", "high", "low", "close"] if col in pred_df.columns]
    if len(cols) < 4:
        return pred_df
    ohlc = pred_df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    pred_df["high"] = ohlc.max(axis=1)
    pred_df["low"] = ohlc.min(axis=1)
    return pred_df


def generate_kronos_reference(
    symbol: str,
    lookback: int = 120,
    pred_len: int = 5,
    sample_count: int = 1,
) -> dict[str, Any]:
    """Generate a Kronos daily K-line reference for a symbol."""
    if lookback < 30:
        return {"error": "lookback must be at least 30 daily bars"}
    if pred_len < 1:
        return {"error": "pred_len must be at least 1"}

    df = _load_recent_ohlcv(symbol, lookback)
    if df.empty:
        return {"error": f"No OHLC data for {symbol}"}
    if len(df) < 30:
        return {"error": f"Need at least 30 daily bars for Kronos, got {len(df)}"}

    predictor = _load_predictor()

    history = df[["open", "high", "low", "close", "volume", "amount"]].copy()
    history_timestamps = df["date"].copy()
    future_timestamps = _future_business_days(df.iloc[-1]["date"], pred_len)

    try:
        pred_df = predictor.predict(
            df=history,
            x_timestamp=history_timestamps,
            y_timestamp=future_timestamps,
            pred_len=pred_len,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )
    except TypeError:
        # Older Kronos examples use positional arguments.
        pred_df = predictor.predict(history, history_timestamps, future_timestamps, pred_len)
    except Exception as exc:  # pragma: no cover
        return {"error": f"Kronos inference failed: {exc}"}

    if not isinstance(pred_df, pd.DataFrame) or pred_df.empty:
        return {"error": "Kronos returned no forecast rows"}
    pred_df = _sanitize_ohlc(pred_df)

    last_close = float(df.iloc[-1]["close"])
    last_date = df.iloc[-1]["date"].strftime("%Y-%m-%d")

    close_path = pd.to_numeric(pred_df.get("close"), errors="coerce").dropna()
    if close_path.empty:
        return {"error": "Kronos forecast did not include close prices"}

    end_close = float(close_path.iloc[-1])
    t_return = end_close / last_close - 1

    up_steps = int((close_path.diff().dropna() > 0).sum())
    total_steps = max(len(close_path) - 1, 1)
    path_up_ratio = up_steps / total_steps
    verdict = "positive" if t_return > 0.01 else "negative" if t_return < -0.01 else "neutral"

    return {
        "symbol": symbol,
        "model": settings.kronos_model_name,
        "tokenizer": settings.kronos_tokenizer_name,
        "as_of_date": last_date,
        "lookback": len(df),
        "pred_len": pred_len,
        "sample_count": sample_count,
        "last_close": round(last_close, 4),
        "predicted_close": round(end_close, 4),
        "predicted_return": round(t_return, 4),
        "path_up_ratio": round(path_up_ratio, 4),
        "verdict": verdict,
        "forecast": _to_prediction_records(pred_df, future_timestamps),
        "warning": "Kronos output is a research reference only. It is not investment advice.",
        "notes": [
            "Future dates use business-day placeholders and may not exclude A-share holidays.",
            "This endpoint should be paired with rolling backtests before production use.",
        ],
    }
