"""Submit Layer 1 analysis to Anthropic Batch API for top N tickers.

Usage: python -m backend.batch_submit [--top 50]
"""

import json
import sys
import time
from typing import List, Dict, Any

import anthropic

from backend.config import settings
from backend.database import get_conn
from backend.pipeline.layer1 import (
    get_pending_articles, _build_batch_prompt, BATCH_SIZE, MODEL, MAX_OUTPUT_TOKENS
)


def get_top_tickers(n: int = 50) -> List[Dict[str, Any]]:
    """Get top N tickers by Layer 0 passed count, with pending articles."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT l0.symbol, t.name,
               sum(case when l0.passed=1 then 1 else 0 end) as passed
        FROM layer0_results l0
        JOIN tickers t ON l0.symbol = t.symbol
        GROUP BY l0.symbol
        ORDER BY passed DESC
        LIMIT ?
    """, (n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_batch_requests(
    symbols: List[str],
) -> tuple[list, dict]:
    """Build all batch requests for given symbols.

    Returns (requests_list, mapping_dict) where mapping_dict maps
    custom_id -> (symbol, [article_ids]).
    """
    all_requests = []
    mapping = {}  # custom_id -> (symbol, article_ids_list)

    for symbol in symbols:
        articles = get_pending_articles(symbol)
        if not articles:
            print(f"  {symbol}: no pending articles, skip")
            continue

        for chunk_idx in range(0, len(articles), BATCH_SIZE):
            chunk = articles[chunk_idx:chunk_idx + BATCH_SIZE]
            custom_id = f"{symbol}_{chunk_idx:05d}"

            prompt = _build_batch_prompt(symbol, chunk)
            article_ids = [a["id"] for a in chunk]

            all_requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
            })
            mapping[custom_id] = (symbol, article_ids)

        print(f"  {symbol}: {len(articles)} articles -> {len(range(0, len(articles), BATCH_SIZE))} requests")

    return all_requests, mapping


def submit_batch(requests_list: list, mapping: dict) -> str:
    """Submit to Anthropic Batch API and save mapping to database."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    print(f"\nSubmitting {len(requests_list)} requests to Batch API...")
    batch = client.messages.batches.create(requests=requests_list)
    batch_id = batch.id
    print(f"Batch ID: {batch_id}")
    print(f"Status: {batch.processing_status}")

    # Save batch job
    conn = get_conn()
    total_articles = sum(len(v[1]) for v in mapping.values())
    conn.execute(
        """INSERT OR REPLACE INTO batch_jobs
           (batch_id, symbol, status, total, created_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        (batch_id, "multi", batch.processing_status, total_articles),
    )

    # Save request mapping
    for custom_id, (symbol, article_ids) in mapping.items():
        conn.execute(
            """INSERT OR REPLACE INTO batch_request_map
               (batch_id, custom_id, symbol, article_ids)
               VALUES (?, ?, ?, ?)""",
            (batch_id, custom_id, symbol, json.dumps(article_ids)),
        )

    conn.commit()
    conn.close()

    return batch_id


def main():
    top_n = 50
    if len(sys.argv) > 1:
        for i, arg in enumerate(sys.argv):
            if arg == "--top" and i + 1 < len(sys.argv):
                top_n = int(sys.argv[i + 1])

    print(f"=== Layer 1 Batch API Submission (top {top_n} tickers) ===\n")

    # Get top tickers
    tickers = get_top_tickers(top_n)
    symbols = [t["symbol"] for t in tickers]

    total_pending = 0
    for t in tickers:
        total_pending += t["passed"]
    print(f"Top {len(tickers)} tickers, ~{total_pending} Layer0-passed articles")
    print(f"(Already processed by Layer1 will be excluded)\n")

    # Build requests
    print("Building batch requests...")
    requests_list, mapping = build_batch_requests(symbols)

    if not requests_list:
        print("No pending articles to process!")
        return

    total_articles = sum(len(v[1]) for v in mapping.values())

    # Cost estimate
    est_input_tokens = total_articles * 300
    est_output_tokens = total_articles * 80
    est_cost = (est_input_tokens / 1_000_000 * 0.5) + (est_output_tokens / 1_000_000 * 2.5)

    print(f"\n=== Summary ===")
    print(f"Tickers: {len(symbols)}")
    print(f"Total articles: {total_articles:,}")
    print(f"Batch requests: {len(requests_list)}")
    print(f"Estimated cost: ~${est_cost:.2f} (Batch API pricing)")
    print()

    # Submit
    batch_id = submit_batch(requests_list, mapping)

    print(f"\nBatch submitted! ID: {batch_id}")
    print(f"Check status: python -m backend.batch_collect {batch_id}")


if __name__ == "__main__":
    main()
