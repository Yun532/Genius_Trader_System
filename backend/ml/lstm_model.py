"""LSTM sequence model for stock direction prediction.

Takes a sliding window of N daily feature vectors as input,
captures temporal patterns like "3 days negative then positive".
"""

import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from sklearn.preprocessing import StandardScaler
import joblib

from backend.database import get_conn
from backend.ml.features import build_features, FEATURE_COLS
from backend.ml.features_v2 import FEATURE_COLS_V2_MARKET

MODELS_DIR = Path(__file__).parent / "models"


# ---- Sentiment-filtered news features ----

def _load_news_features_filtered(symbol: str, exclude_neutral: bool = False) -> pd.DataFrame:
    """Load news features, optionally excluding neutral articles."""
    conn = get_conn()
    where_clause = ""
    if exclude_neutral:
        where_clause = "AND l1.sentiment IN ('positive', 'negative')"

    rows = conn.execute(
        f"""
        SELECT na.trade_date,
               COUNT(*)                                                      AS n_articles,
               SUM(CASE WHEN l1.sentiment = 'positive' THEN 1 ELSE 0 END)   AS n_positive,
               SUM(CASE WHEN l1.sentiment = 'negative' THEN 1 ELSE 0 END)   AS n_negative,
               SUM(CASE WHEN l1.relevance IN ('high','medium') THEN 1 ELSE 0 END) AS n_relevant,
               AVG(CASE WHEN l1.sentiment = 'positive' THEN 1
                        WHEN l1.sentiment = 'negative' THEN -1 ELSE 0 END)  AS avg_polarity
        FROM news_aligned na
        JOIN layer1_results l1 ON na.news_id = l1.news_id AND na.symbol = l1.symbol
        WHERE na.symbol = ? {where_clause}
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
    df["positive_ratio"] = df["n_positive"] / total
    df["negative_ratio"] = df["n_negative"] / total
    df["polarity_strength"] = df["avg_polarity"].abs()  # how polarized
    return df


def build_features_filtered(symbol: str, exclude_neutral: bool = True) -> pd.DataFrame:
    """Build features using only positive/negative news (no neutral noise)."""
    from backend.ml.features import _load_ohlc

    ohlc = _load_ohlc(symbol)
    if ohlc.empty or len(ohlc) < 30:
        return pd.DataFrame()

    news = _load_news_features_filtered(symbol, exclude_neutral=exclude_neutral)
    df = ohlc.rename(columns={"date": "trade_date"})

    if not news.empty:
        df = df.merge(news, on="trade_date", how="left")
    else:
        for col in ["n_articles", "n_positive", "n_negative", "n_relevant",
                     "sentiment_score", "positive_ratio", "negative_ratio",
                     "avg_polarity", "polarity_strength"]:
            df[col] = 0

    fill_cols = ["n_articles", "n_positive", "n_negative", "n_relevant",
                 "sentiment_score", "positive_ratio", "negative_ratio",
                 "avg_polarity", "polarity_strength"]
    df[fill_cols] = df[fill_cols].fillna(0)

    # Rolling
    for w in [3, 5, 10]:
        df[f"sent_{w}d"] = df["sentiment_score"].rolling(w, min_periods=1).mean()
        df[f"posratio_{w}d"] = df["positive_ratio"].rolling(w, min_periods=1).mean()
        df[f"negratio_{w}d"] = df["negative_ratio"].rolling(w, min_periods=1).mean()
        df[f"polarity_{w}d"] = df["polarity_strength"].rolling(w, min_periods=1).mean()
        df[f"newscount_{w}d"] = df["n_articles"].rolling(w, min_periods=1).sum()
    df["sent_momentum"] = df["sent_3d"] - df["sent_10d"]

    # Price/tech features (same as v1)
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
    delta = close.diff().shift(1)
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.clip(lower=1e-10)
    df["rsi_14"] = 100 - 100 / (1 + rs)
    df["day_of_week"] = df["trade_date"].dt.dayofweek

    # Market sentiment
    from backend.ml.features_v2 import _load_market_sentiment
    mkt = _load_market_sentiment()
    if not mkt.empty:
        df = df.merge(mkt, on="trade_date", how="left")
        for col in ["mkt_sentiment", "mkt_positive_ratio", "mkt_sentiment_3d",
                     "mkt_sentiment_5d", "mkt_momentum"]:
            df[col] = df[col].fillna(0)

    # Targets
    df["target_t1"] = (close.shift(-1) > close).astype(int)
    df["target_t3"] = (close.shift(-3) > close).astype(int)
    df["target_big1_t1"] = ((close.shift(-1) / close - 1).abs() > 0.01).astype(int)

    df = df.dropna(subset=["ret_10d", "rsi_14"]).reset_index(drop=True)
    return df


# Feature columns for filtered version
FILTERED_FEATURE_COLS = [
    "n_articles", "n_positive", "n_negative", "n_relevant",
    "sentiment_score", "positive_ratio", "negative_ratio", "polarity_strength",
    "sent_3d", "sent_5d", "sent_10d",
    "posratio_3d", "posratio_5d", "posratio_10d",
    "negratio_3d", "negratio_5d", "negratio_10d",
    "polarity_3d", "polarity_5d", "polarity_10d",
    "newscount_3d", "newscount_5d", "newscount_10d",
    "sent_momentum",
    "ret_1d", "ret_3d", "ret_5d", "ret_10d",
    "volatility_5d", "volatility_10d",
    "volume_ratio_5d", "gap", "ma5_vs_ma20", "rsi_14", "day_of_week",
    "mkt_sentiment", "mkt_positive_ratio", "mkt_sentiment_3d", "mkt_sentiment_5d", "mkt_momentum",
]


# ---- LSTM Model ----

class SequenceDataset(Dataset):
    def __init__(self, X_seq, y):
        self.X = torch.FloatTensor(X_seq)
        self.y = torch.LongTensor(y)

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]


class StockLSTM(nn.Module):
    def __init__(self, input_size, hidden_size=64, num_layers=2, dropout=0.3):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                            batch_first=True, dropout=dropout if num_layers > 1 else 0)
        self.dropout = nn.Dropout(dropout)
        self.fc = nn.Linear(hidden_size, 2)

    def forward(self, x):
        # x: (batch, seq_len, features)
        out, (hn, _) = self.lstm(x)
        # Use last hidden state
        last = hn[-1]  # (batch, hidden)
        last = self.dropout(last)
        return self.fc(last)


def make_sequences(X: np.ndarray, y: np.ndarray, seq_len: int = 10):
    """Create sliding window sequences from feature matrix."""
    X_seq, y_seq = [], []
    for i in range(seq_len, len(X)):
        X_seq.append(X[i - seq_len:i])
        y_seq.append(y[i])
    return np.array(X_seq), np.array(y_seq)


def train_lstm(X_train_seq, y_train, X_test_seq, y_test,
               input_size, epochs=50, lr=0.001, batch_size=32):
    """Train LSTM and return predictions."""
    device = torch.device("cpu")

    train_ds = SequenceDataset(X_train_seq, y_train)
    train_dl = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

    model = StockLSTM(input_size, hidden_size=64, num_layers=2, dropout=0.3).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()

    model.train()
    for epoch in range(epochs):
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            out = criterion(model(xb), yb)
            out.backward()
            optimizer.step()

    # Predict
    model.eval()
    with torch.no_grad():
        X_test_t = torch.FloatTensor(X_test_seq).to(device)
        logits = model(X_test_t)
        preds = logits.argmax(dim=1).cpu().numpy()

    return preds, model


def run_lstm_backtest(symbol: str, target_col: str = "target_t3",
                      feature_cols: list = None, seq_len: int = 10,
                      n_folds: int = 5, min_train: int = 200,
                      exclude_neutral: bool = False) -> dict:
    """Run expanding-window CV with LSTM."""
    if exclude_neutral:
        df = build_features_filtered(symbol, exclude_neutral=True)
        if feature_cols is None:
            feature_cols = FILTERED_FEATURE_COLS
    else:
        from backend.ml.features_v2 import build_features_v2
        df = build_features_v2(symbol)
        if feature_cols is None:
            feature_cols = FEATURE_COLS_V2_MARKET

    if df.empty:
        return {"error": f"No data for {symbol}"}

    df = df.dropna(subset=[target_col]).reset_index(drop=True)
    valid_cols = [c for c in feature_cols if c in df.columns]
    X_raw = df[valid_cols].values.astype(np.float64)
    np.nan_to_num(X_raw, copy=False)
    y_raw = df[target_col].values.astype(int)

    n = len(X_raw)
    if n < min_train + seq_len + 20:
        return {"error": f"Too few rows ({n})"}

    test_size = (n - min_train) // n_folds
    if test_size < 10:
        n_folds = max(1, (n - min_train) // 10)
        test_size = (n - min_train) // n_folds

    all_true, all_pred = [], []
    folds = []

    for fold in range(n_folds):
        train_end = min_train + fold * test_size
        test_end = train_end + test_size if fold < n_folds - 1 else n

        # Scale based on training data only
        scaler = StandardScaler()
        X_scaled = X_raw.copy()
        X_scaled[:train_end] = scaler.fit_transform(X_raw[:train_end])
        X_scaled[train_end:] = scaler.transform(X_raw[train_end:])

        # Create sequences
        X_seq, y_seq = make_sequences(X_scaled, y_raw, seq_len)
        # Adjust indices: sequence i corresponds to original index i+seq_len
        adj_train_end = train_end - seq_len
        adj_test_end = test_end - seq_len

        if adj_train_end < 50 or adj_test_end <= adj_train_end:
            continue

        X_tr = X_seq[:adj_train_end]
        y_tr = y_seq[:adj_train_end]
        X_te = X_seq[adj_train_end:adj_test_end]
        y_te = y_seq[adj_train_end:adj_test_end]

        if len(y_te) < 5:
            continue

        preds, _ = train_lstm(X_tr, y_tr, X_te, y_te,
                              input_size=len(valid_cols), epochs=40, lr=0.001)

        acc = accuracy_score(y_te, preds)
        base = max(y_te.mean(), 1 - y_te.mean())

        folds.append({
            "fold": fold + 1,
            "train_size": len(y_tr),
            "test_size": len(y_te),
            "accuracy": round(acc, 4),
            "baseline": round(base, 4),
            "precision": round(precision_score(y_te, preds, zero_division=0), 4),
            "recall": round(recall_score(y_te, preds, zero_division=0), 4),
            "f1": round(f1_score(y_te, preds, zero_division=0), 4),
        })

        all_true.extend(y_te.tolist())
        all_pred.extend(preds.tolist())

    if not all_true:
        return {"error": "No valid folds"}

    t = np.array(all_true)
    p = np.array(all_pred)
    overall_acc = accuracy_score(t, p)
    overall_base = max(t.mean(), 1 - t.mean())

    return {
        "symbol": symbol,
        "model": "LSTM",
        "target": target_col,
        "seq_len": seq_len,
        "exclude_neutral": exclude_neutral,
        "n_folds": len(folds),
        "total_predictions": len(t),
        "overall_accuracy": round(overall_acc, 4),
        "overall_baseline": round(overall_base, 4),
        "lift": round((overall_acc - overall_base) * 100, 1),
        "overall_precision": round(precision_score(t, p, zero_division=0), 4),
        "overall_recall": round(recall_score(t, p, zero_division=0), 4),
        "overall_f1": round(f1_score(t, p, zero_division=0), 4),
        "folds": folds,
    }


# ---- Production: train full data, save model ----

def train_and_save_lstm(symbol: str, target_col: str = "target_t3",
                        feature_cols: list = None, seq_len: int = 10,
                        exclude_neutral: bool = False, epochs: int = 50) -> dict:
    """Train LSTM on ALL available data and save for production inference."""
    if exclude_neutral:
        df = build_features_filtered(symbol, exclude_neutral=True)
        if feature_cols is None:
            feature_cols = FILTERED_FEATURE_COLS
    else:
        from backend.ml.features_v2 import build_features_v2
        df = build_features_v2(symbol)
        if feature_cols is None:
            feature_cols = FEATURE_COLS_V2_MARKET

    if df.empty:
        return {"error": f"No data for {symbol}"}

    df = df.dropna(subset=[target_col]).reset_index(drop=True)
    valid_cols = [c for c in feature_cols if c in df.columns]
    X_raw = df[valid_cols].values.astype(np.float64)
    np.nan_to_num(X_raw, copy=False)
    y_raw = df[target_col].values.astype(int)

    n = len(X_raw)
    if n < 100:
        return {"error": f"Too few rows ({n})"}

    # Scale all data
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_raw)

    # Create sequences
    X_seq, y_seq = make_sequences(X_scaled, y_raw, seq_len)

    # Train on all data
    device = torch.device("cpu")
    train_ds = SequenceDataset(X_seq, y_seq)
    train_dl = DataLoader(train_ds, batch_size=32, shuffle=True)

    model = StockLSTM(len(valid_cols), hidden_size=64, num_layers=2, dropout=0.3).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()

    model.train()
    for epoch in range(epochs):
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            optimizer.step()

    # Save model artifacts
    MODELS_DIR.mkdir(exist_ok=True)
    prefix = f"{symbol}_lstm"

    torch.save(model.state_dict(), MODELS_DIR / f"{prefix}.pt")
    joblib.dump(scaler, MODELS_DIR / f"{prefix}_scaler.joblib")

    meta = {
        "symbol": symbol,
        "model_type": "LSTM",
        "target": target_col,
        "seq_len": seq_len,
        "feature_cols": valid_cols,
        "exclude_neutral": exclude_neutral,
        "input_size": len(valid_cols),
        "hidden_size": 64,
        "num_layers": 2,
        "dropout": 0.3,
        "train_size": len(y_seq),
        "trained_at": datetime.now().isoformat(),
    }
    (MODELS_DIR / f"{prefix}_meta.json").write_text(json.dumps(meta, indent=2))

    print(f"  Saved LSTM model: {prefix}.pt ({len(y_seq)} sequences)")
    return meta


def predict_lstm(symbol: str) -> dict | None:
    """Load saved LSTM model and predict direction for the latest data.

    Returns prediction dict or None if no LSTM model exists for this symbol.
    """
    prefix = f"{symbol}_lstm"
    pt_path = MODELS_DIR / f"{prefix}.pt"
    meta_path = MODELS_DIR / f"{prefix}_meta.json"
    scaler_path = MODELS_DIR / f"{prefix}_scaler.joblib"

    if not pt_path.exists():
        return None

    meta = json.loads(meta_path.read_text())
    scaler = joblib.load(scaler_path)
    valid_cols = meta["feature_cols"]
    seq_len = meta["seq_len"]
    exclude_neutral = meta.get("exclude_neutral", False)
    target_col = meta["target"]

    # Build features
    if exclude_neutral:
        df = build_features_filtered(symbol, exclude_neutral=True)
    else:
        from backend.ml.features_v2 import build_features_v2
        df = build_features_v2(symbol)

    if df.empty or len(df) < seq_len:
        return None

    # Prepare last seq_len rows
    X_raw = df[valid_cols].values.astype(np.float64)
    np.nan_to_num(X_raw, copy=False)
    X_scaled = scaler.transform(X_raw)

    # Take last seq_len rows as input sequence
    X_input = X_scaled[-seq_len:].reshape(1, seq_len, len(valid_cols))

    # Load and run model
    model = StockLSTM(meta["input_size"], meta["hidden_size"],
                      meta["num_layers"], meta["dropout"])
    model.load_state_dict(torch.load(pt_path, weights_only=True))
    model.eval()

    with torch.no_grad():
        logits = model(torch.FloatTensor(X_input))
        proba = torch.softmax(logits, dim=1).cpu().numpy()[0]

    pred_class = int(np.argmax(proba))
    confidence = float(proba[pred_class])

    # Horizon from target name (target_t3 → t3)
    horizon = target_col.replace("target_", "")

    last_date = df.iloc[-1]["trade_date"]
    date_str = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)

    return {
        "direction": "up" if pred_class == 1 else "down",
        "confidence": round(confidence, 4),
        "horizon": horizon,
        "model_type": "LSTM",
        "date": date_str,
        "seq_len": seq_len,
    }
