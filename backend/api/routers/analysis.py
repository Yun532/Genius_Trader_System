from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.ashare.symbol import normalize
from backend.analysis_cache import get_analysis_cache, llm_date_policy, stable_hash, store_analysis_cache
from backend.config import settings
from backend.database import get_conn
from backend.llm import analyze_price_range
from backend.pipeline.layer2 import analyze_article, generate_story
from backend.pipeline.similarity import find_similar

router = APIRouter()


class DeepAnalysisRequest(BaseModel):
    news_id: str
    symbol: str


class RangeAnalysisRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    question: Optional[str] = None
    use_llm: Optional[bool] = None
    force_llm: bool = False
    refresh_cache: bool = False


class SimilarRequest(BaseModel):
    news_id: str
    symbol: str
    top_k: Optional[int] = 20


class StoryRequest(BaseModel):
    symbol: str


def _norm(symbol: str) -> str:
    s = symbol.strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if s.lower().startswith(("sh", "sz", "bj")) or (len(digits) == 6 and s == digits):
        return normalize(s)
    return s.upper()


def _ticker_name(symbol: str) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT name FROM tickers WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    name = (row["name"] if row else None) or ""
    if name and name.lower() != symbol.lower():
        return name
    return None


def _display_symbol(symbol: str, name: Optional[str] = None) -> str:
    if name and name.lower() != symbol.lower():
        return f"{name}（{symbol}）"
    return symbol


@router.post("/deep")
def deep_analysis(req: DeepAnalysisRequest):
    return analyze_article(req.news_id, _norm(req.symbol))


@router.post("/story")
def create_story(req: StoryRequest):
    symbol = _norm(req.symbol)
    conn = get_conn()
    ohlc_rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM ohlc WHERE symbol = ? ORDER BY date ASC",
        (symbol,),
    ).fetchall()
    event_rows = conn.execute(
        """SELECT na.trade_date, l1.chinese_summary
           FROM news_aligned na
           JOIN layer1_results l1 ON na.news_id = l1.news_id AND na.symbol = l1.symbol
           WHERE na.symbol = ? AND l1.relevance = 'relevant'
           ORDER BY na.trade_date ASC""",
        (symbol,),
    ).fetchall()
    conn.close()

    events_by_day: dict[str, list[str]] = {}
    for row in event_rows:
        events_by_day.setdefault(row["trade_date"], []).append(row["chinese_summary"] or "")

    lines = ["date,open,high,low,close,volume,events"]
    for row in ohlc_rows:
        events = "; ".join(events_by_day.get(row["date"], []))
        lines.append(
            f"{row['date']},{row['open']},{row['high']},{row['low']},{row['close']},{row['volume']},\"{events}\""
        )
    return {"story": generate_story(symbol, "\n".join(lines))}


@router.post("/range")
def range_analysis(req: RangeAnalysisRequest):
    return analyze_symbol_range(
        _norm(req.symbol),
        req.start_date,
        req.end_date,
        req.question,
        use_llm=req.use_llm,
        force_llm=req.force_llm,
        refresh_cache=req.refresh_cache,
    )


@router.post("/range-local")
def range_analysis_local(req: RangeAnalysisRequest):
    result = _range_analysis_local(_norm(req.symbol), req.start_date, req.end_date, req.question)
    result.pop("_events_context", None)
    return result


