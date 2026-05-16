"""Check and collect results from Anthropic Batch API.

Usage: python -m backend.batch_collect <batch_id>
"""

import json
import sys
import time

import anthropic

from backend.config import settings
from backend.database import get_conn


def check_status(batch_id: str) -> dict:
    """Check batch status."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    batch = client.messages.batches.retrieve(batch_id)

    conn = get_conn()
    conn.execute(
        "UPDATE batch_jobs SET status = ? WHERE batch_id = ?",
        (batch.processing_status, batch_id),
    )
    conn.commit()
    conn.close()

    return {
        "status": batch.processing_status,
        "processing": batch.request_counts.processing,
        "succeeded": batch.request_counts.succeeded,
        "errored": batch.request_counts.errored,
        "canceled": batch.request_counts.canceled,
        "expired": batch.request_counts.expired,
    }


def collect_results(batch_id: str) -> dict:
    """Collect results from a completed batch."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = get_conn()

    # Load mapping
    rows = conn.execute(
        "SELECT custom_id, symbol, article_ids FROM batch_request_map WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()

    mapping = {}
    for r in rows:
        mapping[r["custom_id"]] = {
            "symbol": r["symbol"],
            "article_ids": json.loads(r["article_ids"]),
        }

    stats = {"processed": 0, "relevant": 0, "irrelevant": 0, "errors": 0}

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        info = mapping.get(custom_id)
        if not info:
            stats["errors"] += 1
            continue

        symbol = info["symbol"]
        article_ids = info["article_ids"]

        if result.result.type != "succeeded":
            stats["errors"] += len(article_ids)
            continue

        message = result.result.message
        text = message.content[0].text if message.content else "[]"

        try:
            start = text.find("[")
            end = text.rfind("]") + 1
            if start < 0 or end <= start:
                stats["errors"] += len(article_ids)
                continue

            items = json.loads(text[start:end])

            for item in items:
                idx = item.get("i")
                if idx is None or idx >= len(article_ids):
                    stats["errors"] += 1
                    continue

                is_relevant = item.get("r") in ("y", "relevant")
                relevance = "relevant" if is_relevant else "irrelevant"
                raw_s = item.get("s", "0")
                sentiment = {"+": "positive", "-": "negative"}.get(raw_s, "neutral")

                conn.execute(
                    """INSERT OR REPLACE INTO layer1_results
                       (news_id, symbol, relevance, key_discussion, sentiment,
                        reason_growth, reason_decrease)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        article_ids[idx],
                        symbol,
                        relevance,
                        item.get("e", ""),
                        sentiment,
                        item.get("u", ""),
                        item.get("d", ""),
                    ),
                )
                stats["processed"] += 1
                if is_relevant:
                    stats["relevant"] += 1
                else:
                    stats["irrelevant"] += 1

        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Parse error for {custom_id}: {e}")
            stats["errors"] += len(article_ids)

    conn.execute(
        "UPDATE batch_jobs SET status = 'collected', completed = ?, finished_at = datetime('now') WHERE batch_id = ?",
        (stats["processed"], batch_id),
    )
    conn.commit()
    conn.close()

    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m backend.batch_collect <batch_id>")
        # If no batch_id, show all batch jobs
        conn = get_conn()
        jobs = conn.execute("SELECT * FROM batch_jobs ORDER BY created_at DESC").fetchall()
        conn.close()
        if jobs:
            print("\nExisting batch jobs:")
            for j in jobs:
                print(f"  {j['batch_id']}  status={j['status']}  total={j['total']}  created={j['created_at']}")
        return

    batch_id = sys.argv[1]

    print(f"Checking batch: {batch_id}")
    status = check_status(batch_id)
    print(f"Status: {status['status']}")
    print(f"  Succeeded: {status['succeeded']}")
    print(f"  Processing: {status['processing']}")
    print(f"  Errored: {status['errored']}")

    if status["status"] == "ended":
        print("\nBatch completed! Collecting results...")
        stats = collect_results(batch_id)
        print(f"\n=== Results ===")
        print(f"Processed: {stats['processed']}")
        print(f"Relevant: {stats['relevant']}")
        print(f"Irrelevant: {stats['irrelevant']}")
        print(f"Errors: {stats['errors']}")
    elif status["status"] == "in_progress":
        print("\nBatch still processing. Run this command again later.")
    else:
        print(f"\nBatch status: {status['status']}")


if __name__ == "__main__":
    main()
