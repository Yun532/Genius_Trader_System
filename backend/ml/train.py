"""CLI entry point: python -m backend.ml.train [--symbol SYM] [--backtest] [--lstm]"""

import argparse
import time

from backend.database import get_conn
from backend.ml.model import train
from backend.ml.backtest import run_backtest

HORIZONS = ["t1", "t5"]

# Best LSTM configs per ticker (from experiments)
LSTM_CONFIGS = {
    "TSLA": {"target_col": "target_t3", "seq_len": 10, "exclude_neutral": False},
    # Add more tickers here as LSTM proves beneficial
}


def get_symbols() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM tickers WHERE last_ohlc_fetch IS NOT NULL ORDER BY symbol"
    ).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Train ML models")
    parser.add_argument("--symbol", type=str, help="Train only this ticker")
    parser.add_argument("--backtest", action="store_true", help="Run backtest after training")
    parser.add_argument("--lstm", action="store_true", help="Also train LSTM for configured tickers")
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else get_symbols()
    print(f"Training for {len(symbols)} ticker(s): {', '.join(symbols)}")

    t0 = time.time()
    for sym in symbols:
        for h in HORIZONS:
            result = train(sym, h)
            if "error" in result:
                print(f"  {sym}/{h}: {result['error']}")
            else:
                print(f"  {sym}/{h}: acc={result['accuracy']:.1%} baseline={result['baseline']:.1%} "
                      f"(train={result['train_size']}, test={result['test_size']})")

            if args.backtest and "error" not in result:
                bt = run_backtest(sym, h)
                if "error" in bt:
                    print(f"    backtest: {bt['error']}")
                else:
                    print(f"    backtest: {bt['n_folds']} folds, "
                          f"acc={bt['overall_accuracy']:.1%} baseline={bt['overall_baseline']:.1%}")

        # LSTM training for configured tickers
        if args.lstm and sym in LSTM_CONFIGS:
            from backend.ml.lstm_model import train_and_save_lstm
            cfg = LSTM_CONFIGS[sym]
            print(f"  {sym}/LSTM: training {cfg['target_col']} seq={cfg['seq_len']}...")
            result = train_and_save_lstm(sym, **cfg, epochs=50)
            if "error" in result:
                print(f"    LSTM: {result['error']}")
            else:
                print(f"    LSTM: saved ({result['train_size']} sequences)")

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