def analyze_symbol_range(
    symbol: str,
    start_date: str,
    end_date: str,
    question: Optional[str] = None,
    *,
    use_llm: Optional[bool] = None,
    force_llm: bool = False,
    refresh_cache: bool = False,
):
    local = _range_analysis_local(symbol, start_date, end_date, question)
    if local.get("error"):
        return local

    event_context = local.pop("_events_context", "")
    policy = llm_date_policy(end_date, use_llm=use_llm, force_llm=force_llm)
    cache_parts = {
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "question": question or "",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "fallback_provider": settings.llm_fallback_provider,
        "fallback_model": settings.llm_fallback_model,
        "analysis_mode": settings.llm_analysis_mode,
        "context_hash": stable_hash(
            {
                "open": local.get("open_price"),
                "close": local.get("close_price"),
                "price_change_pct": local.get("price_change_pct"),
                "news_count": local.get("news_count"),
                "trading_days": local.get("trading_days"),
                "events_context": event_context,
            }
        ),
    }
    cached = None if refresh_cache else get_analysis_cache("range_analysis", cache_parts)
    if cached:
        local["analysis"] = cached.get("analysis") or local["analysis"]
        local["llm_used"] = bool(cached.get("llm_used"))
        local["llm_error"] = cached.get("llm_error")
        local["llm_policy"] = {**policy, "cache_hit": True}
        local["llm_cache"] = cached.get("cache")
        return local
    if not policy["use_llm"]:
        local["llm_used"] = False
        local["llm_policy"] = {**policy, "cache_hit": False}
        local["llm_cache"] = {"hit": False, "type": "range_analysis"}
        return local
    display_name = local.get("display_name") or symbol
    price_summary = (
        f"开盘 {local['open_price']:.2f}，收盘 {local['close_price']:.2f}，"
        f"最高 {local['high_price']:.2f}，最低 {local['low_price']:.2f}，"
        f"区间涨跌幅 {local['price_change_pct']:+.2f}%，"
        f"交易日 {local['trading_days']}，事件 {local['news_count']} 条。"
    )
    try:
        llm_result = analyze_price_range(
            symbol=display_name,
            start_date=start_date,
            end_date=end_date,
            price_summary=price_summary,
            event_context=event_context,
            question=question,
        )
        if llm_result:
            local["analysis"] = {
                "summary": llm_result.get("summary") or local["analysis"]["summary"],
                "key_events": llm_result.get("key_events") or local["analysis"]["key_events"],
                "bullish_factors": llm_result.get("bullish_factors") or local["analysis"]["bullish_factors"],
                "bearish_factors": llm_result.get("bearish_factors") or local["analysis"]["bearish_factors"],
                "trend_analysis": llm_result.get("trend_analysis") or local["analysis"]["trend_analysis"],
                "model_consensus": llm_result.get("model_consensus"),
                "model_disagreements": llm_result.get("model_disagreements") or [],
            }
            if llm_result.get("model_reviews"):
                local["analysis"]["model_reviews"] = llm_result.get("model_reviews")
            if llm_result.get("analysis_mode"):
                local["analysis"]["analysis_mode"] = llm_result.get("analysis_mode")
            if llm_result.get("reviewer_provider"):
                local["analysis"]["reviewer_provider"] = llm_result.get("reviewer_provider")
            local["llm_used"] = True
            store_analysis_cache(
                "range_analysis",
                cache_parts,
                {"analysis": local["analysis"], "llm_error": None},
                ttl_hours=settings.llm_range_analysis_cache_ttl_hours,
                llm_used=True,
                meta={"symbol": symbol, "start_date": start_date, "end_date": end_date},
            )
    except Exception as exc:
        local["llm_used"] = False
        local["llm_error"] = str(exc)
    local["llm_policy"] = {**policy, "cache_hit": False}
    local["llm_cache"] = {"hit": False, "type": "range_analysis"}
    return local


