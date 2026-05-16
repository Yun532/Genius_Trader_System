"""Expanding-window cross-validation backtest."""

import json
from pathlib import Path

import numpy as np
from xgboost import XGBClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

from backend.ml.features import build_features, build_features_multi, FEATURE_COLS

MODELS_DIR = Path(__file__).parent / "models"


def _run_cv(X, y, dates, n_folds, min_train, labels=None):
    """Core expanding-window CV logic. Returns folds + aggregate."""
    n = len(X)
    test_size = (n - min_train) // n_folds
    if test_size < 10:
        n_folds = max(1, (n - min_train) // 10)
        test_size = (n - min_train) // n_folds

    folds = []
    all_preds = []
    all_true = []
    all_dates = []
    all_labels = []

    for fold in range(n_folds):
        train_end = min_train + fold * test_size
        test_end = train_end + test_size if fold < n_folds - 1 else n

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]

        model = XGBClassifier(
            max_depth=4, n_estimators=300, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42,
        )
        model.fit(X_tr, y_tr, verbose=False)

        y_pred = model.predict(X_te)

        acc = accuracy_score(y_te, y_pred)
        baseline = max(y_te.mean(), 1 - y_te.mean())

        folds.append({
            "fold": fold + 1,
            "train_size": int(train_end),
            "test_size": int(test_end - train_end),
            "test_start": dates[train_end],
            "test_end": dates[test_end - 1],
            "accuracy": round(acc, 4),
            "baseline": round(baseline, 4),
            "precision": round(precision_score(y_te, y_pred, zero_division=0), 4),
            "recall": round(recall_score(y_te, y_pred, zero_division=0), 4),
            "f1": round(f1_score(y_te, y_pred, zero_division=0), 4),
        })

        for i in range(len(y_te)):
            all_preds.append(int(y_pred[i]))
            all_true.append(int(y_te[i]))
            all_dates.append(dates[train_end + i])
            if labels is not None:
                all_labels.append(labels[train_end + i])

    all_true_arr = np.array(all_true)
    all_preds_arr = np.array(all_preds)

    return folds, all_preds, all_true, all_dates, all_labels, all_true_arr, all_preds_arr


def run_backtest(symbol: str, horizon: str = "t1", n_folds: int = 5, min_train: int = 200) -> dict:
    """Expanding-window CV for a single ticker. Returns per-fold and aggregate metrics."""
    target_col = f"target_{horizon}"

    df = build_features(symbol)
    if df.empty:
        return {"error": f"No data for {symbol}"}

    df = df.dropna(subset=[target_col]).reset_index(drop=True)
    n = len(df)

    if n < min_train + 20:
        return {"error": f"Too few rows ({n}) for backtest"}

    X = df[FEATURE_COLS].values
    y = df[target_col].values
    dates = df["trade_date"].dt.strftime("%Y-%m-%d").tolist()

    folds, all_preds, all_true, all_dates, _, all_true_arr, all_preds_arr = _run_cv(
        X, y, dates, n_folds, min_train
    )

    overall_acc = accuracy_score(all_true_arr, all_preds_arr)
    overall_baseline = max(all_true_arr.mean(), 1 - all_true_arr.mean())

    result = {
        "symbol": symbol,
        "horizon": horizon,
        "n_folds": len(folds),
        "total_predictions": len(all_true),
        "overall_accuracy": round(overall_acc, 4),
        "overall_baseline": round(overall_baseline, 4),
        "overall_precision": round(precision_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "overall_recall": round(recall_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "overall_f1": round(f1_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "folds": folds,
        "daily_predictions": [
            {"date": d, "predicted": p, "actual": a}
            for d, p, a in zip(all_dates, all_preds, all_true)
        ],
    }

    MODELS_DIR.mkdir(exist_ok=True)
    out_path = MODELS_DIR / f"{symbol}_{horizon}_backtest.json"
    out_path.write_text(json.dumps(result, indent=2))

    return result


def run_backtest_unified(horizon: str = "t1", n_folds: int = 5, min_train: int = 800,
                         symbols: list[str] | None = None) -> dict:
    """Expanding-window CV on combined multi-ticker data."""
    target_col = f"target_{horizon}"

    df = build_features_multi(symbols)
    if df.empty:
        return {"error": "No combined data"}

    # Sort by date (mixing tickers chronologically)
    df = df.sort_values("trade_date").reset_index(drop=True)
    df = df.dropna(subset=[target_col]).reset_index(drop=True)
    n = len(df)

    if n < min_train + 50:
        return {"error": f"Too few rows ({n}) for unified backtest"}

    X = df[FEATURE_COLS].values
    y = df[target_col].values
    dates = df["trade_date"].dt.strftime("%Y-%m-%d").tolist()
    syms = df["symbol"].tolist()

    folds, all_preds, all_true, all_dates, all_labels, all_true_arr, all_preds_arr = _run_cv(
        X, y, dates, n_folds, min_train, labels=syms
    )

    overall_acc = accuracy_score(all_true_arr, all_preds_arr)
    overall_baseline = max(all_true_arr.mean(), 1 - all_true_arr.mean())

    # Per-ticker breakdown
    per_ticker = {}
    for i in range(len(all_true)):
        sym = all_labels[i]
        if sym not in per_ticker:
            per_ticker[sym] = {"true": [], "pred": []}
        per_ticker[sym]["true"].append(all_true[i])
        per_ticker[sym]["pred"].append(all_preds[i])

    ticker_metrics = {}
    for sym, data in sorted(per_ticker.items()):
        t = np.array(data["true"])
        p = np.array(data["pred"])
        ticker_metrics[sym] = {
            "n": len(t),
            "accuracy": round(accuracy_score(t, p), 4),
            "baseline": round(max(t.mean(), 1 - t.mean()), 4),
            "precision": round(precision_score(t, p, zero_division=0), 4),
            "recall": round(recall_score(t, p, zero_division=0), 4),
            "f1": round(f1_score(t, p, zero_division=0), 4),
        }

    result = {
        "symbol": "UNIFIED",
        "horizon": horizon,
        "tickers": sorted(set(syms)),
        "n_folds": len(folds),
        "total_predictions": len(all_true),
        "overall_accuracy": round(overall_acc, 4),
        "overall_baseline": round(overall_baseline, 4),
        "overall_precision": round(precision_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "overall_recall": round(recall_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "overall_f1": round(f1_score(all_true_arr, all_preds_arr, zero_division=0), 4),
        "per_ticker": ticker_metrics,
        "folds": folds,
    }

    MODELS_DIR.mkdir(exist_ok=True)
    out_path = MODELS_DIR / f"UNIFIED_{horizon}_backtest.json"
    out_path.write_text(json.dumps(result, indent=2))

    return result
