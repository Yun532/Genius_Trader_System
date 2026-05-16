"""Layer 0: Rule-based filter (free, instant).

Filters out clearly irrelevant news before sending to LLM.
Expected: ~25-35% rejection at zero cost.
"""

import json
import re
from typing import List, Tuple

from backend.database import get_conn

# Patterns for list articles
LIST_PATTERN = re.compile(
    r"^\d+\s+(best|top|worst|biggest|largest|most|highest|lowest)\b",
    re.IGNORECASE,
)
LIST_PATTERN_2 = re.compile(
    r"\b(top|best|worst)\s+\d+\b", re.IGNORECASE
)


def _check_article(
    title: str,
    description: str | None,
    tickers_json: str | None,
    symbol: str,
) -> Tuple[bool, str]:
    """Return (passed, reason). passed=True means article should proceed to Layer 1."""
    desc = (description or "").strip()

    # Rule 1: Empty description
    if not desc:
        return False, "empty_description"

    # Rule 2: Description too short (just a title repeat)
    if len(desc) < 30:
        return False, "description_too_short"

    # Rule 3: Market roundup — mentions >10 tickers and target not in title
    tickers: list = []
    if tickers_json:
        try:
            tickers = json.loads(tickers_json)
        except (json.JSONDecodeError, TypeError):
            pass
    if len(tickers) > 10 and symbol.upper() not in (title or "").upper():
        return False, "market_roundup"

    # Rule 4: List articles
    t = (title or "").strip()
    if LIST_PATTERN.search(t) or LIST_PATTERN_2.search(t):
        return False, "list_article"

    return True, "passed"


def run_layer0(symbol: str) -> dict:
    """Run Layer 0 on all news for a symbol. Returns stats."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT nr.id, nr.title, nr.description, nr.tickers_json
           FROM news_raw nr
           JOIN news_ticker nt ON nr.id = nt.news_id
           WHERE nt.symbol = ?
           AND nr.id NOT IN (
               SELECT news_id FROM layer0_results WHERE symbol = ?
           )""",
        (symbol, symbol),
    ).fetchall()

    stats = {"total": len(rows), "passed": 0, "filtered": 0}

    for row in rows:
        passed, reason = _check_article(
            row["title"], row["description"], row["tickers_json"], symbol
        )
        conn.execute(
            "INSERT OR IGNORE INTO layer0_results (news_id, symbol, passed, reason) VALUES (?, ?, ?, ?)",
            (row["id"], symbol, 1 if passed else 0, reason),
        )
        if passed:
            stats["passed"] += 1
        else:
            stats["filtered"] += 1

    conn.commit()
    conn.close()
    return stats
