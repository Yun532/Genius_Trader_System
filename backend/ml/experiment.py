"""Comparative experiment: test multiple feature sets, models, and targets."""

import numpy as np
from xgboost import XGBClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

from backend.ml.features_v2 import (
    build_features_v2,
    FEATURE_COLS,
    FEATURE_COLS_V2_MARKET,
    FEATURE_COLS_V2_CANDLE,
    get_feature_cols_v2_full,
)


def _expanding_cv(X, y, n_folds=5, min_train=200, model_cls=None, model_kwargs=None):
    """Run expanding-window CV and return aggregate metrics."""
    n = len(X)
    if n < min_train + 20:
        return None

    test_size = (n - min_train) // n_folds
    if test_size < 10:
        n_folds = max(1, (n - min_train) // 10)
        test_size = (n - min_train) // n_folds

    all_true, all_pred = [], []

    for fold in range(n_folds):
        train_end = min_train + fold * test_size
        test_end = train_end + test_size if fold < n_folds - 1 else n

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]

        if model_cls is None:
            model = XGBClassifier(
                max_depth=4, n_estimators=200, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                eval_metric="logloss", random_state=42,
            )
        else:
            model = model_cls(**(model_kwargs or {}))

        # Handle NaN
        X_tr = np.nan_to_num(X_tr, nan=0.0)
        X_te = np.nan_to_num(X_te, nan=0.0)

        model.fit(X_tr, y_tr)
        y_p = model.predict(X_te)
        all_true.extend(y_te.tolist())
        all_pred.extend(y_p.tolist())

    t = np.array(all_true)
    p = np.array(all_pred)
    acc = accuracy_score(t, p)
    base = max(t.mean(), 1 - t.mean())

    return {
        "n": len(t),
        "accuracy": round(acc, 4),
        "baseline": round(base, 4),
        "lift": round((acc - base) * 100, 1),
        "precision": round(precision_score(t, p, zero_division=0), 4),
        "recall": round(recall_score(t, p, zero_division=0), 4),
        "f1": round(f1_score(t, p, zero_division=0), 4),
    }


def run_experiment(symbol: str):
    """Run all experiment combinations for a single ticker."""
    print(f"\n{'='*60}")
    print(f"  {symbol} — Comparative Experiment")
    print(f"{'='*60}")

    df = build_features_v2(symbol)
    if df.empty or len(df) < 250:
        print(f"  Not enough data: {len(df)} rows")
        return

    full_cols = get_feature_cols_v2_full(df)

    # Define experiments
    feature_sets = {
        "v1_base":      FEATURE_COLS,
        "v2_market":    FEATURE_COLS_V2_MARKET,
        "v2_candle":    FEATURE_COLS_V2_CANDLE,
        "v2_full":      full_cols,
    }

    targets = {
        "direction_t1":     "target_t1",
        "direction_t2":     "target_t2",
        "direction_t3":     "target_t3",
        "big_move_1pct":    "target_big1_t1",
        "big_move_2pct":    "target_big2_t1",
        "up_big_1pct":      "target_up_big_t1",
    }

    models = {
        "XGBoost":  (None, None),
        "LogReg":   (LogisticRegression, {"max_iter": 1000, "C": 0.1, "random_state": 42}),
        "RF":       (RandomForestClassifier, {"n_estimators": 200, "max_depth": 6, "random_state": 42}),
    }

    # Run combinations
    results = []
    for target_name, target_col in targets.items():
        sub = df.dropna(subset=[target_col]).reset_index(drop=True)
        if len(sub) < 250:
            continue
        y = sub[target_col].values

        for feat_name, feat_cols in feature_sets.items():
            # Only use columns that exist
            valid_cols = [c for c in feat_cols if c in sub.columns]
            X = sub[valid_cols].values.astype(np.float64)

            for model_name, (model_cls, model_kw) in models.items():
                r = _expanding_cv(X, y, n_folds=5, min_train=200,
                                  model_cls=model_cls, model_kwargs=model_kw)
                if r is None:
                    continue
                results.append({
                    "target": target_name,
                    "features": feat_name,
                    "model": model_name,
                    **r,
                })

    # Print results sorted by lift
    results.sort(key=lambda x: x["lift"], reverse=True)

    print(f"\n{'Target':<18} {'Features':<12} {'Model':<8} {'Acc':>6} {'Base':>6} {'Lift':>6} {'Prec':>6} {'Rec':>6} {'F1':>6}")
    print("-" * 82)
    for r in results:
        lift_str = f"{r['lift']:+.1f}pp"
        print(f"{r['target']:<18} {r['features']:<12} {r['model']:<8} "
              f"{r['accuracy']*100:5.1f}% {r['baseline']*100:5.1f}% {lift_str:>6} "
              f"{r['precision']*100:5.1f}% {r['recall']*100:5.1f}% {r['f1']*100:5.1f}%")

    # Top 5
    print(f"\n  Top 5 by lift:")
    for i, r in enumerate(results[:5]):
        print(f"  {i+1}. {r['target']} + {r['features']} + {r['model']}: "
              f"acc={r['accuracy']*100:.1f}% lift={r['lift']:+.1f}pp f1={r['f1']*100:.1f}%")

    return results


if __name__ == "__main__":
    import sys
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "AAPL", "TSLA"]
    for sym in tickers:
        run_experiment(sym)