def _range_analysis_local(symbol: str, start_date: str, end_date: str, question: Optional[str] = None):
    name = _ticker_name(symbol)
    display_name = _display_symbol(symbol, name)
    conn = get_conn()
    ohlc_rows = conn.execute(
        """SELECT date, open, high, low, close, volume
           FROM ohlc
           WHERE symbol = ? AND date >= ? AND date <= ?
           ORDER BY date ASC""",
        (symbol, start_date, end_date),
    ).fetchall()

    if not ohlc_rows:
        conn.close()
        return {"error": "No OHLC data for this range"}

    open_price = ohlc_rows[0]["open"]
    close_price = ohlc_rows[-1]["close"]
    high_price = max(row["high"] for row in ohlc_rows)
    low_price = min(row["low"] for row in ohlc_rows)
    price_change_pct = round((close_price - open_price) / open_price * 100, 2)

    event_rows = conn.execute(
        """SELECT nr.title, nr.news_type, l1.sentiment, l1.chinese_summary,
                  l1.reason_growth, l1.reason_decrease, na.trade_date, na.ret_t0
           FROM news_aligned na
           JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = na.symbol
           JOIN news_raw nr ON na.news_id = nr.id
           WHERE na.symbol = ? AND na.trade_date >= ? AND na.trade_date <= ?
             AND l1.relevance IN ('high', 'medium', 'relevant')
           ORDER BY ABS(COALESCE(na.ret_t0, 0)) DESC
           LIMIT 50""",
        (symbol, start_date, end_date),
    ).fetchall()
    conn.close()

    positive = [row for row in event_rows if row["sentiment"] == "positive"]
    negative = [row for row in event_rows if row["sentiment"] == "negative"]
    direction = "上涨" if price_change_pct > 0 else "下跌" if price_change_pct < 0 else "横盘"
    summary = (
        f"{display_name} 在 {start_date} 至 {end_date} {direction} {abs(price_change_pct):.2f}%，"
        f"区间内匹配到 {len(event_rows)} 条事件，其中利好 {len(positive)} 条、利空 {len(negative)} 条。"
    )

    key_events = []
    for row in event_rows[:8]:
        ret = f" 当日{row['ret_t0'] * 100:+.1f}%" if row["ret_t0"] is not None else ""
        key_events.append(f"[{row['trade_date']}] [{row['news_type'] or 'event'}] {row['title']}{ret}")

    bullish = [
        row["reason_growth"] or row["chinese_summary"] or row["title"]
        for row in positive[:5]
        if row["reason_growth"] or row["chinese_summary"] or row["title"]
    ]
    bearish = [
        row["reason_decrease"] or row["chinese_summary"] or row["title"]
        for row in negative[:5]
        if row["reason_decrease"] or row["chinese_summary"] or row["title"]
    ]

    if len(ohlc_rows) >= 3:
        mid = len(ohlc_rows) // 2
        first_half = (ohlc_rows[mid]["close"] - ohlc_rows[0]["open"]) / ohlc_rows[0]["open"] * 100
        second_half = (ohlc_rows[-1]["close"] - ohlc_rows[mid]["open"]) / ohlc_rows[mid]["open"] * 100
        first_direction = "上涨" if first_half > 0 else "下跌"
        second_direction = "上涨" if second_half > 0 else "下跌"
        trend = (
            f"前半段{first_direction} {abs(first_half):.1f}%，"
            f"后半段{second_direction} {abs(second_half):.1f}%。"
            f"区间最高 {high_price:.2f}，最低 {low_price:.2f}，振幅 {(high_price - low_price) / low_price * 100:.1f}%。"
        )
    else:
        trend = f"区间较短，累计涨跌幅 {price_change_pct:+.2f}%。"

    events_context = "\n".join(
        f"{i + 1}. [{row['trade_date']}] [{row['news_type'] or 'event'}] {row['title']} - {row['chinese_summary'] or ''}"
        for i, row in enumerate(event_rows[:30])
    )

    return {
        "symbol": symbol,
        "name": name,
        "display_name": display_name,
        "start_date": start_date,
        "end_date": end_date,
        "price_change_pct": price_change_pct,
        "open_price": open_price,
        "close_price": close_price,
        "high_price": high_price,
        "low_price": low_price,
        "news_count": len(event_rows),
        "trading_days": len(ohlc_rows),
        "question": question,
        "_events_context": events_context,
        "analysis": {
            "summary": summary,
            "key_events": key_events,
            "bullish_factors": bullish,
            "bearish_factors": bearish,
            "trend_analysis": trend,
        },
    }


@router.post("/similar")
def similar_news(req: SimilarRequest):
    return find_similar(req.news_id, _norm(req.symbol), req.top_k or 20)
