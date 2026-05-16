"""Feature engineering: one row per trading day per ticker."""

import pandas as pd
import numpy as np
from backend.database import get_conn


def _load_news_features(symbol: str) -> pd.DataFrame:
    """Aggregate news_aligned + layer1_results per trade_date."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT na.trade_date,
               COUNT(*)                                          AS n_articles,
               SUM(CASE WHEN l1.relevance IN ('high','medium') THEN 1 ELSE 0 END) AS n_relevant,
               SUM(CASE WHEN l1.sentiment = 'positive' THEN 1 ELSE 0 END) AS n_positive,
               SUM(CASE WHEN l1.sentiment = 'negative' THEN 1 ELSE 0 END) AS n_negative,
               SUM(CASE WHEN l1.sentiment = 'neutral'  THEN 1 ELSE 0 END) AS n_neutral
        FROM news_aligned na
        JOIN layer1_results l1 ON na.news_id = l1.news_id AND na.symbol = l1.symbol
        WHERE na.symbol = ?
        GROUP BY na.trade_date
        ORDER BY na.trade_date
        """,
        (symbol,),
    ).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    total = df["n_articles"].clip(lower=1)
    df["sentiment_score"] = (df["n_positive"] - df["n_negative"]) / total
    df["relevance_ratio"] = df["n_relevant"] / total
    df["positive_ratio"] = df["n_positive"] / total
    df["negative_ratio"] = df["n_negative"] / total
    df["has_news"] = 1
    return df


def _load_ohlc(symbol: str) -> pd.DataFrame:
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM ohlc WHERE symbol = ? ORDER BY date",
        (symbol,),
    ).fetchall()
    conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_features(symbol: str) -> pd.DataFrame:
    """Build feature matrix: one row per trading day.

    All features use shift(1) or past windows to prevent look-ahead leakage.
    Target: whether close > previous close (binary up/down).
    """
    ohlc = _load_ohlc(symbol)
    if ohlc.empty or len(ohlc) < 30:
        return pd.DataFrame()

    news = _load_news_features(symbol)

    # Merge news onto OHLC dates
    df = ohlc.rename(columns={"date": "trade_date"})
    if not news.empty:
        df = df.merge(news, on="trade_date", how="left")
    else:
        for col in ["n_articles", "n_relevant", "n_positive", "n_negative",
                     "n_neutral", "sentiment_score", "relevance_ratio",
                     "positive_ratio", "negative_ratio", "has_news"]:
            df[col] = 0

    # Fill missing news days
    news_cols = ["n_articles", "n_relevant", "n_positive", "n_negative",
                 "n_neutral", "sentiment_score", "relevance_ratio",
                 "positive_ratio", "negative_ratio", "has_news"]
    df[news_cols] = df[news_cols].fillna(0)

    # --- Rolling news features (use current + past, no shift needed since news is pre-market/same day) ---
    for w in [3, 5, 10]:
        df[f"sentiment_score_{w}d"] = df["sentiment_score"].rolling(w, min_periods=1).mean()
        df[f"positive_ratio_{w}d"] = df["positive_ratio"].rolling(w, min_periods=1).mean()
        df[f"negative_ratio_{w}d"] = df["negative_ratio"].rolling(w, min_periods=1).mean()
        df[f"news_count_{w}d"] = df["n_articles"].rolling(w, min_periods=1).sum()
    # Sentiment momentum: 3d mean - 10d mean
    df["sentiment_momentum_3d"] = df["sentiment_score_3d"] - df["sentiment_score_10d"]

    # --- Price / technical features (shifted by 1 to prevent leakage) ---
    close = df["close"]
    df["ret_1d"] = close.pct_change(1).shift(1)
    df["ret_3d"] = close.pct_change(3).shift(1)
    df["ret_5d"] = close.pct_change(5).shift(1)
    df["ret_10d"] = close.pct_change(10).shift(1)

    df["volatility_5d"] = close.pct_change().rolling(5).std().shift(1)
    df["volatility_10d"] = close.pct_change().rolling(10).std().shift(1)

    avg_vol_5 = df["volume"].rolling(5).mean().shift(1)
    df["volume_ratio_5d"] = (df["volume"].shift(1) / avg_vol_5.clip(lower=1))

    df["gap"] = (df["open"] / close.shift(1) - 1).shift(1)

    ma5 = close.rolling(5).mean().shift(1)
    ma20 = close.rolling(20).mean().shift(1)
    df["ma5_vs_ma20"] = (ma5 / ma20.clip(lower=0.01) - 1)

    # RSI 14
    delta = close.diff().shift(1)
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.clip(lower=1e-10)
    df["rsi_14"] = 100 - 100 / (1 + rs)

    df["day_of_week"] = df["trade_date"].dt.dayofweek

    # --- Targets: next-N-day direction ---
    df["target_t1"] = (close.shift(-1) > close).astype(int)
    df["target_t2"] = (close.shift(-2) > close).astype(int)
    df["target_t3"] = (close.shift(-3) > close).astype(int)
    df["target_t5"] = (close.shift(-5) > close).astype(int)

    # Drop rows without enough history
    df = df.dropna(subset=["ret_10d", "rsi_14"]).reset_index(drop=True)

    return df


def build_features_multi(symbols: list[str] | None = None) -> pd.DataFrame:
    """Build combined feature matrix for multiple tickers.

    Adds a 'symbol' column. All price features are already returns/ratios
    so they are comparable across tickers.
    """
    if symbols is None:
        from backend.database import get_conn
        conn = get_conn()
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM ohlc"
        ).fetchall()
        conn.close()
        symbols = [r["symbol"] for r in rows]

    frames = []
    for sym in symbols:
        df = build_features(sym)
        if df.empty:
            continue
        df["symbol"] = sym
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


FEATURE_COLS = [
    # News
    "n_articles", "n_relevant", "n_positive", "n_negative", "n_neutral",
    "sentiment_score", "relevance_ratio", "positive_ratio", "negative_ratio", "has_news",
    # Rolling news
    "sentiment_score_3d", "sentiment_score_5d", "sentiment_score_10d",
    "positive_ratio_3d", "positive_ratio_5d", "positive_ratio_10d",
    "negative_ratio_3d", "negative_ratio_5d", "negative_ratio_10d",
    "news_count_3d", "news_count_5d", "news_count_10d",
    "sentiment_momentum_3d",
    # Price / tech
    "ret_1d", "ret_3d", "ret_5d", "ret_10d",
    "volatility_5d", "volatility_10d",
    "volume_ratio_5d", "gap", "ma5_vs_ma20", "rsi_14", "day_of_week",
]
