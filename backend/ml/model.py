"""XGBoost model training and prediction."""

import json
from pathlib import Path
from datetime import datetime

import numpy as np
import joblib
from xgboost import XGBClassifier

from backend.ml.features import build_features, build_features_multi, FEATURE_COLS

MODELS_DIR = Path(__file__).parent / "models"
MODELS_DIR.mkdir(exist_ok=True)


def train(symbol: str, horizon: str = "t1") -> dict:
    """Train XGBoost for a single symbol/horizon. Returns metrics dict."""
    target_col = f"target_{horizon}"

    df = build_features(symbol)
    if df.empty or len(df) < 60:
        return {"error": f"Not enough data for {symbol} ({len(df)} rows)"}

    # Drop rows where target is NaN (last few days)
    df = df.dropna(subset=[target_col]).reset_index(drop=True)

    X = df[FEATURE_COLS].values
    y = df[target_col].values
    dates = df["trade_date"].dt.strftime("%Y-%m-%d").tolist()

    # Time-series split: last 20% for test
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    model = XGBClassifier(
        max_depth=4,
        n_estimators=200,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    # Metrics
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    baseline = max(y_test.mean(), 1 - y_test.mean())

    # Feature importance
    importances = model.feature_importances_
    top_features = sorted(
        zip(FEATURE_COLS, importances.tolist()),
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    meta = {
        "symbol": symbol,
        "horizon": horizon,
        "accuracy": round(accuracy, 4),
        "baseline": round(baseline, 4),
        "precision": round(precision_score(y_test, y_pred, zero_division=0), 4),
        "recall": round(recall_score(y_test, y_pred, zero_division=0), 4),
        "f1": round(f1_score(y_test, y_pred, zero_division=0), 4),
        "train_size": split_idx,
        "test_size": len(y_test),
        "train_start": dates[0],
        "train_end": dates[split_idx - 1],
        "test_start": dates[split_idx],
        "test_end": dates[-1],
        "top_features": [{"name": n, "importance": round(v, 4)} for n, v in top_features],
        "trained_at": datetime.now().isoformat(),
    }

    # Save
    model_path = MODELS_DIR / f"{symbol}_{horizon}.joblib"
    meta_path = MODELS_DIR / f"{symbol}_{horizon}_meta.json"
    joblib.dump(model, model_path)
    meta_path.write_text(json.dumps(meta, indent=2))

    return meta


def train_unified(horizon: str = "t1", symbols: list[str] | None = None) -> dict:
    """Train a single XGBoost on ALL tickers combined. Returns metrics dict."""
    target_col = f"target_{horizon}"

    df = build_features_multi(symbols)
    if df.empty or len(df) < 100:
        return {"error": f"Not enough combined data ({len(df)} rows)"}

    df = df.dropna(subset=[target_col]).reset_index(drop=True)

    X = df[FEATURE_COLS].values
    y = df[target_col].values
    dates = df["trade_date"].dt.strftime("%Y-%m-%d").tolist()
    syms = df["symbol"].tolist()

    # Time-series split: sort by date, last 20% for test
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    model = XGBClassifier(
        max_depth=4,
        n_estimators=300,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    baseline = max(y_test.mean(), 1 - y_test.mean())

    importances = model.feature_importances_
    top_features = sorted(
        zip(FEATURE_COLS, importances.tolist()),
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    meta = {
        "symbol": "UNIFIED",
        "horizon": horizon,
        "accuracy": round(accuracy, 4),
        "baseline": round(baseline, 4),
        "precision": round(precision_score(y_test, y_pred, zero_division=0), 4),
        "recall": round(recall_score(y_test, y_pred, zero_division=0), 4),
        "f1": round(f1_score(y_test, y_pred, zero_division=0), 4),
        "train_size": split_idx,
        "test_size": len(y_test),
        "train_start": dates[0],
        "train_end": dates[split_idx - 1],
        "test_start": dates[split_idx],
        "test_end": dates[-1],
        "tickers": sorted(set(syms)),
        "top_features": [{"name": n, "importance": round(v, 4)} for n, v in top_features],
        "trained_at": datetime.now().isoformat(),
    }

    model_path = MODELS_DIR / f"UNIFIED_{horizon}.joblib"
    meta_path = MODELS_DIR / f"UNIFIED_{horizon}_meta.json"
    joblib.dump(model, model_path)
    meta_path.write_text(json.dumps(meta, indent=2))

    return meta


def predict(symbol: str, horizon: str = "t1") -> dict:
    """Load model and predict direction for the latest trading day."""
    model_path = MODELS_DIR / f"{symbol}_{horizon}.joblib"
    meta_path = MODELS_DIR / f"{symbol}_{horizon}_meta.json"

    # Fall back to unified model if per-ticker model missing
    if not model_path.exists():
        model_path = MODELS_DIR / f"UNIFIED_{horizon}.joblib"
        meta_path = MODELS_DIR / f"UNIFIED_{horizon}_meta.json"
    if not model_path.exists():
        return {"error": f"No model for {symbol}/{horizon}. Run training first."}

    model = joblib.load(model_path)
    meta = json.loads(meta_path.read_text())

    df = build_features(symbol)
    if df.empty:
        return {"error": f"No feature data for {symbol}"}

    # Use the last row (most recent trading day with complete features)
    last_row = df.iloc[-1]
    X = last_row[FEATURE_COLS].values.reshape(1, -1).astype(np.float64)

    proba = model.predict_proba(X)[0]
    pred_class = int(np.argmax(proba))
    confidence = float(proba[pred_class])

    # Top feature contributions for this prediction
    importances = model.feature_importances_
    feature_values = {col: float(last_row[col]) for col in FEATURE_COLS}
    top = sorted(
        zip(FEATURE_COLS, importances.tolist()),
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    return {
        "symbol": symbol,
        "horizon": horizon,
        "direction": "up" if pred_class == 1 else "down",
        "confidence": round(confidence, 4),
        "date": str(last_row["trade_date"].date()),
        "top_features": [
            {"name": n, "value": round(feature_values[n], 4), "importance": round(imp, 4)}
            for n, imp in top
        ],
        "model_accuracy": meta["accuracy"],
        "baseline_accuracy": meta["baseline"],
    }
