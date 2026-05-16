"""One-time migration: import existing CSV/JSONL/JSON data into SQLite."""

import csv
import json
import re
from pathlib import Path

from backend.database import get_conn, init_db
from backend.config import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"

# Map filename patterns to tickers
OHLC_FILES = {
    "BABA": "ohlc_BABA_20201031_20251104.csv",
    "TSLA": "ohlc_TSLA_20231103_20251104.csv",
    "AAPL": "ohlc_AAPL_20231103_20251104.csv",
    "NVDA": "ohlc_NVDA_20231103_20251104.csv",
    "GLD": "ohlc_GLD_20231104_20251105.csv",
}

NEWS_FILES = {
    "BABA": "news_BABA_20201031_202511042backup.jsonl",
    "TSLA": "news_TSLA_20231103_20251104.jsonl",
    "AAPL": "news_AAPL_20231103_20251104.jsonl",
    "NVDA": "news_NVDA_20231103_20251104.jsonl",
    "GLD": "news_GLD_20231104_20251105.jsonl",
}

TICKER_NAMES = {
    "BABA": "Alibaba Group",
    "TSLA": "Tesla Inc",
    "AAPL": "Apple Inc",
    "NVDA": "NVIDIA Corp",
    "GLD": "SPDR Gold Shares",
}

OUTPUT_DIR = DATA_DIR / "output"


def migrate_tickers(conn):
    print("Migrating tickers...")
    for symbol, name in TICKER_NAMES.items():
        conn.execute(
            "INSERT OR IGNORE INTO tickers (symbol, name) VALUES (?, ?)",
            (symbol, name),
        )
    conn.commit()
    print(f"  {len(TICKER_NAMES)} tickers registered")


def migrate_ohlc(conn):
    print("Migrating OHLC data...")
    total = 0
    for symbol, filename in OHLC_FILES.items():
        path = DATA_DIR / filename
        if not path.exists():
            print(f"  SKIP {filename} (not found)")
            continue
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date = row.get("date", "").strip()
                if not date:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO ohlc
                       (symbol, date, open, high, low, close, volume, vwap, transactions)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        symbol,
                        date,
                        _float(row.get("open")),
                        _float(row.get("high")),
                        _float(row.get("low")),
                        _float(row.get("close")),
                        _float(row.get("volume")),
                        _float(row.get("vwap")),
                        _int(row.get("transactions")),
                    ),
                )
                count += 1
        conn.commit()
        total += count
        print(f"  {symbol}: {count} rows")
    print(f"  Total OHLC rows: {total}")


def migrate_news(conn):
    print("Migrating news data...")
    total = 0
    for symbol, filename in NEWS_FILES.items():
        path = DATA_DIR / filename
        if not path.exists():
            print(f"  SKIP {filename} (not found)")
            continue
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    art = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                news_id = art.get("id")
                if not news_id:
                    continue
                tickers = art.get("tickers") or []
                conn.execute(
                    """INSERT OR IGNORE INTO news_raw
                       (id, title, description, publisher, author,
                        published_utc, article_url, amp_url, tickers_json, insights_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        news_id,
                        art.get("title"),
                        art.get("description"),
                        art.get("publisher"),
                        art.get("author"),
                        art.get("published_utc"),
                        art.get("article_url"),
                        art.get("amp_url"),
                        json.dumps(tickers),
                        json.dumps(art.get("insights")) if art.get("insights") else None,
                    ),
                )
                # Junction table for each ticker mentioned
                for tk in tickers:
                    conn.execute(
                        "INSERT OR IGNORE INTO news_ticker (news_id, symbol) VALUES (?, ?)",
                        (news_id, tk),
                    )
                count += 1
        conn.commit()
        total += count
        print(f"  {symbol}: {count} articles")
    print(f"  Total news articles: {total}")


def migrate_parsed_output(conn):
    """Import already-parsed JSON files from data/output/ into layer1_results."""
    print("Migrating parsed output (layer1_results)...")
    if not OUTPUT_DIR.exists():
        print("  output/ directory not found, skipping")
        return
    count = 0
    for json_file in OUTPUT_DIR.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, ValueError):
            continue
        news_id = parsed.get("id")
        if not news_id:
            continue
        # These were all parsed for BABA originally
        symbol = "BABA"
        conn.execute(
            """INSERT OR IGNORE INTO layer1_results
               (news_id, symbol, relevance, key_discussion, chinese_summary,
                discussion, reason_growth, reason_decrease)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                news_id,
                symbol,
                parsed.get("relevance", ""),
                parsed.get("key_discussion", ""),
                parsed.get("chinese_key_discussion", ""),
                parsed.get("discussion", ""),
                parsed.get("reason_growth", ""),
                parsed.get("reason_decrease", ""),
            ),
        )
        count += 1
    conn.commit()
    print(f"  {count} parsed articles imported")


def _float(val):
    if val is None or val == "":
        return None
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _int(val):
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def run_migration():
    print("=== Stock News Migration ===")
    init_db()
    conn = get_conn()
    try:
        migrate_tickers(conn)
        migrate_ohlc(conn)
        migrate_news(conn)
        migrate_parsed_output(conn)
    finally:
        conn.close()
    print("=== Migration complete ===")


if __name__ == "__main__":
    run_migration()
