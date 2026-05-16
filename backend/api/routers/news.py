from typing import Optional

from fastapi import APIRouter, Query

from backend.ashare.symbol import normalize
from backend.database import get_conn

router = APIRouter()


def _norm(symbol: str) -> str:
    s = symbol.strip()
    if s.lower().startswith(("sh", "sz", "bj")) or (len(s) == 6 and s.isdigit()):
        return normalize(s)
    return s.upper()


def _event_rows(symbol: str, date: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None):
    conn = get_conn()
    query = """SELECT na.news_id, na.trade_date, na.published_utc,
                      na.ret_t0, na.ret_t1, na.ret_t3, na.ret_t5, na.ret_t10,
                      nr.title, nr.description, nr.publisher, nr.article_url, nr.image_url, nr.news_type,
                      l1.relevance, l1.key_discussion, l1.chinese_summary,
                      l1.sentiment, l1.reason_growth, l1.reason_decrease
               FROM news_aligned na
               JOIN news_raw nr ON na.news_id = nr.id
               LEFT JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = ?
               WHERE na.symbol = ?"""
    params: list = [symbol, symbol]
    if date:
        query += " AND na.trade_date = ?"
        params.append(date)
    if start:
        query += " AND na.trade_date >= ?"
        params.append(start)
    if end:
        query += " AND na.trade_date <= ?"
        params.append(end)
    query += " ORDER BY na.trade_date DESC, na.published_utc DESC LIMIT 500"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


@router.get("/{symbol}")
def get_news_for_date(symbol: str, date: Optional[str] = None):
    return _event_rows(_norm(symbol), date=date)


@router.get("/{symbol}/range")
def get_news_for_range(
    symbol: str,
    start: str = Query(..., description="Start date YYYY-MM-DD"),
    end: str = Query(..., description="End date YYYY-MM-DD"),
):
    articles = _event_rows(_norm(symbol), start=start, end=end)
    top_bullish = sorted(
        [a for a in articles if a.get("sentiment") == "positive" and a.get("ret_t0") is not None],
        key=lambda a: a["ret_t0"],
        reverse=True,
    )[:5]
    top_bearish = sorted(
        [a for a in articles if a.get("sentiment") == "negative" and a.get("ret_t0") is not None],
        key=lambda a: a["ret_t0"],
    )[:5]
    return {"total": len(articles), "date_range": [start, end], "articles": articles, "top_bullish": top_bullish, "top_bearish": top_bearish}


@router.get("/{symbol}/particles")
def get_news_particles(symbol: str):
    conn = get_conn()
    norm = _norm(symbol)
    rows = conn.execute(
        """SELECT na.news_id, na.trade_date, na.ret_t1,
                  nr.title, nr.news_type, l1.sentiment, l1.relevance
           FROM news_aligned na
           JOIN news_raw nr ON na.news_id = nr.id
           LEFT JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = ?
           WHERE na.symbol = ?
           ORDER BY na.trade_date ASC, l1.relevance DESC""",
        (norm, norm),
    ).fetchall()
    conn.close()
    return [
        {
            "id": row["news_id"],
            "d": row["trade_date"],
            "s": row["sentiment"],
            "r": row["relevance"],
            "t": (row["title"] or "")[:80],
            "rt1": row["ret_t1"],
            "type": row["news_type"],
        }
        for row in rows
    ]


@router.get("/{symbol}/categories")
def get_news_categories(symbol: str, date: Optional[str] = None):
    conn = get_conn()
    norm = _norm(symbol)
    params: list = [norm, norm]
    date_filter = ""
    if date:
        date_filter = " AND na.trade_date = ?"
        params.append(date)
    rows = conn.execute(
        """SELECT na.news_id, nr.title, nr.news_type,
                  l1.key_discussion, l1.reason_growth, l1.reason_decrease, l1.sentiment
           FROM news_aligned na
           JOIN news_raw nr ON na.news_id = nr.id
           LEFT JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = ?
           WHERE na.symbol = ?
           """ + date_filter + """
           ORDER BY na.trade_date DESC""",
        params,
    ).fetchall()
    conn.close()

    category_keywords = {
        "news": ["新闻", "市场", "股价", "资金", "行业", "政策"],
        "announcement": ["公告", "减持", "增持", "回购", "并购", "重组", "问询", "处罚", "中标"],
        "financial_report": ["财报", "季报", "年报", "半年报", "营收", "净利润", "扣非", "现金流"],
        "policy": ["政策", "监管", "发改委", "证监会", "央行", "财政部"],
        "capital": ["龙虎榜", "北向", "融资", "融券", "资金"],
    }
    categories = {
        key: {"label": key, "count": 0, "article_ids": [], "positive_ids": [], "negative_ids": [], "neutral_ids": []}
        for key in category_keywords
    }

    for row in rows:
        text = " ".join(
            [
                row["news_type"] or "",
                row["title"] or "",
                row["key_discussion"] or "",
                row["reason_growth"] or "",
                row["reason_decrease"] or "",
            ]
        )
        sentiment = row["sentiment"] or "neutral"
        matched = set()
        if row["news_type"] in categories:
            matched.add(row["news_type"])
        for key, keywords in category_keywords.items():
            if any(keyword in text for keyword in keywords):
                matched.add(key)
        for key in matched:
            categories[key]["count"] += 1
            categories[key]["article_ids"].append(row["news_id"])
            if sentiment == "positive":
                categories[key]["positive_ids"].append(row["news_id"])
            elif sentiment == "negative":
                categories[key]["negative_ids"].append(row["news_id"])
            else:
                categories[key]["neutral_ids"].append(row["news_id"])

    return {"categories": categories, "total": len(rows)}


@router.get("/{symbol}/timeline")
def get_news_timeline(symbol: str):
    conn = get_conn()
    norm = _norm(symbol)
    rows = conn.execute(
        """SELECT trade_date, COUNT(*) as news_count,
                  SUM(CASE WHEN l1.relevance = 'relevant' THEN 1 ELSE 0 END) as relevant_count
           FROM news_aligned na
           LEFT JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = na.symbol
           WHERE na.symbol = ?
           GROUP BY trade_date
           ORDER BY trade_date ASC""",
        (norm,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
