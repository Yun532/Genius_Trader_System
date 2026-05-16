import json
import logging
import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from pydantic import BaseModel

from backend.ashare.client import (
    MARKET_INDEXES,
    fetch_analyst_ratings,
    fetch_announcements,
    fetch_financial_reports,
    fetch_industry_board_constituents,
    fetch_industry_board_ohlc,
    fetch_market_index_ohlc,
    fetch_news,
    fetch_northbound_flow,
    fetch_ohlc,
    fetch_stock_profile,
    get_stock_name,
    industry_board_name,
    search_tickers,
)
from backend.ashare.symbol import get_market, normalize
from backend.analysis_cache import get_analysis_cache, llm_date_policy, stable_hash, store_analysis_cache
from backend.config import settings
from backend.database import get_conn
from backend.llm import analyze_daily_reason, chat_json, configured as llm_configured, summarize_event
from backend.pipeline.alignment import align_news_for_symbol
from backend.web_search import discover_external_info, discover_macro_chain_info

logger = logging.getLogger(__name__)
router = APIRouter()


class AddTickerRequest(BaseModel):
    symbol: str
    name: Optional[str] = None


class AnalyzeRequest(BaseModel):
    start_date: str
    end_date: str
    question: Optional[str] = None
    use_llm: Optional[bool] = None
    force_llm: bool = False
    refresh_cache: bool = False


class SignalReferenceRequest(BaseModel):
    date: Optional[str] = None
    window_days: int = 30
    top_k: int = 10
    question: Optional[str] = None
    use_llm: Optional[bool] = None
    force_llm: bool = False
    refresh_cache: bool = False


class StockReportRequest(BaseModel):
    date: Optional[str] = None
    lookback_days: int = 180
    question: Optional[str] = None
    force_llm: bool = True
    refresh_cache: bool = False


def _source_result(
    status: str = "pending",
    count: int = 0,
    *,
    used_cache: bool = False,
    error: Optional[str] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> dict:
    return {
        "status": status,
        "count": count,
        "used_cache": used_cache,
        "error": error,
        "min_date": min_date,
        "max_date": max_date,
    }


def _bounds_from_rows(rows: list[dict], key: str) -> tuple[Optional[str], Optional[str]]:
    dates = sorted(str(row.get(key) or "") for row in rows if row.get(key))
    return (dates[0], dates[-1]) if dates else (None, None)


def _is_ashare(symbol: str) -> bool:
    s = symbol.strip().lower().replace(".", "")
    digits = "".join(c for c in s if c.isdigit())
    return s.startswith(("sh", "sz", "bj")) or (len(digits) == 6 and s == digits)


def _norm(symbol: str) -> str:
    return normalize(symbol) if _is_ashare(symbol) else symbol.upper()


def _display_symbol(symbol: str, name: Optional[str] = None) -> str:
    clean_name = (name or "").strip()
    if clean_name and clean_name.lower() != symbol.lower():
        return f"{clean_name}（{symbol}）"
    return symbol


def _get_ticker_name(symbol: str) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT name FROM tickers WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    current = (row["name"] if row else None) or ""
    if current and current.lower() != symbol.lower():
        return current

    try:
        fetched = get_stock_name(symbol)
    except Exception as exc:
        logger.info("Could not resolve stock name for %s: %s", symbol, exc)
        fetched = None
    if fetched and fetched.lower() != symbol.lower():
        conn = get_conn()
        conn.execute(
            """INSERT INTO tickers (symbol, name, market)
               VALUES (?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET name = excluded.name""",
            (symbol, fetched, get_market(symbol) if _is_ashare(symbol) else None),
        )
        conn.commit()
        conn.close()
        return fetched
    return current or None


def _apply_display_name(payload: dict) -> dict:
    symbol = payload.get("symbol")
    if not symbol:
        return payload
    name = payload.get("name") or _get_ticker_name(symbol)
    payload["name"] = name
    payload["display_name"] = _display_symbol(symbol, name)

    analysis = payload.get("analysis")
    if analysis and name and name.lower() != str(symbol).lower():
        display_label = _display_symbol(symbol, name)

        def replace_value(value):
            if isinstance(value, str):
                updated = value
                if display_label and display_label != symbol:
                    updated = updated.replace(display_label, name)
                updated = updated.replace(f"{name}\uff08{symbol}\uff09", name)
                updated = updated.replace(f"{name}({symbol})", name)
                return updated.replace(str(symbol), name)
            if isinstance(value, list):
                return [replace_value(item) for item in value]
            if isinstance(value, dict):
                return {key: replace_value(item) for key, item in value.items()}
            return value

        payload["analysis"] = replace_value(analysis)
    return payload


def _default_dates(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    end_date = datetime.now().date() if not end else datetime.fromisoformat(end).date()
    start_date = end_date - timedelta(days=2 * 366) if not start else datetime.fromisoformat(start).date()
    return start_date.isoformat(), end_date.isoformat()


def _classify_event(event: dict) -> dict:
    text = f"{event.get('title', '')} {event.get('summary', '')}"
    negative_words = ("减持", "亏损", "下滑", "处罚", "立案", "问询", "风险", "终止", "诉讼")
    positive_words = ("增长", "预增", "中标", "回购", "增持", "盈利", "突破", "签订", "并购")
    sentiment = "neutral"
    if any(word in text for word in negative_words):
        sentiment = "negative"
    elif any(word in text for word in positive_words):
        sentiment = "positive"
    return {
        "summary": event.get("summary") or event.get("title") or "",
        "sentiment": sentiment,
        "impact": event.get("impact") or ("high" if event.get("event_type") != "news" else "medium"),
        "reason_growth": "事件内容可能改善市场预期。" if sentiment == "positive" else "",
        "reason_decrease": "事件内容可能压制市场风险偏好。" if sentiment == "negative" else "",
    }


def _event_identity(event: dict) -> str:
    raw = f"{event.get('symbol')}:{event.get('event_type')}:{event.get('event_date')}:{event.get('title')}:{event.get('url', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _apply_financial_disclosure_dates(financial_reports: list[dict], announcements: list[dict]) -> list[dict]:
    """Use report announcements to place financial events on real disclosure dates when possible."""
    for report in financial_reports:
        metrics = report.get("metrics") or {}
        period = metrics.get("report_period") or report.get("event_date")
        if not period:
            continue

        year = str(period)[:4]
        suffix = str(period)[5:10]
        keywords = {
            "03-31": ("一季报", "第一季度报告", "一季度报告"),
            "06-30": ("半年报", "半年度报告", "中期报告"),
            "09-30": ("三季报", "第三季度报告", "三季度报告"),
            "12-31": ("年报", "年度报告", "年度报告摘要"),
        }.get(suffix, ("季报", "年报", "半年报"))

        matched = None
        for announcement in announcements:
            title = announcement.get("title") or ""
            if year in title and any(keyword in title for keyword in keywords):
                matched = announcement
                break

        raw = report.setdefault("raw", {})
        if matched:
            report["event_date"] = matched["event_date"]
            report["published_at"] = matched.get("published_at") or f"{matched['event_date']}T00:00:00"
            metrics["announcement_date"] = matched["event_date"]
            raw["date_confidence"] = "high"
            raw["matched_announcement_id"] = matched.get("id")
            report["id"] = _event_identity(report)
        else:
            raw["date_confidence"] = "low"
    return financial_reports


def _insert_ohlc(symbol: str, rows: list[dict]) -> None:
    conn = get_conn()
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO ohlc
               (symbol, date, open, high, low, close, volume, vwap,
                amount, turnover_rate, change_pct, amplitude,
                is_limit_up, is_limit_down, is_suspended)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["symbol"],
                row["date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
                row["vwap"],
                row.get("amount"),
                row.get("turnover_rate"),
                row.get("change_pct"),
                row.get("amplitude"),
                row.get("is_limit_up", 0),
                row.get("is_limit_down", 0),
                row.get("is_suspended", 0),
            ),
        )
    conn.execute("UPDATE tickers SET last_ohlc_fetch = ? WHERE symbol = ?", (datetime.now().date().isoformat(), symbol))
    conn.commit()
    conn.close()


def _insert_events(symbol: str, events: list[dict], *, use_llm: bool = False) -> dict:
    conn = get_conn()
    inserted = 0
    for event in events:
        event_id = event["id"]
        metrics = event.get("metrics") or {}
        if event["event_type"] == "financial_report":
            stale_rows = list(conn.execute(
                "SELECT id FROM market_events WHERE symbol = ? AND event_type = ? AND event_date = ? AND id <> ?",
                (symbol, event["event_type"], event["event_date"], event_id),
            ).fetchall())
            report_period = metrics.get("report_period")
            if report_period:
                stale_rows.extend(
                    conn.execute(
                        """SELECT id
                           FROM financial_reports
                           WHERE symbol = ? AND report_period = ? AND id <> ?""",
                        (symbol, report_period, event_id),
                    ).fetchall()
                )
            for stale in stale_rows:
                stale_id = stale["id"]
                conn.execute("DELETE FROM news_aligned WHERE news_id = ? AND symbol = ?", (stale_id, symbol))
                conn.execute("DELETE FROM layer1_results WHERE news_id = ? AND symbol = ?", (stale_id, symbol))
                conn.execute("DELETE FROM layer2_results WHERE news_id = ? AND symbol = ?", (stale_id, symbol))
                conn.execute("DELETE FROM news_ticker WHERE news_id = ? AND symbol = ?", (stale_id, symbol))
                conn.execute("DELETE FROM news_raw WHERE id = ?", (stale_id,))
                conn.execute("DELETE FROM market_events WHERE id = ?", (stale_id,))
                conn.execute("DELETE FROM financial_reports WHERE id = ?", (stale_id,))
        enriched = _classify_event(event)
        if use_llm:
            try:
                llm_result = summarize_event(event)
                if llm_result:
                    enriched.update({k: v for k, v in llm_result.items() if v is not None})
            except Exception as exc:
                logger.info("LLM event summary skipped for %s: %s", event_id, exc)

        raw = event.get("raw") or {}
        conn.execute(
            """INSERT OR REPLACE INTO market_events
               (id, symbol, event_type, event_date, published_at, title, summary,
                source, url, sentiment, impact, metrics_json, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                symbol,
                event["event_type"],
                event["event_date"],
                event.get("published_at"),
                event["title"],
                enriched.get("summary"),
                event.get("source"),
                event.get("url"),
                enriched.get("sentiment"),
                enriched.get("impact"),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO news_raw
               (id, title, description, publisher, author, published_utc,
                article_url, tickers_json, news_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event["title"],
                enriched.get("summary"),
                event.get("source"),
                "",
                event.get("published_at") or f"{event['event_date']}T00:00:00",
                event.get("url"),
                json.dumps([symbol]),
                event["event_type"],
            ),
        )
        conn.execute("INSERT OR IGNORE INTO news_ticker (news_id, symbol) VALUES (?, ?)", (event_id, symbol))
        conn.execute(
            """INSERT OR REPLACE INTO layer1_results
               (news_id, symbol, relevance, key_discussion, chinese_summary,
                sentiment, discussion, reason_growth, reason_decrease)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                symbol,
                "relevant",
                enriched.get("summary"),
                enriched.get("summary"),
                enriched.get("sentiment"),
                enriched.get("summary"),
                enriched.get("reason_growth", ""),
                enriched.get("reason_decrease", ""),
            ),
        )
        if event["event_type"] == "financial_report":
            conn.execute(
                """INSERT OR REPLACE INTO financial_reports
                   (id, symbol, announcement_date, report_period, revenue,
                    net_profit, non_gaap_net_profit, operating_cash_flow,
                    roe, yoy_revenue, yoy_net_profit, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    symbol,
                    metrics.get("announcement_date") or event["event_date"],
                    metrics.get("report_period"),
                    metrics.get("revenue"),
                    metrics.get("net_profit"),
                    metrics.get("non_gaap_net_profit"),
                    metrics.get("operating_cash_flow"),
                    metrics.get("roe"),
                    metrics.get("yoy_revenue"),
                    metrics.get("yoy_net_profit"),
                    json.dumps(metrics, ensure_ascii=False),
                ),
            )
        inserted += 1
    conn.execute("UPDATE tickers SET last_news_fetch = ? WHERE symbol = ?", (datetime.now().date().isoformat(), symbol))
    conn.commit()
    conn.close()
    align_news_for_symbol(symbol)
    return {"inserted": inserted}


def _insert_analyst_ratings(symbol: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_conn()
    now = datetime.now().isoformat()
    inserted = 0
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO analyst_ratings
               (id, symbol, stock_name, report_date, institution, analyst, rating,
                is_first_rating, rating_change, previous_rating, target_price_low,
                target_price_high, source, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                symbol,
                row.get("stock_name"),
                row["report_date"],
                row.get("institution"),
                row.get("analyst"),
                row.get("rating"),
                row.get("is_first_rating"),
                row.get("rating_change"),
                row.get("previous_rating"),
                row.get("target_price_low"),
                row.get("target_price_high"),
                row.get("source"),
                json.dumps(row.get("raw") or {}, ensure_ascii=False),
                now,
            ),
        )
        inserted += 1
    conn.execute(
        """INSERT OR REPLACE INTO analyst_rating_sync
           (symbol, last_checked_at, start_date, end_date, error)
           VALUES (?, ?, ?, ?, NULL)""",
        (symbol, now, min(row["report_date"] for row in rows), max(row["report_date"] for row in rows)),
    )
    conn.commit()
    conn.close()
    return inserted


def _mark_analyst_rating_sync(symbol: str, start_date: str, end_date: str, error: Optional[str] = None) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO analyst_rating_sync
           (symbol, last_checked_at, start_date, end_date, error)
           VALUES (?, ?, ?, ?, ?)""",
        (symbol, datetime.now().isoformat(), start_date, end_date, error),
    )
    conn.commit()
    conn.close()


def _analyst_rating_row(row) -> dict:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "stock_name": row["stock_name"],
        "report_date": row["report_date"],
        "institution": row["institution"],
        "analyst": row["analyst"],
        "rating": row["rating"],
        "is_first_rating": row["is_first_rating"],
        "rating_change": row["rating_change"],
        "previous_rating": row["previous_rating"],
        "target_price_low": row["target_price_low"],
        "target_price_high": row["target_price_high"],
        "source": row["source"],
        "raw": _parse_json(row["raw_json"], {}),
        "updated_at": row["updated_at"],
    }


def _cached_analyst_ratings(
    symbol: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM analyst_ratings WHERE symbol = ?"
    params: list = [symbol]
    if start_date:
        query += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND report_date <= ?"
        params.append(end_date)
    query += " ORDER BY report_date DESC, institution ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_analyst_rating_row(row) for row in rows]


def _recent_analyst_sync(symbol: str, max_age_hours: int = 24) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM analyst_rating_sync WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    if not row:
        return None
    checked = row["last_checked_at"]
    if not checked:
        return None
    try:
        age = datetime.now() - datetime.fromisoformat(checked)
    except ValueError:
        return None
    if age.total_seconds() > max_age_hours * 3600:
        return None
    return dict(row)


def _sync_analyst_ratings(symbol: str, start_date: str, end_date: str, *, max_days: int = 90) -> dict:
    try:
        rows = fetch_analyst_ratings(symbol, start_date, end_date, max_days=max_days)
        inserted = _insert_analyst_ratings(symbol, rows)
        if not rows:
            _mark_analyst_rating_sync(symbol, start_date, end_date)
        min_date, max_date = _bounds_from_rows(rows, "report_date")
        return _source_result("success", inserted, min_date=min_date, max_date=max_date)
    except Exception as exc:
        _mark_analyst_rating_sync(symbol, start_date, end_date, error=str(exc))
        cached = _cached_analyst_ratings(symbol, start_date=start_date, end_date=end_date, limit=100)
        if cached:
            min_date, max_date = _bounds_from_rows(cached, "report_date")
            return _source_result("partial_success", len(cached), used_cache=True, error=str(exc), min_date=min_date, max_date=max_date)
        return _source_result("failed", 0, error=str(exc))


def _cached_ohlc_count(symbol: str, start_date: str, end_date: str) -> int:
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM ohlc WHERE symbol = ? AND date >= ? AND date <= ?",
        (symbol, start_date, end_date),
    ).fetchone()[0]
    conn.close()
    return int(count)


def _coverage_for_symbol(symbol: str) -> dict:
    conn = get_conn()
    coverage = {
        "symbol": symbol,
        "prices": dict(
            conn.execute(
                "SELECT COUNT(*) AS count, MIN(date) AS min_date, MAX(date) AS max_date FROM ohlc WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        ),
        "news": dict(
            conn.execute(
                """SELECT COUNT(*) AS count, MIN(event_date) AS min_date, MAX(event_date) AS max_date
                   FROM market_events WHERE symbol = ? AND event_type = 'news'""",
                (symbol,),
            ).fetchone()
        ),
        "announcements": dict(
            conn.execute(
                """SELECT COUNT(*) AS count, MIN(event_date) AS min_date, MAX(event_date) AS max_date
                   FROM market_events WHERE symbol = ? AND event_type = 'announcement'""",
                (symbol,),
            ).fetchone()
        ),
        "financial_reports": dict(
            conn.execute(
                """SELECT COUNT(*) AS count, MIN(event_date) AS min_date, MAX(event_date) AS max_date
                   FROM market_events WHERE symbol = ? AND event_type = 'financial_report'""",
                (symbol,),
            ).fetchone()
        ),
        "daily_reason_cache": dict(
            conn.execute(
                "SELECT COUNT(*) AS count, MIN(date) AS min_date, MAX(date) AS max_date FROM daily_reason_cache WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        ),
        "analyst_ratings": dict(
            conn.execute(
                "SELECT COUNT(*) AS count, MIN(report_date) AS min_date, MAX(report_date) AS max_date FROM analyst_ratings WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        ),
    }
    conn.close()
    return coverage


def _should_sync_from_coverage(coverage: dict, max_age_days: int) -> bool:
    prices = coverage.get("prices") or {}
    max_date = prices.get("max_date")
    if not max_date:
        return True
    try:
        latest = datetime.fromisoformat(max_date).date()
    except ValueError:
        return True
    age_days = (datetime.now().date() - latest).days
    return age_days >= max_age_days


def _source_with_coverage(symbol: str, source: str, result: dict) -> dict:
    coverage = _coverage_for_symbol(symbol)
    key = "prices" if source == "prices" else source
    bounds = coverage.get(key, {})
    if not bounds:
        return result
    result["min_date"] = bounds.get("min_date")
    result["max_date"] = bounds.get("max_date")
    if result["count"] == 0 and bounds.get("count"):
        result["count"] = int(bounds.get("count") or 0)
    return result


def _sync_status(sources: dict[str, dict]) -> str:
    statuses = [source["status"] for source in sources.values()]
    if sources["prices"]["status"] == "failed" and sum(
        source["count"] for key, source in sources.items() if key != "prices"
    ) == 0:
        return "failed"
    if all(status == "success" for status in statuses):
        return "success"
    if any(status in ("success", "partial_success") for status in statuses):
        return "partial_success"
    return "failed"


def sync_symbol(symbol: str, start: Optional[str] = None, end: Optional[str] = None, *, use_llm: bool = False) -> dict:
    norm = _norm(symbol)
    start_date, end_date = _default_dates(start, end)
    name = _get_ticker_name(norm) or get_stock_name(norm) or norm

    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO tickers (symbol, name, market, last_ohlc_fetch, last_news_fetch)
           VALUES (
             ?,
             CASE
               WHEN COALESCE((SELECT name FROM tickers WHERE symbol = ?), '') = '' THEN ?
               WHEN lower((SELECT name FROM tickers WHERE symbol = ?)) = lower(?) THEN ?
               ELSE (SELECT name FROM tickers WHERE symbol = ?)
             END,
             ?,
             (SELECT last_ohlc_fetch FROM tickers WHERE symbol = ?),
             (SELECT last_news_fetch FROM tickers WHERE symbol = ?)
           )""",
        (norm, norm, name, norm, norm, name, norm, get_market(norm), norm, norm),
    )
    conn.commit()
    conn.close()

    sources = {
        "prices": _source_result(),
        "news": _source_result(),
        "web_news": _source_result(),
        "announcements": _source_result(),
        "financial_reports": _source_result(),
        "analyst_ratings": _source_result(),
    }
    warnings: list[str] = []

    ohlc_rows: list[dict] = []
    try:
        ohlc_rows = fetch_ohlc(norm, start_date, end_date)
        if ohlc_rows:
            _insert_ohlc(norm, ohlc_rows)
            sources["prices"] = _source_result("success", len(ohlc_rows))
        else:
            cached_count = _cached_ohlc_count(norm, start_date, end_date)
            if cached_count > 0:
                sources["prices"] = _source_result("partial_success", cached_count, used_cache=True)
                warnings.append("日 K 上游暂无返回，已使用本地缓存行情。")
            else:
                sources["prices"] = _source_result("failed", 0, error="No A-share OHLC data returned")
                warnings.append("日 K 拉取失败，且本地没有可用缓存。")
    except Exception as exc:
        cached_count = _cached_ohlc_count(norm, start_date, end_date)
        if cached_count > 0:
            sources["prices"] = _source_result("partial_success", cached_count, used_cache=True, error=str(exc))
            warnings.append("日 K 拉取异常，已使用本地缓存行情。")
        else:
            sources["prices"] = _source_result("failed", 0, error=str(exc))
            warnings.append("日 K 拉取异常，且本地没有可用缓存。")

    news_events: list[dict] = []
    announcement_events: list[dict] = []
    financial_events: list[dict] = []

    try:
        news_events = fetch_news(norm, start_date, end_date)
        sources["news"] = _source_result("success", len(news_events))
    except Exception as exc:
        sources["news"] = _source_result("failed", 0, error=str(exc))
        warnings.append("新闻源拉取失败。")

    web_news_events: list[dict] = []
    if settings.news_web_search_enabled:
        try:
            web_info_result = discover_external_info(
                norm,
                name,
                start=start_date,
                end=end_date,
                provider=settings.news_web_search_provider,
                max_results=settings.news_web_search_max_results,
            )
            web_news_events = web_info_result["events"]
            min_date, max_date = _bounds_from_rows(web_news_events, "event_date")
            sources["web_news"] = _source_result(
                "success",
                len(web_news_events),
                used_cache=bool(web_info_result.get("cached")),
                min_date=min_date,
                max_date=max_date,
            )
        except Exception as exc:
            sources["web_news"] = _source_result("failed", 0, error=str(exc))
            warnings.append("外部资讯搜索失败。")
    else:
        sources["web_news"] = _source_result("success", 0)

    try:
        announcement_events = fetch_announcements(norm, start_date, end_date)
        sources["announcements"] = _source_result("success", len(announcement_events))
    except Exception as exc:
        sources["announcements"] = _source_result("failed", 0, error=str(exc))
        warnings.append("公告源拉取失败。")

    try:
        financial_events = fetch_financial_reports(norm)
        financial_events = _apply_financial_disclosure_dates(financial_events, announcement_events)
        sources["financial_reports"] = _source_result("success", len(financial_events))
    except Exception as exc:
        sources["financial_reports"] = _source_result("failed", 0, error=str(exc))
        warnings.append("财报源拉取失败。")

    analyst_start = (datetime.fromisoformat(end_date).date() - timedelta(days=180)).isoformat()
    sources["analyst_ratings"] = _sync_analyst_ratings(norm, analyst_start, end_date, max_days=90)
    if sources["analyst_ratings"]["status"] == "failed":
        warnings.append("机构评级/目标价拉取失败。")

    events = [*news_events, *web_news_events, *announcement_events, *financial_events]
    event_result = _insert_events(norm, events, use_llm=use_llm) if events else {"inserted": 0}
    _clear_daily_reason_cache(norm, start_date, end_date)
    for key in list(sources.keys()):
        sources[key] = _source_with_coverage(norm, key, sources[key])
    status = _sync_status(sources)

    return {
        "symbol": norm,
        "name": name,
        "start": start_date,
        "end": end_date,
        "status": status,
        "sources": sources,
        "ohlc_rows": sources["prices"]["count"],
        "events": event_result["inserted"],
        "daily_reason_cache": {"generated": 0, "failed": 0, "mode": "on_demand"},
        "coverage": _coverage_for_symbol(norm),
        "warnings": warnings,
    }


def _parse_json(value: Optional[str], fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _event_row(row) -> dict:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "event_type": row["event_type"],
        "event_date": row["event_date"],
        "published_at": row["published_at"],
        "title": row["title"],
        "summary": row["summary"],
        "source": row["source"],
        "url": row["url"],
        "sentiment": row["sentiment"] or "neutral",
        "impact": row["impact"],
        "metrics": _parse_json(row["metrics_json"], {}),
        "raw": _parse_json(row["raw_json"], {}),
    }


def _market_index_for_symbol(symbol: str) -> Optional[str]:
    code = "".join(ch for ch in symbol if ch.isdigit())
    if code.startswith(("600", "601", "603", "605")):
        return "sh000001"
    if code.startswith(("300", "301")):
        return "sz399006"
    if code.startswith(("000", "001", "002", "003")):
        return "sz399001"
    return None


def _insert_market_index_ohlc(rows: list[dict]) -> None:
    if not rows:
        return
    conn = get_conn()
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO market_index_ohlc
               (index_symbol, index_name, date, open, high, low, close,
                volume, amount, change_pct, amplitude)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["index_symbol"],
                row.get("index_name"),
                row["date"],
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume"),
                row.get("amount"),
                row.get("change_pct"),
                row.get("amplitude"),
            ),
        )
    conn.commit()
    conn.close()


def _cached_index_row(index_symbol: str, requested_date: str):
    conn = get_conn()
    row = conn.execute(
        """SELECT *
           FROM market_index_ohlc
           WHERE index_symbol = ? AND date <= ?
           ORDER BY date DESC
           LIMIT 1""",
        (index_symbol, requested_date),
    ).fetchone()
    conn.close()
    return row


def _ensure_index_row(index_symbol: str, requested_date: str):
    cached = _cached_index_row(index_symbol, requested_date)
    if cached and cached["date"] == requested_date:
        return cached, True, None

    start = (datetime.fromisoformat(requested_date).date() - timedelta(days=21)).isoformat()
    try:
        rows = fetch_market_index_ohlc(index_symbol, start, requested_date)
        _insert_market_index_ohlc(rows)
    except Exception as exc:
        rows = []
        error = str(exc)
    else:
        error = None

    row = _cached_index_row(index_symbol, requested_date)
    if row:
        return row, not bool(rows), error
    return None, False, error or "指数行情暂无可用数据"


def _build_market_context(symbol: str, requested_date: str, stock_change_pct: float) -> dict:
    selected_symbol = _market_index_for_symbol(symbol)
    index_symbols = [selected_symbol] if selected_symbol else list(MARKET_INDEXES.keys())
    summaries = []
    first_error = None

    for index_symbol in index_symbols:
        if not index_symbol:
            continue
        row, used_cache, error = _ensure_index_row(index_symbol, requested_date)
        if error and not first_error:
            first_error = error
        if not row:
            continue
        index_change = float(row["change_pct"] or 0)
        excess = stock_change_pct - index_change
        if stock_change_pct > 0 and index_change < 0:
            label = "逆势上涨"
        elif stock_change_pct < 0 and index_change > 0:
            label = "逆势下跌"
        elif abs(excess) >= 2:
            label = "明显跑赢大盘" if excess > 0 else "明显跑输大盘"
        elif (stock_change_pct >= 0 and index_change >= 0) or (stock_change_pct <= 0 and index_change <= 0):
            label = "跟随大盘"
        else:
            label = "与大盘分化"
        summaries.append(
            {
                "available": True,
                "index_symbol": row["index_symbol"],
                "index_name": row["index_name"],
                "index_date": row["date"],
                "index_change_pct": round(index_change, 2),
                "stock_change_pct": round(stock_change_pct, 2),
                "excess_return_pct": round(excess, 2),
                "relationship": label,
                "used_cache": used_cache,
            }
        )

    if not summaries:
        return {
            "available": False,
            "index_symbol": selected_symbol,
            "index_name": MARKET_INDEXES.get(selected_symbol or "", {}).get("name"),
            "error": first_error or "没有匹配到可用指数行情",
        }

    primary = dict(summaries[0])
    primary["all_indexes"] = summaries
    return primary


def _insert_northbound_flow(rows: list[dict]) -> None:
    if not rows:
        return
    conn = get_conn()
    for row in rows:
        if not row.get("date"):
            continue
        conn.execute(
            """INSERT OR REPLACE INTO northbound_flow
               (date, sh_net_flow, sz_net_flow, total_flow)
               VALUES (?, ?, ?, ?)""",
            (row["date"], row.get("sh_net_flow"), row.get("sz_net_flow"), row.get("total_flow")),
        )
    conn.commit()
    conn.close()


def _cached_northbound_row(requested_date: str):
    conn = get_conn()
    row = conn.execute(
        """SELECT *
           FROM northbound_flow
           WHERE date <= ?
           ORDER BY date DESC
           LIMIT 1""",
        (requested_date,),
    ).fetchone()
    conn.close()
    return row


def _build_fund_context(requested_date: str, stock_change_pct: float) -> dict:
    row = _cached_northbound_row(requested_date)
    used_cache = bool(row)
    error = None
    if not row or row["date"] != requested_date:
        try:
            _insert_northbound_flow(fetch_northbound_flow(days=365))
        except Exception as exc:
            error = str(exc)
        row = _cached_northbound_row(requested_date)

    if not row:
        return {"available": False, "error": error or "暂无北向资金数据"}

    total_flow = float(row["total_flow"] or 0)
    direction = "净流入" if total_flow > 0 else "净流出" if total_flow < 0 else "基本持平"
    if stock_change_pct > 0 and total_flow > 0:
        relationship = "资金方向与上涨同向"
    elif stock_change_pct < 0 and total_flow < 0:
        relationship = "资金方向与下跌同向"
    elif abs(total_flow) < 1:
        relationship = "资金信号较弱"
    else:
        relationship = "资金方向与股价分化"

    return {
        "available": True,
        "date": row["date"],
        "total_flow": round(total_flow, 2),
        "sh_net_flow": row["sh_net_flow"],
        "sz_net_flow": row["sz_net_flow"],
        "direction": direction,
        "relationship": relationship,
        "used_cache": used_cache,
        "error": error,
    }


def _cached_stock_industry(symbol: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM stock_industry_map WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    return row


def _ensure_stock_industry(symbol: str) -> dict:
    cached = _cached_stock_industry(symbol)
    if cached and cached["board_name"]:
        return {
            "available": True,
            "industry_name": cached["industry_name"],
            "board_name": cached["board_name"],
            "used_cache": True,
        }

    try:
        profile = fetch_stock_profile(symbol)
    except Exception as exc:
        profile = {}
        error = str(exc)
    else:
        error = None

    industry = profile.get("industry_name")
    board = profile.get("board_name") or industry_board_name(industry or "")
    if industry or board:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO stock_industry_map
               (symbol, industry_name, board_name, source, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (symbol, industry, board, "akshare.stock_individual_info_em", datetime.now().isoformat()),
        )
        if industry:
            conn.execute("UPDATE tickers SET industry = ? WHERE symbol = ?", (industry, symbol))
        conn.commit()
        conn.close()
        return {
            "available": True,
            "industry_name": industry,
            "board_name": board,
            "used_cache": False,
        }

    return {"available": False, "error": error or "暂无行业映射"}


def _insert_industry_board_ohlc(rows: list[dict]) -> None:
    if not rows:
        return
    conn = get_conn()
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO industry_board_ohlc
               (board_name, date, open, high, low, close, volume, amount, change_pct, amplitude)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["board_name"],
                row["date"],
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume"),
                row.get("amount"),
                row.get("change_pct"),
                row.get("amplitude"),
            ),
        )
    conn.commit()
    conn.close()


def _cached_industry_row(board_name: str, requested_date: str):
    conn = get_conn()
    row = conn.execute(
        """SELECT *
           FROM industry_board_ohlc
           WHERE board_name = ? AND date <= ?
           ORDER BY date DESC
           LIMIT 1""",
        (board_name, requested_date),
    ).fetchone()
    conn.close()
    return row


def _build_industry_context(symbol: str, requested_date: str, stock_change_pct: float) -> dict:
    mapping = _ensure_stock_industry(symbol)
    if not mapping.get("available"):
        return {"available": False, "error": mapping.get("error") or "暂无行业映射"}

    industry = mapping.get("industry_name")
    board = mapping.get("board_name")
    row = _cached_industry_row(board, requested_date)
    used_cache = bool(row)
    error = None
    if not row or row["date"] != requested_date:
        start = (datetime.fromisoformat(requested_date).date() - timedelta(days=21)).isoformat()
        try:
            rows = fetch_industry_board_ohlc(board, start, requested_date)
            _insert_industry_board_ohlc(rows)
        except Exception as exc:
            rows = []
            error = str(exc)
        row = _cached_industry_row(board, requested_date)
        used_cache = not bool(rows)

    if not row:
        return {
            "available": False,
            "industry_name": industry,
            "board_name": board,
            "error": error or "暂无行业板块行情",
            "mapping_used_cache": mapping.get("used_cache", False),
        }

    industry_change = float(row["change_pct"] or 0)
    excess = stock_change_pct - industry_change
    if stock_change_pct > 0 and industry_change < 0:
        relationship = "逆板块上涨"
    elif stock_change_pct < 0 and industry_change > 0:
        relationship = "逆板块下跌"
    elif abs(excess) >= 2:
        relationship = "明显跑赢行业" if excess > 0 else "明显跑输行业"
    elif (stock_change_pct >= 0 and industry_change >= 0) or (stock_change_pct <= 0 and industry_change <= 0):
        relationship = "跟随行业"
    else:
        relationship = "与行业分化"

    return {
        "available": True,
        "industry_name": industry,
        "board_name": board,
        "board_date": row["date"],
        "industry_change_pct": round(industry_change, 2),
        "stock_change_pct": round(stock_change_pct, 2),
        "excess_return_pct": round(excess, 2),
        "relationship": relationship,
        "used_cache": used_cache,
        "mapping_used_cache": mapping.get("used_cache", False),
        "error": error,
    }


def _build_analyst_context(symbol: str, requested_date: str, current_close: float) -> dict:
    start_date = (datetime.fromisoformat(requested_date).date() - timedelta(days=180)).isoformat()
    ratings = _cached_analyst_ratings(symbol, start_date=start_date, end_date=requested_date, limit=8)
    used_cache = True
    error = None
    if not ratings and not _recent_analyst_sync(symbol, max_age_hours=24):
        result = _sync_analyst_ratings(symbol, start_date, requested_date, max_days=90)
        used_cache = result.get("used_cache", False)
        error = result.get("error")
        ratings = _cached_analyst_ratings(symbol, start_date=start_date, end_date=requested_date, limit=8)

    if not ratings:
        return {
            "available": False,
            "ratings": [],
            "error": error or "暂无可用机构评级/目标价",
            "used_cache": used_cache,
        }

    targets = [
        item
        for item in ratings
        if item.get("target_price_low") is not None or item.get("target_price_high") is not None
    ]
    latest = ratings[0]
    latest_target = targets[0] if targets else None
    target_mid = None
    target_low = None
    target_high = None
    target_upside_pct = None
    if latest_target:
        target_low = latest_target.get("target_price_low")
        target_high = latest_target.get("target_price_high")
        values = [value for value in (target_low, target_high) if value is not None]
        if values:
            target_mid = sum(float(value) for value in values) / len(values)
            if current_close:
                target_upside_pct = (target_mid - current_close) / current_close * 100

    return {
        "available": True,
        "ratings": ratings,
        "latest": latest,
        "latest_with_target": latest_target,
        "current_close": current_close,
        "target_price_low": target_low,
        "target_price_high": target_high,
        "target_price_mid": round(target_mid, 2) if target_mid is not None else None,
        "target_upside_pct": round(target_upside_pct, 2) if target_upside_pct is not None else None,
        "used_cache": used_cache,
        "error": error,
    }


def _analyst_context_text(analyst_context: Optional[dict]) -> str:
    if not analyst_context:
        return ""
    if not analyst_context.get("available"):
        return f"机构预期不可用：{analyst_context.get('error') or '暂无数据'}。"
    latest = analyst_context.get("latest") or {}
    target = analyst_context.get("latest_with_target") or {}
    target_mid = analyst_context.get("target_price_mid")
    target_upside = analyst_context.get("target_upside_pct")
    target_text = "未披露目标价"
    if target_mid is not None:
        target_text = f"目标价中枢 {target_mid:.2f} 元，相对收盘价 {target_upside:+.2f}%"
    return (
        f"最新机构评级：{latest.get('report_date')} {latest.get('institution')} "
        f"{latest.get('rating') or '未给出评级'}，评级变化 {latest.get('rating_change') or '未披露'}。"
        f"目标价参考：{target_text}。"
        f"可用评级记录 {len(analyst_context.get('ratings') or [])} 条。"
    )


def _market_context_text(
    market_context: dict,
    fund_context: dict,
    industry_context: Optional[dict] = None,
    analyst_context: Optional[dict] = None,
) -> str:
    lines = []
    if market_context.get("available"):
        lines.append(
            f"{market_context.get('index_name')}({market_context.get('index_date')}) "
            f"涨跌幅 {market_context.get('index_change_pct'):+.2f}%，"
            f"个股超额收益 {market_context.get('excess_return_pct'):+.2f}%，"
            f"关系：{market_context.get('relationship')}。"
        )
    else:
        lines.append(f"指数背景不可用：{market_context.get('error') or '暂无数据'}。")
    if fund_context.get("available"):
        lines.append(
            f"北向资金({fund_context.get('date')}) {fund_context.get('direction')} "
            f"{fund_context.get('total_flow'):+.2f} 亿元，"
            f"关系：{fund_context.get('relationship')}。"
        )
    else:
        lines.append(f"北向资金不可用：{fund_context.get('error') or '暂无数据'}。")
    if industry_context:
        if industry_context.get("available"):
            lines.append(
                f"行业板块 {industry_context.get('board_name')}({industry_context.get('board_date')}) "
                f"涨跌幅 {industry_context.get('industry_change_pct'):+.2f}%，"
                f"个股相对行业 {industry_context.get('excess_return_pct'):+.2f}%，"
                f"关系：{industry_context.get('relationship')}。"
            )
        else:
            lines.append(f"行业背景不可用：{industry_context.get('error') or '暂无数据'}。")
    analyst_line = _analyst_context_text(analyst_context)
    if analyst_line:
        lines.append(analyst_line)
    return "\n".join(lines)


def _daily_local_reason(
    symbol: str,
    day: dict,
    prev_day: Optional[dict],
    events: list[dict],
    market_context: Optional[dict] = None,
    fund_context: Optional[dict] = None,
    industry_context: Optional[dict] = None,
    analyst_context: Optional[dict] = None,
) -> dict:
    previous_close = prev_day["close"] if prev_day else None
    if day.get("change_pct") is not None:
        change_pct = float(day["change_pct"])
    elif previous_close:
        change_pct = (float(day["close"]) - float(previous_close)) / float(previous_close) * 100
    else:
        change_pct = (float(day["close"]) - float(day["open"])) / float(day["open"]) * 100

    direction = "上涨" if change_pct > 0 else "下跌" if change_pct < 0 else "横盘"
    positive = [event for event in events if event.get("sentiment") == "positive"]
    negative = [event for event in events if event.get("sentiment") == "negative"]
    financial = [event for event in events if event.get("event_type") == "financial_report"]
    announcements = [event for event in events if event.get("event_type") == "announcement"]
    news = [event for event in events if event.get("event_type") == "news"]
    capital = [event for event in events if event.get("event_type") == "capital"]
    amplitude = float(day.get("amplitude") or 0)
    turnover_rate = float(day.get("turnover_rate") or 0)
    volume = float(day.get("volume") or 0)

    possible_reasons: list[str] = []
    if financial:
        possible_reasons.append(f"附近有 {len(financial)} 条财报事件，可能影响基本面预期。")
    if announcements:
        possible_reasons.append(f"附近有 {len(announcements)} 条公告，可能触发信息重估。")
    if news:
        possible_reasons.append(f"附近有 {len(news)} 条新闻，可能影响短期情绪。")
    if capital:
        possible_reasons.append(f"附近有 {len(capital)} 条资金/游资资讯，可能影响短线资金偏好。")
    if positive and change_pct >= 0:
        possible_reasons.append(f"匹配到 {len(positive)} 条偏利好事件，与当日上涨方向一致。")
    if negative and change_pct <= 0:
        possible_reasons.append(f"匹配到 {len(negative)} 条偏利空事件，与当日下跌方向一致。")
    if abs(change_pct) >= 3:
        possible_reasons.append(f"当日涨跌幅达到 {change_pct:+.2f}%，属于较明显波动，需要结合事件和市场环境复核。")
    if amplitude >= 4:
        possible_reasons.append(f"当日振幅 {amplitude:.2f}%，盘中分歧较大。")
    if turnover_rate >= 3:
        possible_reasons.append(f"当日换手率 {turnover_rate:.2f}%，交易活跃度较高。")
    if volume > 0 and prev_day and prev_day.get("volume"):
        prev_volume = float(prev_day["volume"] or 0)
        if prev_volume > 0:
            volume_ratio = volume / prev_volume
            if volume_ratio >= 1.5:
                possible_reasons.append(f"成交量较前一交易日放大 {volume_ratio:.1f} 倍，说明资金参与度上升。")
            elif volume_ratio <= 0.6:
                possible_reasons.append("成交量较前一交易日明显缩小，价格变化可能缺少成交确认。")
    if market_context and market_context.get("available"):
        relationship = market_context.get("relationship")
        index_name = market_context.get("index_name")
        index_change = market_context.get("index_change_pct")
        excess = market_context.get("excess_return_pct")
        if relationship in ("逆势上涨", "逆势下跌"):
            possible_reasons.append(
                f"当日个股{relationship}，{index_name}涨跌幅 {index_change:+.2f}%，说明个股表现与大盘方向分化。"
            )
        elif relationship in ("明显跑赢大盘", "明显跑输大盘"):
            possible_reasons.append(
                f"当日个股{relationship}，相对 {index_name} 超额收益 {excess:+.2f}%，需要优先检查个股事件或板块因素。"
            )
        else:
            possible_reasons.append(
                f"当日表现与 {index_name} 的市场环境关系为“{relationship}”，大盘涨跌幅 {index_change:+.2f}%。"
            )
    elif market_context:
        possible_reasons.append(f"指数背景暂不可用：{market_context.get('error') or '暂无可用数据'}。")
    if fund_context and fund_context.get("available"):
        possible_reasons.append(
            f"最近北向资金{fund_context.get('direction')} {fund_context.get('total_flow'):+.2f} 亿元，"
            f"{fund_context.get('relationship')}，可作为市场资金情绪背景。"
        )
    elif fund_context:
        possible_reasons.append(f"北向资金背景暂不可用：{fund_context.get('error') or '暂无可用数据'}。")
    if industry_context and industry_context.get("available"):
        relationship = industry_context.get("relationship")
        board_name = industry_context.get("board_name")
        industry_change = industry_context.get("industry_change_pct")
        excess = industry_context.get("excess_return_pct")
        if relationship in ("逆板块上涨", "逆板块下跌"):
            possible_reasons.append(
                f"当日个股{relationship}，{board_name}涨跌幅 {industry_change:+.2f}%，个股与行业方向分化。"
            )
        elif relationship in ("明显跑赢行业", "明显跑输行业"):
            possible_reasons.append(
                f"当日个股{relationship}，相对 {board_name} 超额收益 {excess:+.2f}%，个股自身事件或资金因素更值得复核。"
            )
        else:
            possible_reasons.append(
                f"当日个股与 {board_name} 的关系为“{relationship}”，行业涨跌幅 {industry_change:+.2f}%。"
            )
    elif industry_context:
        possible_reasons.append(f"行业背景暂不可用：{industry_context.get('error') or '暂无可用数据'}。")
    if analyst_context and analyst_context.get("available"):
        ratings = analyst_context.get("ratings") or []
        latest = analyst_context.get("latest") or {}
        target_mid = analyst_context.get("target_price_mid")
        target_upside = analyst_context.get("target_upside_pct")
        if target_mid is not None and target_upside is not None:
            possible_reasons.append(
                f"近 180 天匹配到 {len(ratings)} 条机构评级，最新可用目标价中枢约 {target_mid:.2f} 元，"
                f"相对当日收盘价 {target_upside:+.2f}%，这是预期参考，不代表短期确定驱动。"
            )
        else:
            possible_reasons.append(
                f"近 180 天匹配到 {len(ratings)} 条机构评级，最新为 {latest.get('institution') or '机构'}"
                f"{latest.get('rating') or '评级未披露'}，但未披露目标价。"
            )
    elif analyst_context:
        possible_reasons.append(f"机构预期暂不可用：{analyst_context.get('error') or '暂无可用机构评级/目标价'}。")
    if not possible_reasons:
        possible_reasons.append("未匹配到足够明确的同日或前一交易日事件，可能更多受市场、行业或技术面因素影响。")

    evidence_quality = "high" if len(events) >= 3 and (positive or negative or financial or capital) else "medium" if events else "low"
    summary = (
        f"{symbol} 在 {day['date']} {direction} {change_pct:+.2f}%。"
        f"系统在当日及附近交易日匹配到 {len(events)} 条事件："
        f"新闻 {len(news)}、公告 {len(announcements)}、财报 {len(financial)}、资金 {len(capital)}。"
        "同时纳入了大盘指数、北向资金和行业板块背景。"
        "这些只能作为可能归因，不能视为确定因果。"
    )
    return {
        "summary": summary,
        "possible_reasons": possible_reasons,
        "bullish_factors": [event["title"] for event in positive[:5]],
        "bearish_factors": [event["title"] for event in negative[:5]],
        "evidence_quality": evidence_quality,
        "change_pct": round(change_pct, 2),
    }


def _daily_event_context(events: list[dict]) -> str:
    return "\n".join(
        f"{i + 1}. [{event['event_date']}] [{event['event_type']}] "
        f"{event['title']} - {event.get('summary') or ''}"
        for i, event in enumerate(events[:30])
    )


@router.get("")
def list_tickers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tickers ORDER BY symbol").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        if not item.get("name") or str(item.get("name")).lower() == str(item["symbol"]).lower():
            resolved = _get_ticker_name(item["symbol"])
            if resolved:
                item["name"] = resolved
        coverage_row = conn.execute(
            """SELECT
                   (SELECT MAX(date) FROM ohlc WHERE symbol = ?) AS latest_ohlc_date,
                   (SELECT MAX(event_date) FROM market_events WHERE symbol = ?) AS latest_event_date
            """,
            (item["symbol"], item["symbol"]),
        ).fetchone()
        if coverage_row:
            item["latest_ohlc_date"] = coverage_row["latest_ohlc_date"]
            item["latest_event_date"] = coverage_row["latest_event_date"]
        item["display_name"] = _display_symbol(item["symbol"], item.get("name"))
        results.append(item)
    conn.close()
    return results


@router.get("/search")
def search(q: str = Query(..., min_length=1)):
    conn = get_conn()
    local = conn.execute(
        "SELECT symbol, name, sector, market FROM tickers WHERE symbol LIKE ? OR name LIKE ? LIMIT 10",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    conn.close()
    results = [dict(row) for row in local]
    seen = {row["symbol"] for row in results}
    try:
        for item in search_tickers(q, limit=20):
            if item["symbol"] not in seen:
                results.append(item)
    except Exception as exc:
        logger.info("Remote A-share search failed: %s", exc)
    for item in results:
        item["display_name"] = _display_symbol(item["symbol"], item.get("name"))
    return results


@router.get("/{symbol}/ohlc")
def get_ohlc(symbol: str, start: Optional[str] = None, end: Optional[str] = None):
    conn = get_conn()
    norm = _norm(symbol)
    query = "SELECT * FROM ohlc WHERE symbol = ?"
    params: list = [norm]
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    query += " ORDER BY date ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol}. Try POST /api/stocks/{symbol}/sync first.")
    return [dict(row) for row in rows]


@router.get("/{symbol}/prices")
def get_prices(symbol: str, start: Optional[str] = None, end: Optional[str] = None):
    return get_ohlc(symbol, start, end)


@router.get("/{symbol}/events")
def get_events(symbol: str, start: Optional[str] = None, end: Optional[str] = None):
    conn = get_conn()
    norm = _norm(symbol)
    query = "SELECT * FROM market_events WHERE symbol = ?"
    params: list = [norm]
    if start:
        query += " AND event_date >= ?"
        params.append(start)
    if end:
        query += " AND event_date <= ?"
        params.append(end)
    query += " ORDER BY event_date DESC, published_at DESC LIMIT 500"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


@router.get("/{symbol}/analyst-ratings")
def get_analyst_ratings(
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    refresh: bool = False,
):
    norm = _norm(symbol)
    end_date = datetime.fromisoformat(end).date().isoformat() if end else datetime.now().date().isoformat()
    start_date = (
        datetime.fromisoformat(start).date().isoformat()
        if start
        else (datetime.fromisoformat(end_date).date() - timedelta(days=180)).isoformat()
    )
    ratings = _cached_analyst_ratings(norm, start_date=start_date, end_date=end_date, limit=limit)
    sync_result = None
    if refresh or not ratings:
        sync_result = _sync_analyst_ratings(norm, start_date, end_date, max_days=90)
        ratings = _cached_analyst_ratings(norm, start_date=start_date, end_date=end_date, limit=limit)
    return {
        "symbol": norm,
        "name": _get_ticker_name(norm),
        "start": start_date,
        "end": end_date,
        "count": len(ratings),
        "items": ratings,
        "sync": sync_result,
    }


def _macro_cache_expiry() -> str:
    return (datetime.now() + timedelta(hours=settings.macro_chain_cache_ttl_hours)).isoformat()


def _macro_cache_row(symbol: str, date_text: str, context_type: str = "macro_chain") -> Optional[dict]:
    now = datetime.now().isoformat()
    conn = get_conn()
    row = conn.execute(
        """SELECT payload_json, sources_count, llm_used, generated_at, expires_at
           FROM macro_chain_context
           WHERE symbol = ? AND date = ? AND context_type = ?
             AND (expires_at IS NULL OR expires_at >= ?)""",
        (symbol, date_text, context_type, now),
    ).fetchone()
    conn.close()
    if not row:
        return None
    payload = _parse_json(row["payload_json"], {})
    if not isinstance(payload, dict):
        return None
    payload["cached"] = True
    payload["generated_at"] = payload.get("generated_at") or row["generated_at"]
    payload["expires_at"] = payload.get("expires_at") or row["expires_at"]
    payload["sources_count"] = int(row["sources_count"] or payload.get("sources_count") or 0)
    payload["llm_used"] = bool(row["llm_used"])
    return payload


def _store_macro_cache(symbol: str, date_text: str, payload: dict, context_type: str = "macro_chain") -> None:
    generated_at = payload.get("generated_at") or datetime.now().isoformat()
    expires_at = payload.get("expires_at") or _macro_cache_expiry()
    to_store = dict(payload)
    to_store["cached"] = False
    to_store["generated_at"] = generated_at
    to_store["expires_at"] = expires_at
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO macro_chain_context
           (symbol, date, context_type, payload_json, sources_count, llm_used, generated_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            symbol,
            date_text,
            context_type,
            json.dumps(to_store, ensure_ascii=False),
            int(to_store.get("sources_count") or len(to_store.get("sources") or [])),
            1 if to_store.get("llm_used") else 0,
            generated_at,
            expires_at,
        ),
    )
    conn.commit()
    conn.close()


def _industry_change(board_name: str, date_text: str, stock_change_pct: float = 0) -> dict:
    if not board_name:
        return {"available": False, "board_name": board_name, "error": "暂无行业名称"}
    row = _cached_industry_row(board_name, date_text)
    if not row:
        try:
            start = (datetime.fromisoformat(date_text).date() - timedelta(days=14)).isoformat()
            _insert_industry_board_ohlc(fetch_industry_board_ohlc(board_name, start, date_text))
            row = _cached_industry_row(board_name, date_text)
        except Exception as exc:
            return {"available": False, "board_name": board_name, "error": str(exc)}
    if not row:
        return {"available": False, "board_name": board_name, "error": "暂无行业行情"}
    change = float(row["change_pct"] or 0)
    return {
        "available": True,
        "board_name": row["board_name"],
        "date": row["date"],
        "change_pct": round(change, 2),
        "excess_return_pct": round(stock_change_pct - change, 2),
        "amount": row["amount"],
        "volume": row["volume"],
    }


def _sector_constituents_expiry(date_text: str) -> str:
    try:
        snapshot_date = datetime.fromisoformat(date_text).date()
    except ValueError:
        snapshot_date = datetime.now().date()
    if snapshot_date < datetime.now().date():
        return (datetime.now() + timedelta(days=30)).isoformat()
    return (datetime.now() + timedelta(hours=18)).isoformat()


def _cached_sector_constituents(board_name: str, date_text: str) -> Optional[list[dict]]:
    if not board_name:
        return None
    now = datetime.now().isoformat()
    conn = get_conn()
    row = conn.execute(
        """SELECT payload_json, expires_at
           FROM sector_constituents_cache
           WHERE board_name = ? AND date = ?
           LIMIT 1""",
        (board_name, date_text),
    ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT payload_json, expires_at
               FROM sector_constituents_cache
               WHERE board_name = ?
               ORDER BY date DESC, generated_at DESC
               LIMIT 1""",
            (board_name,),
        ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        snapshot_date = datetime.fromisoformat(date_text).date()
    except ValueError:
        snapshot_date = datetime.now().date()
    if snapshot_date >= datetime.now().date() and row["expires_at"] and row["expires_at"] < now:
        return None
    payload = _parse_json(row["payload_json"], [])
    return payload if isinstance(payload, list) else None


def _store_sector_constituents(board_name: str, date_text: str, items: list[dict]) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO sector_constituents_cache
           (board_name, date, payload_json, generated_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            board_name,
            date_text,
            json.dumps(items, ensure_ascii=False),
            datetime.now().isoformat(),
            _sector_constituents_expiry(date_text),
        ),
    )
    for item in items:
        if item.get("symbol"):
            conn.execute(
                "INSERT OR IGNORE INTO tickers (symbol, name, market) VALUES (?, ?, ?)",
                (item["symbol"], item.get("name"), get_market(item["symbol"])),
            )
            conn.execute(
                """INSERT OR REPLACE INTO stock_industry_map
                   (symbol, industry_name, board_name, source, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    item["symbol"],
                    board_name,
                    board_name,
                    item.get("source") or "sector_constituents_cache",
                    datetime.now().isoformat(),
                ),
            )
    conn.commit()
    conn.close()


def _local_constituent_performance(symbol: str, date_text: str) -> Optional[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT date, close, change_pct, amount, volume, turnover_rate
           FROM ohlc
           WHERE symbol = ? AND date <= ?
           ORDER BY date DESC
           LIMIT 21""",
        (symbol, date_text),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    latest = dict(rows[0])

    def ret_at(index: int) -> Optional[float]:
        if len(rows) <= index:
            return None
        previous = rows[index]["close"]
        current = latest.get("close")
        if not previous or not current:
            return None
        return round((float(current) / float(previous) - 1) * 100, 2)

    return {
        "date": latest.get("date"),
        "close": latest.get("close"),
        "change_pct": latest.get("change_pct"),
        "return_5d_pct": ret_at(5),
        "return_20d_pct": ret_at(20),
        "amount": latest.get("amount"),
        "volume": latest.get("volume"),
        "turnover_rate": latest.get("turnover_rate"),
        "source": "local_ohlc",
    }


def _ensure_constituent_ohlc(symbol: str, date_text: str) -> Optional[dict]:
    """Best-effort OHLC fill for a mentioned peer company."""
    if not symbol:
        return None
    existing = _local_constituent_performance(symbol, date_text)
    if existing:
        return existing

    try:
        end_date = datetime.fromisoformat(date_text).date()
    except ValueError:
        return None
    start = (end_date - timedelta(days=45)).isoformat()
    try:
        rows = fetch_ohlc(symbol, start, date_text)
        if rows:
            _insert_ohlc(symbol, rows)
    except Exception as exc:
        logger.info("Failed to hydrate peer OHLC for %s: %s", symbol, exc)
        return None
    return _local_constituent_performance(symbol, date_text)


def _fallback_local_sector_companies(board_name: str, date_text: str, limit: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.symbol, t.name, o.date, o.close, o.change_pct, o.amount, o.volume, o.turnover_rate
           FROM tickers t
           JOIN stock_industry_map m ON m.symbol = t.symbol
           JOIN ohlc o ON o.symbol = t.symbol
           WHERE m.board_name = ? AND o.date <= ?
           ORDER BY o.date DESC, COALESCE(o.amount, 0) DESC
           LIMIT ?""",
        (board_name, date_text, max(limit, 12)),
    ).fetchall()
    conn.close()
    latest_by_symbol: dict[str, dict] = {}
    for row in rows:
        latest_by_symbol.setdefault(row["symbol"], dict(row))
    return [
        {
            "symbol": row["symbol"],
            "code": row["symbol"][-6:],
            "name": row["name"],
            "board_name": board_name,
            "latest_price": row["close"],
            "change_pct": row["change_pct"],
            "amount": row["amount"],
            "volume": row["volume"],
            "turnover_rate": row["turnover_rate"],
            "source": "local_sector_sample",
        }
        for row in latest_by_symbol.values()
    ][:limit]


def _event_mentioned_companies(symbol: str, board_name: str, date_text: str, limit: int = 10) -> list[dict]:
    if not symbol:
        return []
    try:
        start_date = (datetime.fromisoformat(date_text).date() - timedelta(days=60)).isoformat()
    except ValueError:
        start_date = date_text
    conn = get_conn()
    rows = conn.execute(
        """SELECT title, summary, event_date
           FROM market_events
           WHERE symbol = ? AND event_date >= ? AND event_date <= ?
           ORDER BY event_date DESC, published_at DESC
           LIMIT 80""",
        (symbol, start_date, date_text),
    ).fetchall()
    conn.close()
    candidates: dict[str, dict] = {}
    pattern = re.compile(r"(?P<code>\d{6})\s*(?P<name>[\*A-Za-z\u4e00-\u9fff]{2,12})")
    for row in rows:
        text = f"{row['title'] or ''} {row['summary'] or ''}"
        for match in pattern.finditer(text):
            code = match.group("code")
            name = match.group("name").strip("：:，,。；;（）()[]【】")
            norm = normalize(code)
            if norm == symbol or not name:
                continue
            tail = text[match.end(): match.end() + 48]
            numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", tail)
            inline_change = None
            inline_price = None
            if numbers:
                try:
                    candidate_change = float(numbers[0])
                    if abs(candidate_change) <= 30:
                        inline_change = candidate_change
                except ValueError:
                    pass
            if len(numbers) > 1:
                try:
                    candidate_price = float(numbers[1])
                    if candidate_price > 0:
                        inline_price = candidate_price
                except ValueError:
                    pass
            item = candidates.setdefault(
                norm,
                {
                    "symbol": norm,
                    "code": code,
                    "name": name[:12],
                    "board_name": board_name,
                    "latest_price": inline_price,
                    "change_pct": inline_change,
                    "amount": None,
                    "volume": None,
                    "turnover_rate": None,
                    "source": "event_mentions",
                    "mentions": 0,
                },
            )
            item["mentions"] += 1
            if item.get("change_pct") is None and inline_change is not None:
                item["change_pct"] = inline_change
            if item.get("latest_price") is None and inline_price is not None:
                item["latest_price"] = inline_price
    ranked = sorted(candidates.values(), key=lambda item: item.get("mentions") or 0, reverse=True)
    return ranked[:limit]


def _sector_company_candidates(board_name: str, date_text: str, limit: int = 12, context_symbol: Optional[str] = None) -> dict:
    if not board_name:
        return {
            "board_name": board_name,
            "date": date_text,
            "items": [],
            "quality": "low",
            "note": "暂无行业名称，无法获取板块成分股。",
        }
    board = industry_board_name(board_name)
    cached = _cached_sector_constituents(board, date_text)
    fetched = False
    error = None
    items = cached
    if items is None:
        try:
            items = fetch_industry_board_constituents(board)
            fetched = bool(items)
            if items:
                _store_sector_constituents(board, date_text, items)
        except Exception as exc:
            error = str(exc)
            items = []
    if not items:
        items = _fallback_local_sector_companies(board, date_text, limit)
    if context_symbol and len(items) < limit:
        known = {item.get("symbol") for item in items}
        for item in _event_mentioned_companies(context_symbol, board, date_text, limit=limit):
            if item.get("symbol") not in known:
                items.append(item)
                known.add(item.get("symbol"))
            if len(items) >= limit:
                break
    elif context_symbol:
        mentioned = {item.get("symbol"): item for item in _event_mentioned_companies(context_symbol, board, date_text, limit=limit)}
        for item in items:
            extra = mentioned.get(item.get("symbol"))
            if not extra:
                continue
            if item.get("change_pct") is None and extra.get("change_pct") is not None:
                item["change_pct"] = extra["change_pct"]
            if item.get("latest_price") is None and extra.get("latest_price") is not None:
                item["latest_price"] = extra["latest_price"]
            if item.get("source") == "event_mentions":
                item["source"] = "event_mentions"
    if items and cached is None and not fetched:
        _store_sector_constituents(board, date_text, items)

    enriched = []
    seen_symbols: set[str] = set()
    for item in items:
        symbol = item.get("symbol")
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        local_perf = _local_constituent_performance(symbol, date_text) if symbol else None
        enriched.append(
            {
                "symbol": symbol,
                "code": item.get("code") or (symbol[-6:] if symbol else None),
                "name": item.get("name") or symbol,
                "display_name": _display_symbol(symbol, item.get("name")) if symbol else item.get("name"),
                "board_name": board,
                "date": (local_perf or {}).get("date") or date_text,
                "close": (local_perf or {}).get("close", item.get("latest_price")),
                "change_pct": (local_perf or {}).get("change_pct", item.get("change_pct")),
                "return_5d_pct": (local_perf or {}).get("return_5d_pct"),
                "return_20d_pct": (local_perf or {}).get("return_20d_pct"),
                "amount": (local_perf or {}).get("amount", item.get("amount")),
                "volume": (local_perf or {}).get("volume", item.get("volume")),
                "turnover_rate": (local_perf or {}).get("turnover_rate", item.get("turnover_rate")),
                "market_cap": item.get("market_cap"),
                "source": (local_perf or {}).get("source") or item.get("source") or "sector_constituents",
            }
        )
    enriched.sort(key=lambda item: (item.get("amount") or 0), reverse=True)
    quality = "high" if len(enriched) >= 8 else "medium" if len(enriched) >= 3 else "low"
    note = "板块成分股来自 AKShare 东方财富行业成分，近 5/20 日涨幅优先使用本地已缓存日 K；缺失时显示为空。"
    if error and not enriched:
        note = f"暂无可靠成分股数据：{error}"
    elif not fetched and cached is not None:
        note = "板块成分股来自本地缓存，近 5/20 日涨幅优先使用本地已缓存日 K。"
    elif any((item.get("source") == "event_mentions") for item in enriched):
        note = "板块成分股接口不可用时，使用资讯中明确提到的公司作为同链候选；当日涨跌优先取日 K，缺失时用资讯表格中的当日涨跌线索，近 5/20 日缺失则留空。"
    elif not enriched:
        note = "暂无可靠成分股数据。"
    return {
        "board_name": board,
        "date": date_text,
        "items": enriched[:limit],
        "count": len(enriched),
        "quality": quality,
        "note": note,
        "cached": cached is not None,
        "error": error,
    }


def _leader_candidates(board_name: str, date_text: str, symbol: str) -> dict:
    candidates = _sector_company_candidates(board_name, date_text, limit=8, context_symbol=symbol)
    if candidates.get("items"):
        return {
            "board_name": candidates.get("board_name") or board_name,
            "date": date_text,
            "items": candidates["items"][:5],
            "quality": candidates.get("quality", "medium"),
            "note": "按成交额筛选同板块活跃公司，作为板块龙头/核心样本参考，不代表确定排名。",
        }

    conn = get_conn()
    rows = conn.execute(
        """SELECT t.symbol, t.name, o.date, o.close, o.change_pct, o.amount, o.volume
           FROM tickers t
           JOIN stock_industry_map m ON m.symbol = t.symbol
           JOIN ohlc o ON o.symbol = t.symbol
           WHERE m.board_name = ? AND o.date <= ?
           ORDER BY o.date DESC, COALESCE(o.amount, 0) DESC
           LIMIT 12""",
        (board_name, date_text),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """SELECT t.symbol, t.name, o.date, o.close, o.change_pct, o.amount, o.volume
               FROM tickers t
               JOIN ohlc o ON o.symbol = t.symbol
               WHERE t.symbol = ? AND o.date <= ?
               ORDER BY o.date DESC
               LIMIT 1""",
            (symbol, date_text),
        ).fetchall()
    conn.close()
    latest_by_symbol: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        latest_by_symbol.setdefault(item["symbol"], item)
    leaders = []
    for item in latest_by_symbol.values():
        leaders.append(
            {
                "symbol": item["symbol"],
                "name": item.get("name"),
                "display_name": _display_symbol(item["symbol"], item.get("name")),
                "date": item.get("date"),
                "close": item.get("close"),
                "change_pct": item.get("change_pct"),
                "amount": item.get("amount"),
                "volume": item.get("volume"),
                "note": "本地同板块样本候选，非确定龙头排名。",
            }
        )
    leaders.sort(key=lambda item: (item.get("amount") or 0), reverse=True)
    return {
        "board_name": board_name,
        "date": date_text,
        "items": leaders[:5],
        "quality": "medium" if len(leaders) > 1 else "low",
        "note": "第一版基于本地已同步股票和成交额筛选；样本不足时仅作参考。",
    }


def _stored_sector_relations(board_name: str) -> list[dict]:
    if not board_name:
        return []
    conn = get_conn()
    rows = conn.execute(
        """SELECT related_board_name, relation_type, reason, source
           FROM sector_relation_map
           WHERE base_board_name = ?
           ORDER BY relation_type, related_board_name""",
        (board_name,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _store_sector_relation_candidates(base_board_name: str, candidates: list[dict] | None, source: str = "llm_macro_chain") -> int:
    if not base_board_name or not candidates:
        return 0
    allowed = {"upstream", "downstream", "complementary", "substitute", "competitive"}
    rows = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        related = str(item.get("board_name") or item.get("related_board_name") or "").strip()
        relation_type = str(item.get("relation_type") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not related or not relation_type or relation_type not in allowed:
            continue
        if related == base_board_name:
            continue
        rows.append((base_board_name, industry_board_name(related), relation_type, reason, source, datetime.now().isoformat()))
    if not rows:
        return 0
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO sector_relation_map
           (base_board_name, related_board_name, relation_type, reason, source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def _sector_relations_payload(symbol: str, date_text: str, mapping: Optional[dict] = None) -> dict:
    mapping = mapping or _ensure_stock_industry(symbol)
    board = mapping.get("board_name") or mapping.get("industry_name") or ""
    conn = get_conn()
    price_row = conn.execute(
        "SELECT change_pct FROM ohlc WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (symbol, date_text),
    ).fetchone()
    conn.close()
    stock_change_pct = float(price_row["change_pct"] or 0) if price_row else 0
    related = []
    related_groups = []
    for relation in _stored_sector_relations(board):
        performance = _industry_change(relation["related_board_name"], date_text, stock_change_pct)
        companies = _sector_company_candidates(relation["related_board_name"], date_text, limit=10, context_symbol=symbol)
        relation_payload = {**relation, "performance": performance}
        related.append(relation_payload)
        related_groups.append(
            {
                "relation_type": relation["relation_type"],
                "relation_label": relation["relation_type"],
                "board_name": relation["related_board_name"],
                "reason": relation.get("reason"),
                "source": relation.get("source"),
                "performance": performance,
                "companies": companies.get("items", []),
                "companies_count": companies.get("count", 0),
                "quality": companies.get("quality", "low"),
                "note": companies.get("note"),
            }
        )
    base_companies = _sector_company_candidates(board, date_text, limit=12, context_symbol=symbol)
    return {
        "symbol": symbol,
        "date": date_text,
        "industry_name": mapping.get("industry_name"),
        "board_name": board,
        "base_performance": _industry_change(board, date_text, stock_change_pct),
        "base_companies": base_companies,
        "relations": related,
        "related_groups": related_groups,
        "leaders": _leader_candidates(board, date_text, symbol),
    }


def _hydrate_sector_relation_companies(symbol: str, date_text: str, max_companies: int = 12) -> dict:
    mapping = _ensure_stock_industry(symbol)
    board = mapping.get("board_name") or mapping.get("industry_name") or ""
    if not board:
        return {
            "symbol": symbol,
            "date": date_text,
            "attempted": 0,
            "hydrated": 0,
            "failed": [],
            "message": "暂无行业名称，无法补全同行行情。",
            "sector_relations": _sector_relations_payload(symbol, date_text, mapping=mapping),
        }

    candidate_symbols: list[str] = []
    for item in _sector_company_candidates(board, date_text, limit=max_companies, context_symbol=symbol).get("items", []):
        if item.get("symbol") and item["symbol"] not in candidate_symbols:
            candidate_symbols.append(item["symbol"])
    for relation in _stored_sector_relations(board):
        for item in _sector_company_candidates(relation["related_board_name"], date_text, limit=max_companies, context_symbol=symbol).get("items", []):
            if item.get("symbol") and item["symbol"] not in candidate_symbols:
                candidate_symbols.append(item["symbol"])

    attempted = 0
    hydrated = 0
    failed: list[dict] = []
    for candidate in candidate_symbols[:max_companies]:
        if _local_constituent_performance(candidate, date_text):
            continue
        attempted += 1
        perf = _ensure_constituent_ohlc(candidate, date_text)
        if perf:
            hydrated += 1
        else:
            failed.append({"symbol": candidate, "reason": "行情源暂无返回或网络不可用"})

    return {
        "symbol": symbol,
        "date": date_text,
        "attempted": attempted,
        "hydrated": hydrated,
        "failed": failed,
        "message": f"已尝试补全 {attempted} 只同行/同链公司，成功 {hydrated} 只。",
        "sector_relations": _sector_relations_payload(symbol, date_text, mapping=mapping),
    }


def _macro_sources_from_events(events: list[dict]) -> list[dict]:
    return [
        {
            "id": event.get("id"),
            "date": event.get("event_date"),
            "type": event.get("event_type"),
            "title": event.get("title"),
            "summary": event.get("summary"),
            "source": event.get("source"),
            "url": event.get("url"),
            "sentiment": event.get("sentiment"),
        }
        for event in events
    ]


def _local_macro_chain_payload(symbol: str, date_text: str, events: list[dict], sector_payload: dict) -> dict:
    name = _get_ticker_name(symbol)
    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event.get("event_type") or "news", []).append(event)
    policy = [event.get("title") for event in by_type.get("policy", [])[:4]]
    global_items = [event.get("title") for event in by_type.get("global_macro", [])[:3]]
    supply = by_type.get("supply_chain", []) + by_type.get("sector", [])
    board_name = sector_payload.get("board_name") or ""
    summary = "已整理本地行业表现和外部证据；请结合来源复核。"
    if policy or supply:
        summary = f"{_display_symbol(symbol, name)} 的联动研究已覆盖 {len(events)} 条证据，重点关注政策、产业链和行业表现。"
    return {
        "symbol": symbol,
        "name": name,
        "display_name": _display_symbol(symbol, name),
        "date": date_text,
        "available": True,
        "summary": summary,
        "policy_summary": policy,
        "global_summary": global_items,
        "supply_chain": {
            "upstream": [],
            "current_position": board_name,
            "downstream": [],
            "complementary": [],
            "substitute": [],
        },
        "sector_relations": sector_payload,
        "transmission_paths": [event.get("title") for event in supply[:5]],
        "watch_points": [
            "继续观察政策落地、订单公告、原材料成本和下游需求变化。",
            "板块龙头和同产业链行业若同步走强，联动证据更充分。",
        ],
        "risks": ["外部搜索覆盖有限，行业关系第一版为研究参考，不构成买卖建议。"],
        "sources": _macro_sources_from_events(events),
        "sources_count": len(events),
        "llm_used": False,
        "llm_error": None,
        "evidence_quality": "medium" if len(events) >= 3 else "low",
        "generated_at": datetime.now().isoformat(),
        "expires_at": _macro_cache_expiry(),
        "cached": False,
    }


def _llm_macro_chain_payload(base_payload: dict) -> tuple[dict, Optional[str]]:
    if not llm_configured():
        return base_payload, "LLM not configured"
    prompt = f"""你是A股研究助手。只能基于下面JSON里的来源、行情和行业数据，整理宏观与产业链联动研究，不要凭记忆补充事实，不要给买卖建议。
系统上下文：
{json.dumps(base_payload, ensure_ascii=False)}

输出JSON，字段：
{{
  "summary": "2-3句总览",
  "policy_summary": ["政策/监管/产业规划影响"],
  "global_summary": ["国际局势/汇率/关税/大宗商品影响，若无证据则写暂无直接证据"],
  "supply_chain": {{
    "upstream": ["上游成本或供应变化"],
    "current_position": "公司所在环节",
    "downstream": ["下游需求变化"],
    "complementary": ["互补行业及逻辑"],
    "substitute": ["互斥/替代/竞争行业及逻辑"]
  }},
  "relation_candidates": [
    {{"relation_type": "upstream/downstream/complementary/substitute/competitive", "board_name": "证据支持的A股行业板块名", "reason": "为什么相关，必须来自系统上下文里的来源或行情证据"}}
  ],
  "transmission_paths": ["可能传导路径"],
  "watch_points": ["后续观察点"],
  "risks": ["风险或证据不足"],
  "evidence_quality": "high/medium/low"
}}"""
    try:
        result = chat_json([{"role": "user", "content": prompt}], max_tokens=1800)
        if not result:
            return base_payload, "LLM returned empty result"
        merged = dict(base_payload)
        for key in ("summary", "policy_summary", "global_summary", "supply_chain", "relation_candidates", "transmission_paths", "watch_points", "risks", "evidence_quality"):
            if result.get(key):
                merged[key] = result[key]
        merged["llm_used"] = True
        merged["llm_error"] = None
        return merged, None
    except Exception as exc:
        payload = dict(base_payload)
        payload["llm_error"] = str(exc)
        return payload, str(exc)


def _macro_empty_payload(symbol: str, date_text: str, reason: str = "暂无联动研究缓存，请点击生成。") -> dict:
    name = _get_ticker_name(symbol)
    mapping = _ensure_stock_industry(symbol)
    return {
        "symbol": symbol,
        "name": name,
        "display_name": _display_symbol(symbol, name),
        "date": date_text,
        "available": False,
        "cached": False,
        "generated_at": None,
        "expires_at": None,
        "sources_count": 0,
        "llm_used": False,
        "summary": reason,
        "policy_summary": [],
        "global_summary": [],
        "supply_chain": {
            "upstream": [],
            "current_position": mapping.get("board_name") or mapping.get("industry_name") or "",
            "downstream": [],
            "complementary": [],
            "substitute": [],
        },
        "sector_relations": _sector_relations_payload(symbol, date_text, mapping=mapping),
        "transmission_paths": [],
        "watch_points": [],
        "risks": [],
        "sources": [],
        "evidence_quality": "low",
    }


def _macro_light_context(symbol: str, date_text: str) -> dict:
    cached = _macro_cache_row(symbol, date_text)
    if cached:
        return {
            "available": True,
            "cached": True,
            "generated_at": cached.get("generated_at"),
            "expires_at": cached.get("expires_at"),
            "sources_count": cached.get("sources_count", 0),
            "summary": cached.get("summary"),
            "policy_summary": (cached.get("policy_summary") or [])[:3],
            "global_summary": (cached.get("global_summary") or [])[:2],
            "transmission_paths": (cached.get("transmission_paths") or [])[:3],
            "watch_points": (cached.get("watch_points") or [])[:3],
            "evidence_quality": cached.get("evidence_quality", "low"),
        }
    return {
        "available": False,
        "cached": False,
        "sources_count": 0,
        "summary": "暂无宏观与产业链缓存，点击“联动研究”后可手动生成。",
        "policy_summary": [],
        "global_summary": [],
        "transmission_paths": [],
        "watch_points": [],
        "evidence_quality": "low",
    }


def _macro_related_events(symbol: str, start_date: str, end_date: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT *
           FROM market_events
           WHERE symbol = ?
             AND event_date >= ?
             AND event_date <= ?
             AND event_type IN ('policy', 'global_macro', 'sector', 'supply_chain', 'capital', 'news')
           ORDER BY event_date DESC, published_at DESC
           LIMIT 80""",
        (symbol, start_date, end_date),
    ).fetchall()
    conn.close()
    return [_event_row(row) for row in rows]


def _attach_fresh_sector_relations(payload: dict, symbol: str, date_text: str, mapping: Optional[dict] = None) -> dict:
    enriched = dict(payload)
    enriched["sector_relations"] = _sector_relations_payload(symbol, date_text, mapping=mapping)
    return enriched


def _build_macro_chain(
    symbol: str,
    date_text: str,
    *,
    refresh_cache: bool = False,
    use_llm: bool = True,
    use_search: bool = True,
) -> dict:
    norm = _norm(symbol)
    requested_date = _latest_trade_date(norm, date_text)
    cached = None if refresh_cache else _macro_cache_row(norm, requested_date)
    if cached:
        return _attach_fresh_sector_relations(cached, norm, requested_date)

    name = _get_ticker_name(norm)
    mapping = _ensure_stock_industry(norm)
    sector_payload = _sector_relations_payload(norm, requested_date, mapping=mapping)
    start_date = (datetime.fromisoformat(requested_date).date() - timedelta(days=45)).isoformat()
    warnings: list[str] = []

    discovered_events: list[dict] = []
    if use_search:
        try:
            result = discover_macro_chain_info(
                norm,
                name or "",
                industry=mapping.get("industry_name") or "",
                board=mapping.get("board_name") or "",
                start=start_date,
                end=requested_date,
                provider=settings.news_web_search_provider,
                max_results=max(settings.news_web_search_max_results, 10),
            )
            discovered_events = result.get("events") or []
            if discovered_events:
                _insert_events(norm, discovered_events, use_llm=False)
        except Exception as exc:
            warnings.append(f"外部联动资讯搜索失败：{exc}")
    else:
        warnings.append("已跳过外部搜索，仅使用本地缓存和行业行情。")

    events = _macro_related_events(norm, start_date, requested_date)
    base_payload = _local_macro_chain_payload(norm, requested_date, events, sector_payload)
    if warnings:
        base_payload["warnings"] = warnings
    if use_llm:
        base_payload, llm_error = _llm_macro_chain_payload(base_payload)
        if llm_error:
            base_payload["llm_error"] = llm_error
        else:
            _store_sector_relation_candidates(
                mapping.get("board_name") or mapping.get("industry_name") or "",
                base_payload.get("relation_candidates"),
            )
            base_payload["sector_relations"] = _sector_relations_payload(norm, requested_date, mapping=mapping)

    base_payload["available"] = True
    base_payload["sources_count"] = len(base_payload.get("sources") or [])
    base_payload["generated_at"] = base_payload.get("generated_at") or datetime.now().isoformat()
    base_payload["expires_at"] = base_payload.get("expires_at") or _macro_cache_expiry()
    _store_macro_cache(norm, requested_date, base_payload)
    return base_payload


@router.get("/{symbol}/sector-relations")
def get_sector_relations(symbol: str, date: Optional[str] = None):
    norm = _norm(symbol)
    target_date = _latest_trade_date(norm, date)
    return _sector_relations_payload(norm, target_date)


@router.post("/{symbol}/sector-relations/hydrate")
def hydrate_sector_relations(
    symbol: str,
    date: Optional[str] = None,
    max_companies: int = Query(12, ge=1, le=30),
):
    norm = _norm(symbol)
    target_date = _latest_trade_date(norm, date)
    return _hydrate_sector_relation_companies(norm, target_date, max_companies=max_companies)


@router.get("/{symbol}/macro-chain")
def get_macro_chain(symbol: str, date: Optional[str] = None):
    norm = _norm(symbol)
    target_date = _latest_trade_date(norm, date)
    cached = _macro_cache_row(norm, target_date)
    if cached:
        return _attach_fresh_sector_relations(cached, norm, target_date)
    return _macro_empty_payload(norm, target_date)


@router.post("/{symbol}/macro-chain/refresh")
def refresh_macro_chain(
    symbol: str,
    date: Optional[str] = None,
    refresh_cache: bool = False,
    use_llm: bool = True,
    use_search: bool = True,
):
    norm = _norm(symbol)
    target_date = _latest_trade_date(norm, date)
    return _build_macro_chain(norm, target_date, refresh_cache=refresh_cache, use_llm=use_llm, use_search=use_search)


def _generate_daily_reason(symbol: str, date: str, *, use_llm: bool = True) -> dict:
    norm = _norm(symbol)
    name = _get_ticker_name(norm)
    display_name = _display_symbol(norm, name)
    try:
        requested_date = datetime.fromisoformat(date).date().isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    conn = get_conn()
    day_row = conn.execute(
        """SELECT *
           FROM ohlc
           WHERE symbol = ? AND date = ?""",
        (norm, requested_date),
    ).fetchone()
    if not day_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol} on {requested_date}")

    prev_row = conn.execute(
        """SELECT *
           FROM ohlc
           WHERE symbol = ? AND date < ?
           ORDER BY date DESC
           LIMIT 1""",
        (norm, requested_date),
    ).fetchone()
    window_start = prev_row["date"] if prev_row else (datetime.fromisoformat(requested_date).date() - timedelta(days=7)).isoformat()
    event_rows = conn.execute(
        """SELECT *
           FROM market_events
           WHERE symbol = ? AND event_date >= ? AND event_date <= ?
           ORDER BY event_date DESC, published_at DESC
           LIMIT 100""",
        (norm, window_start, requested_date),
    ).fetchall()
    conn.close()

    day = dict(day_row)
    previous = dict(prev_row) if prev_row else None
    events = [_event_row(row) for row in event_rows]
    base_change = day.get("change_pct")
    if base_change is not None:
        stock_change_pct = float(base_change)
    elif previous and previous.get("close"):
        stock_change_pct = (float(day["close"]) - float(previous["close"])) / float(previous["close"]) * 100
    else:
        stock_change_pct = (float(day["close"]) - float(day["open"])) / float(day["open"]) * 100
    market_context = _build_market_context(norm, requested_date, stock_change_pct)
    fund_context = _build_fund_context(requested_date, stock_change_pct)
    industry_context = _build_industry_context(norm, requested_date, stock_change_pct)
    analyst_context = _build_analyst_context(norm, requested_date, float(day["close"] or 0))
    macro_chain_context = _macro_light_context(norm, requested_date)
    local = _daily_local_reason(
        name or display_name,
        day,
        previous,
        events,
        market_context,
        fund_context,
        industry_context,
        analyst_context,
    )

    price_summary = (
        f"日期 {requested_date}，开盘 {day['open']:.2f}，最高 {day['high']:.2f}，"
        f"最低 {day['low']:.2f}，收盘 {day['close']:.2f}，"
        f"涨跌幅 {local['change_pct']:+.2f}%，成交量 {day.get('volume') or 0:g}。"
    )
    event_context = _daily_event_context(events)
    background_context = _market_context_text(market_context, fund_context, industry_context, analyst_context)

    analysis = {
        "summary": local["summary"],
        "possible_reasons": local["possible_reasons"],
        "bullish_factors": local["bullish_factors"],
        "bearish_factors": local["bearish_factors"],
        "evidence_quality": local["evidence_quality"],
    }
    llm_used = False
    llm_error = None
    if use_llm and llm_configured():
        try:
            llm_result = analyze_daily_reason(
                symbol=display_name,
                date=requested_date,
                price_summary=price_summary,
                event_context=event_context,
                background_context=background_context,
                local_summary=local["summary"],
            )
            if llm_result:
                analysis = {
                    "summary": llm_result.get("summary") or analysis["summary"],
                    "possible_reasons": llm_result.get("possible_reasons") or analysis["possible_reasons"],
                    "bullish_factors": llm_result.get("bullish_factors") or analysis["bullish_factors"],
                    "bearish_factors": llm_result.get("bearish_factors") or analysis["bearish_factors"],
                    "evidence_quality": llm_result.get("evidence_quality") or analysis["evidence_quality"],
                    "model_consensus": llm_result.get("model_consensus"),
                    "model_disagreements": llm_result.get("model_disagreements") or [],
                }
                llm_used = True
                if llm_result.get("model_reviews"):
                    analysis["model_reviews"] = llm_result.get("model_reviews")
                if llm_result.get("analysis_mode"):
                    analysis["analysis_mode"] = llm_result.get("analysis_mode")
                if llm_result.get("reviewer_provider"):
                    analysis["reviewer_provider"] = llm_result.get("reviewer_provider")
        except Exception as exc:
            llm_error = str(exc)

    generated_at = datetime.now().isoformat()
    return {
        "symbol": norm,
        "name": name,
        "display_name": display_name,
        "date": requested_date,
        "price": day,
        "previous_close": previous["close"] if previous else None,
        "change_pct": local["change_pct"],
        "event_window": {"start": window_start, "end": requested_date},
        "events": events,
        "market_context": market_context,
        "fund_context": fund_context,
        "industry_context": industry_context,
        "analyst_context": analyst_context,
        "macro_chain_context": macro_chain_context,
        "analysis": analysis,
        "llm_used": llm_used,
        "llm_error": llm_error,
        "cached": False,
        "generated_at": generated_at,
        "data_coverage": _coverage_for_symbol(norm),
    }


def _cached_daily_reason(symbol: str, date: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload_json, generated_at FROM daily_reason_cache WHERE symbol = ? AND date = ?",
        (symbol, date),
    ).fetchone()
    conn.close()
    if not row:
        return None
    payload = _parse_json(row["payload_json"], {})
    if not payload:
        return None
    if "analyst_context" not in payload:
        return None
    if "macro_chain_context" not in payload:
        payload["macro_chain_context"] = _macro_light_context(symbol, date)
    payload["cached"] = True
    payload["generated_at"] = row["generated_at"]
    return _apply_display_name(payload)


def _store_daily_reason(payload: dict) -> None:
    symbol = payload["symbol"]
    date = payload["date"]
    generated_at = payload.get("generated_at") or datetime.now().isoformat()
    to_store = dict(payload)
    to_store["cached"] = False
    to_store["generated_at"] = generated_at
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO daily_reason_cache
           (symbol, date, payload_json, llm_used, generated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (symbol, date, json.dumps(to_store, ensure_ascii=False), 1 if payload.get("llm_used") else 0, generated_at),
    )
    conn.commit()
    conn.close()


def _clear_daily_reason_cache(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> None:
    conn = get_conn()
    query = "DELETE FROM daily_reason_cache WHERE symbol = ?"
    params: list = [symbol]
    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)
    conn.execute(query, params)
    conn.commit()
    conn.close()


def _warm_daily_reason_cache(symbol: str, start_date: str, end_date: str, *, limit: int = 60) -> dict:
    conn = get_conn()
    rows = conn.execute(
        """SELECT date FROM ohlc
           WHERE symbol = ? AND date >= ? AND date <= ?
           ORDER BY date DESC
           LIMIT ?""",
        (symbol, start_date, end_date, limit),
    ).fetchall()
    conn.close()
    generated = 0
    failed = 0
    for row in rows:
        try:
            payload = _generate_daily_reason(symbol, row["date"], use_llm=False)
            _store_daily_reason(payload)
            generated += 1
        except Exception as exc:
            logger.debug("Daily reason warmup failed for %s %s: %s", symbol, row["date"], exc)
            failed += 1
    return {"generated": generated, "failed": failed, "limit": limit}


@router.get("/{symbol}/daily-reason")
def get_daily_reason(
    symbol: str,
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    use_llm: Optional[bool] = None,
    force_llm: bool = False,
    refresh_cache: bool = False,
):
    norm = _norm(symbol)
    try:
        requested_date = datetime.fromisoformat(date).date().isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    policy = llm_date_policy(requested_date, use_llm=use_llm, force_llm=force_llm)
    cached = None if refresh_cache else _cached_daily_reason(norm, requested_date)
    if cached and not (force_llm and not cached.get("llm_used")):
        cached["llm_policy"] = {**policy, "cache_hit": True}
        return cached
    payload = _generate_daily_reason(norm, requested_date, use_llm=policy["use_llm"])
    payload["llm_policy"] = {**policy, "cache_hit": False}
    _store_daily_reason(payload)
    return payload


@router.get("/{symbol}/daily-reasons")
def get_daily_reasons(
    symbol: str,
    start: str = Query(...),
    end: str = Query(...),
    max_generate: int = Query(30, ge=0, le=120),
):
    norm = _norm(symbol)
    conn = get_conn()
    rows = conn.execute(
        """SELECT date FROM ohlc
           WHERE symbol = ? AND date >= ? AND date <= ?
           ORDER BY date ASC""",
        (norm, start, end),
    ).fetchall()
    conn.close()
    payloads: dict[str, dict] = {}
    generated = 0
    for row in reversed(rows):
        date = row["date"]
        payload = _cached_daily_reason(norm, date)
        if not payload:
            if generated >= max_generate:
                continue
            payload = _generate_daily_reason(norm, date, use_llm=False)
            _store_daily_reason(payload)
            generated += 1
        payloads[date] = payload

    items = []
    for row in rows:
        date = row["date"]
        payload = payloads.get(date) or _cached_daily_reason(norm, date) or {}
        items.append(
            {
                "symbol": norm,
                "date": date,
                "change_pct": payload.get("change_pct"),
                "event_count": len(payload.get("events") or []),
                "summary": (payload.get("analysis") or {}).get("summary"),
                "evidence_quality": (payload.get("analysis") or {}).get("evidence_quality"),
                "cached": bool(payload.get("cached")),
                "generated_at": payload.get("generated_at"),
            }
        )
    return {"symbol": norm, "start": start, "end": end, "count": len(items), "generated": generated, "max_generate": max_generate, "items": items}


@router.get("/{symbol}/coverage")
def get_coverage(symbol: str):
    return _coverage_for_symbol(_norm(symbol))


def _refresh_web_info(
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = None,
    use_llm: bool = False,
    refresh_cache: bool = False,
):
    norm = _norm(symbol)
    name = _get_ticker_name(norm) or norm
    start_date, end_date = _default_dates(start, end)
    try:
        search_result = discover_external_info(
            norm,
            name,
            start=start_date,
            end=end_date,
            provider=provider,
            max_results=max_results,
            refresh_cache=refresh_cache,
        )
        events = search_result["events"]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"外部资讯搜索失败：{exc}") from exc
    result = _insert_events(norm, events, use_llm=use_llm) if events else {"inserted": 0}
    _clear_daily_reason_cache(norm, start_date, end_date)
    by_type: dict[str, int] = {}
    for event in events:
        by_type[event.get("event_type") or "news"] = by_type.get(event.get("event_type") or "news", 0) + 1
    return {
        "symbol": norm,
        "name": name,
        "display_name": _display_symbol(norm, name),
        "provider": search_result.get("provider") or provider or settings.news_web_search_provider,
        "start": start_date,
        "end": end_date,
        "found": len(events),
        "inserted": result.get("inserted", 0),
        "cached": bool(search_result.get("cached")),
        "by_type": by_type,
        "coverage": _coverage_for_symbol(norm),
        "events": events,
    }


@router.post("/{symbol}/refresh-web-info")
def refresh_web_info(
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = Query(None, ge=1, le=20),
    use_llm: bool = False,
    refresh_cache: bool = False,
):
    return _refresh_web_info(symbol, start, end, provider, max_results, use_llm, refresh_cache)


@router.post("/{symbol}/refresh-web-news")
def refresh_web_news(
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = Query(None, ge=1, le=20),
    use_llm: bool = False,
    refresh_cache: bool = False,
):
    return _refresh_web_info(symbol, start, end, provider, max_results, use_llm, refresh_cache)


def sync_watchlist_job(*, stale_only: bool = True, max_age_days: int = 1, limit: Optional[int] = None, use_llm: bool = False) -> dict:
    conn = get_conn()
    rows = conn.execute(
        """SELECT symbol, name, last_ohlc_fetch, last_news_fetch
           FROM tickers
           WHERE last_ohlc_fetch IS NOT NULL
           ORDER BY symbol"""
    ).fetchall()
    conn.close()

    candidates = [dict(row) for row in rows]
    if limit is not None:
        candidates = candidates[:limit]

    results = []
    skipped = []
    started_at = datetime.now().isoformat()
    for item in candidates:
        symbol = item["symbol"]
        coverage = _coverage_for_symbol(symbol)
        if stale_only and not _should_sync_from_coverage(coverage, max_age_days):
            skipped.append(
                {
                    "symbol": symbol,
                    "name": item.get("name"),
                    "display_name": _display_symbol(symbol, item.get("name")),
                    "reason": "行情已是最近数据",
                    "coverage": coverage,
                }
            )
            continue
        try:
            result = sync_symbol(symbol, use_llm=use_llm)
            results.append(result)
        except Exception as exc:
            logger.exception("Watchlist sync failed for %s", symbol)
            results.append(
                {
                    "symbol": symbol,
                    "name": item.get("name"),
                    "display_name": _display_symbol(symbol, item.get("name")),
                    "status": "failed",
                    "error": str(exc),
                    "coverage": coverage,
                }
            )

    status = "success"
    if any(result.get("status") == "failed" for result in results):
        status = "partial_success" if any(result.get("status") != "failed" for result in results) or skipped else "failed"
    return {
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "total": len(candidates),
        "synced": len(results),
        "skipped": len(skipped),
        "results": results,
        "skipped_items": skipped,
    }


def _recent_event_summary(symbol: str, end_date: str, window_days: int) -> dict:
    start_date = (datetime.fromisoformat(end_date).date() - timedelta(days=window_days)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT event_type, sentiment, title, event_date, source
           FROM market_events
           WHERE symbol = ? AND event_date >= ? AND event_date <= ?
           ORDER BY event_date DESC, published_at DESC
           LIMIT 80""",
        (symbol, start_date, end_date),
    ).fetchall()
    conn.close()
    items = [dict(row) for row in rows]
    positive = sum(1 for item in items if item.get("sentiment") == "positive")
    negative = sum(1 for item in items if item.get("sentiment") == "negative")
    return {
        "start": start_date,
        "end": end_date,
        "count": len(items),
        "positive": positive,
        "negative": negative,
        "neutral": len(items) - positive - negative,
        "by_type": {
            "news": sum(1 for item in items if item.get("event_type") == "news"),
            "capital": sum(1 for item in items if item.get("event_type") == "capital"),
            "announcement": sum(1 for item in items if item.get("event_type") == "announcement"),
            "financial_report": sum(1 for item in items if item.get("event_type") == "financial_report"),
        },
        "top_events": items[:8],
    }


def _latest_price_context(symbol: str, requested_date: Optional[str]) -> dict:
    conn = get_conn()
    if requested_date:
        row = conn.execute(
            "SELECT * FROM ohlc WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, requested_date),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM ohlc WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    prev = None
    if row:
        prev = conn.execute(
            "SELECT * FROM ohlc WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
            (symbol, row["date"]),
        ).fetchone()
    rows_30 = conn.execute(
        """SELECT date, close, change_pct, volume
           FROM ohlc
           WHERE symbol = ? AND date <= ?
           ORDER BY date DESC
           LIMIT 30""",
        (symbol, row["date"] if row else requested_date or datetime.now().date().isoformat()),
    ).fetchall()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol}")
    closes = [float(item["close"]) for item in reversed(rows_30)]
    ret_20 = None
    if len(closes) >= 20 and closes[0]:
        ret_20 = (closes[-1] / closes[-20] - 1) * 100
    change_pct = row["change_pct"]
    if change_pct is None and prev and prev["close"]:
        change_pct = (float(row["close"]) / float(prev["close"]) - 1) * 100
    return {
        "date": row["date"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "change_pct": round(float(change_pct or 0), 2),
        "volume": row["volume"],
        "ret_20d_pct": round(ret_20, 2) if ret_20 is not None else None,
    }


def _local_signal_reference(symbol: str, req: SignalReferenceRequest) -> dict:
    name = _get_ticker_name(symbol)
    display_name = _display_symbol(symbol, name)
    price = _latest_price_context(symbol, req.date)
    event_summary = _recent_event_summary(symbol, price["date"], req.window_days)
    try:
        from backend.ml.similar import find_similar_days

        similar = find_similar_days(symbol, price["date"], req.top_k)
    except Exception as exc:
        similar = {"error": str(exc), "similar_days": [], "stats": {"count": 0}}

    market_context = _build_market_context(symbol, price["date"], price["change_pct"])
    fund_context = _build_fund_context(price["date"], price["change_pct"])
    industry_context = _build_industry_context(symbol, price["date"], price["change_pct"])
    analyst_context = _build_analyst_context(symbol, price["date"], float(price["close"] or 0))
    coverage = _coverage_for_symbol(symbol)

    stats = similar.get("stats") or {}
    reasons = []
    if stats.get("count"):
        reasons.append(f"找到 {stats.get('count')} 个相似历史日期，可作为统计参照。")
    else:
        reasons.append("相似历史样本不足，统计参考较弱。")
    if event_summary["count"]:
        reasons.append(
            f"近 {req.window_days} 天有 {event_summary['count']} 条事件，"
            f"利好 {event_summary['positive']} 条、利空 {event_summary['negative']} 条。"
        )
    else:
        reasons.append(f"近 {req.window_days} 天未匹配到足够事件，需更多依赖行情和市场背景。")
    if market_context.get("available"):
        reasons.append(
            f"当前个股相对 {market_context.get('index_name')} 的关系为 {market_context.get('relationship')}。"
        )
    if industry_context.get("available"):
        reasons.append(
            f"相对行业板块的关系为 {industry_context.get('relationship')}。"
        )
    if analyst_context.get("available"):
        latest = analyst_context.get("latest") or {}
        reasons.append(f"最新机构评级参考：{latest.get('institution') or '机构'} {latest.get('rating') or '未披露评级'}。")

    evidence_quality = "high" if stats.get("count", 0) >= 5 and event_summary["count"] >= 5 else "medium" if stats.get("count") or event_summary["count"] else "low"
    return {
        "symbol": symbol,
        "name": name,
        "display_name": display_name,
        "date": price["date"],
        "window_days": req.window_days,
        "price": price,
        "event_summary": event_summary,
        "similar_days": similar,
        "market_context": market_context,
        "fund_context": fund_context,
        "industry_context": industry_context,
        "analyst_context": analyst_context,
        "coverage": coverage,
        "local_view": {
            "summary": f"{display_name} 的信号参考基于相似历史、近期事件、市场/行业背景和机构预期，不构成买卖建议。",
            "watch_points": reasons,
            "risk_points": [
                "该面板不使用未重训的 A 股强预测模型。",
                "样本较少或新闻覆盖不足时，统计参考会明显变弱。",
                "LLM 只做情景推演，不新增事实。",
            ],
            "evidence_quality": evidence_quality,
        },
    }


def _llm_signal_scenarios(payload: dict, question: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    if not llm_configured():
        return None, None
    context = {
        "symbol": payload["display_name"],
        "date": payload["date"],
        "price": payload["price"],
        "event_summary": payload["event_summary"],
        "similar_stats": (payload.get("similar_days") or {}).get("stats"),
        "market_context": payload.get("market_context"),
        "industry_context": payload.get("industry_context"),
        "analyst_context": {
            "available": (payload.get("analyst_context") or {}).get("available"),
            "latest": (payload.get("analyst_context") or {}).get("latest"),
            "target_upside_pct": (payload.get("analyst_context") or {}).get("target_upside_pct"),
        },
        "local_view": payload["local_view"],
        "question": question,
    }
    prompt = f"""你是 A 股研究助手。请只基于下面系统上下文，生成未来 1/5/10 个交易日的情景推演和观察点，不要给买卖建议，不要写确定预测。
系统上下文:
{json.dumps(context, ensure_ascii=False)}

输出 JSON:
{{
  "summary": "1-2 句克制总结",
  "scenarios": [
    {{"horizon": "T+1", "base_case": "基准情景", "upside_watch": "上行情景观察点", "downside_watch": "下行情景风险点"}},
    {{"horizon": "T+5", "base_case": "...", "upside_watch": "...", "downside_watch": "..."}},
    {{"horizon": "T+10", "base_case": "...", "upside_watch": "...", "downside_watch": "..."}}
  ],
  "do_not_trade_on": ["不能单独作为交易依据的原因"],
  "evidence_quality": "high/medium/low"
}}"""
    try:
        return chat_json([{"role": "user", "content": prompt}], max_tokens=1400), None
    except Exception as exc:
        return None, str(exc)


def _latest_trade_date(symbol: str, requested: Optional[str] = None) -> str:
    conn = get_conn()
    if requested:
        row = conn.execute(
            """SELECT date FROM ohlc
               WHERE symbol = ? AND date <= ?
               ORDER BY date DESC
               LIMIT 1""",
            (symbol, requested),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT date FROM ohlc
               WHERE symbol = ?
               ORDER BY date DESC
               LIMIT 1""",
            (symbol,),
        ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol}. Please sync this stock first.")
    return row["date"]


def _stock_report_context(symbol: str, request: StockReportRequest) -> dict:
    norm = _norm(symbol)
    name = _get_ticker_name(norm)
    display_name = _display_symbol(norm, name)
    report_date = _latest_trade_date(norm, request.date)
    start_date = (datetime.fromisoformat(report_date).date() - timedelta(days=max(30, min(request.lookback_days, 366)))).isoformat()

    conn = get_conn()
    ohlc_rows = [dict(row) for row in conn.execute(
        """SELECT date, open, high, low, close, volume, amount, turnover_rate, change_pct, amplitude
           FROM ohlc
           WHERE symbol = ? AND date >= ? AND date <= ?
           ORDER BY date ASC""",
        (norm, start_date, report_date),
    ).fetchall()]
    event_rows = conn.execute(
        """SELECT *
           FROM market_events
           WHERE symbol = ? AND event_date >= ? AND event_date <= ?
           ORDER BY event_date DESC, published_at DESC
           LIMIT 80""",
        (norm, start_date, report_date),
    ).fetchall()
    financial_rows = [dict(row) for row in conn.execute(
        """SELECT announcement_date, report_period, revenue, net_profit, non_gaap_net_profit,
                  operating_cash_flow, roe, yoy_revenue, yoy_net_profit
           FROM financial_reports
           WHERE symbol = ?
           ORDER BY report_period DESC
           LIMIT 8""",
        (norm,),
    ).fetchall()]
    conn.close()

    if not ohlc_rows:
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol}. Please sync this stock first.")

    latest = ohlc_rows[-1]
    first = ohlc_rows[0]
    ret_window = (float(latest["close"]) - float(first["close"])) / float(first["close"]) * 100 if first.get("close") else None
    ret_20d = None
    if len(ohlc_rows) > 20 and ohlc_rows[-21].get("close"):
        ret_20d = (float(latest["close"]) - float(ohlc_rows[-21]["close"])) / float(ohlc_rows[-21]["close"]) * 100

    stock_change_pct = float(latest.get("change_pct") or 0)
    market_context = _build_market_context(norm, report_date, stock_change_pct)
    fund_context = _build_fund_context(report_date, stock_change_pct)
    industry_context = _build_industry_context(norm, report_date, stock_change_pct)
    analyst_context = _build_analyst_context(norm, report_date, float(latest["close"] or 0))
    macro_chain_context = _macro_light_context(norm, report_date)
    events = [_event_row(row) for row in event_rows]

    positive = [event for event in events if event.get("sentiment") == "positive"]
    negative = [event for event in events if event.get("sentiment") == "negative"]
    by_type: dict[str, int] = {}
    for event in events:
        event_type = event.get("event_type") or "news"
        by_type[event_type] = by_type.get(event_type, 0) + 1

    return {
        "symbol": norm,
        "name": name,
        "display_name": display_name,
        "date": report_date,
        "start_date": start_date,
        "price": {
            "latest": latest,
            "return_window_pct": round(ret_window, 2) if ret_window is not None else None,
            "return_20d_pct": round(ret_20d, 2) if ret_20d is not None else None,
        },
        "event_summary": {
            "count": len(events),
            "positive": len(positive),
            "negative": len(negative),
            "by_type": by_type,
            "recent_events": [
                {
                    "date": event.get("event_date"),
                    "type": event.get("event_type"),
                    "sentiment": event.get("sentiment"),
                    "title": event.get("title"),
                    "summary": event.get("summary"),
                }
                for event in events[:25]
            ],
        },
        "financial_reports": financial_rows,
        "market_context": market_context,
        "fund_context": fund_context,
        "industry_context": industry_context,
        "analyst_context": analyst_context,
        "macro_chain_context": macro_chain_context,
        "data_coverage": _coverage_for_symbol(norm),
    }


def _local_stock_report(context: dict) -> dict:
    display_name = context["display_name"]
    price = context["price"]
    latest = price["latest"]
    event_summary = context["event_summary"]
    reasons = [
        f"最新交易日 {context['date']} 收盘 {float(latest['close']):.2f} 元，近 20 日涨跌幅 {price.get('return_20d_pct')}%。",
        f"近 {context['start_date']} 至 {context['date']} 区间匹配到 {event_summary['count']} 条事件，其中利好 {event_summary['positive']} 条、利空 {event_summary['negative']} 条。",
    ]
    industry = context.get("industry_context") or {}
    if industry.get("available"):
        reasons.append(
            f"行业背景：{industry.get('board_name')} 当日涨跌幅 {industry.get('industry_change_pct'):+.2f}%，"
            f"个股相对行业 {industry.get('excess_return_pct'):+.2f}%。"
        )
    return {
        "summary": f"{display_name} 的本地报告已整理行情、事件、行业、市场和机构预期，建议结合右侧详细项继续复核。",
        "current_status": reasons,
        "future_potential": ["需要观察业绩兑现、行业景气、资金持续性和公告后续进展。"],
        "positive_factors": [event["title"] for event in context["event_summary"]["recent_events"] if event.get("sentiment") == "positive"][:5],
        "risk_factors": [event["title"] for event in context["event_summary"]["recent_events"] if event.get("sentiment") == "negative"][:5],
        "catalysts": ["后续公告、财报披露、行业政策、资金流向变化。"],
        "watch_points": reasons,
        "evidence_quality": "medium" if event_summary["count"] else "low",
        "disclaimer": "这是研究报告草稿，不构成买卖建议。",
    }


def _llm_stock_report(context: dict, question: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    if not llm_configured():
        return None, "LLM not configured"
    focus = f"\n用户额外问题：{question}\n" if question else ""
    prompt = f"""你是 A 股投研助手。请只基于系统提供的数据，分析这只股票的现状和未来潜力，不要凭记忆补充外部事实，不要给买卖建议。
系统上下文:
{json.dumps(context, ensure_ascii=False)}
{focus}
请重点回答：
1. 当前股价和事件反映出的状态。
2. 基本面、行业、资金、机构预期中可能支持未来潜力的因素。
3. 主要风险和证据不足处。
4. 未来 1-3 个月值得观察的催化剂和验证点。

输出 JSON:
{{
  "summary": "2-3 句中文总览",
  "current_status": ["现状判断1", "现状判断2"],
  "future_potential": ["潜力因素1", "潜力因素2"],
  "positive_factors": ["正面因素"],
  "risk_factors": ["风险因素"],
  "catalysts": ["后续催化/验证点"],
  "watch_points": ["需要继续观察的指标或事件"],
  "evidence_quality": "high/medium/low",
  "disclaimer": "克制的非投资建议说明"
}}"""
    try:
        return chat_json([{"role": "user", "content": prompt}], max_tokens=1800), None
    except Exception as exc:
        return None, str(exc)


def _stock_report_response_context(context: dict) -> dict:
    return {
        "price": context["price"],
        "event_summary": context["event_summary"],
        "financial_reports": context["financial_reports"][:4],
        "market_context": context["market_context"],
        "fund_context": context["fund_context"],
        "industry_context": context["industry_context"],
        "analyst_context": context["analyst_context"],
        "macro_chain_context": context.get("macro_chain_context"),
        "data_coverage": context["data_coverage"],
    }


def _cached_stock_report_by_day(symbol: str, report_date: str) -> Optional[dict]:
    if not settings.llm_cache_enabled:
        return None
    now = datetime.now().isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT payload_json, llm_used, created_at, expires_at, meta_json
           FROM analysis_cache
           WHERE cache_type = 'stock_report'
             AND (expires_at IS NULL OR expires_at >= ?)
           ORDER BY created_at DESC
           LIMIT 80""",
        (now,),
    ).fetchall()
    conn.close()
    for row in rows:
        meta = _parse_json(row["meta_json"], {})
        if meta.get("symbol") != symbol or meta.get("date") != report_date:
            continue
        payload = _parse_json(row["payload_json"], {})
        if not payload.get("report") or not payload.get("context"):
            continue
        return {
            "payload": payload,
            "llm_used": bool(row["llm_used"]),
            "cache": {
                "hit": True,
                "type": "stock_report",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "meta": meta,
            },
        }
    return None


@router.post("/sync-watchlist")
def sync_watchlist(
    stale_only: bool = True,
    max_age_days: int = Query(1, ge=0, le=30),
    limit: Optional[int] = Query(None, ge=1, le=200),
    use_llm: bool = False,
):
    return sync_watchlist_job(stale_only=stale_only, max_age_days=max_age_days, limit=limit, use_llm=use_llm)


@router.post("/{symbol}/signal-reference")
def signal_reference(symbol: str, req: Optional[SignalReferenceRequest] = Body(default=None)):
    norm = _norm(symbol)
    request = req or SignalReferenceRequest()
    request.window_days = max(7, min(request.window_days, 90))
    request.top_k = max(3, min(request.top_k, 30))
    payload = _local_signal_reference(norm, request)
    policy = llm_date_policy(payload.get("date"), use_llm=request.use_llm, force_llm=request.force_llm)
    cache_parts = {
        "symbol": norm,
        "date": payload.get("date"),
        "window_days": request.window_days,
        "top_k": request.top_k,
        "question": request.question or "",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "fallback_provider": settings.llm_fallback_provider,
        "fallback_model": settings.llm_fallback_model,
        "analysis_mode": settings.llm_analysis_mode,
        "context_hash": stable_hash(
            {
                "price": payload.get("price"),
                "event_summary": payload.get("event_summary"),
                "similar_stats": (payload.get("similar_days") or {}).get("stats"),
                "market_context": payload.get("market_context"),
                "industry_context": payload.get("industry_context"),
                "analyst_latest": (payload.get("analyst_context") or {}).get("latest"),
            }
        ),
    }
    cached_signal = None if request.refresh_cache else get_analysis_cache("signal_reference", cache_parts)
    if cached_signal:
        llm_result = cached_signal.get("scenario_analysis")
        llm_error = cached_signal.get("llm_error")
        cache_meta = cached_signal.get("cache")
    elif policy["use_llm"]:
        llm_result, llm_error = _llm_signal_scenarios(payload, request.question)
        cache_meta = {"hit": False, "type": "signal_reference"}
        if llm_result:
            store_analysis_cache(
                "signal_reference",
                cache_parts,
                {"scenario_analysis": llm_result, "llm_error": llm_error},
                ttl_hours=settings.llm_signal_reference_cache_ttl_hours,
                llm_used=True,
                meta={"symbol": norm, "date": payload.get("date")},
            )
    else:
        llm_result, llm_error = None, None
        cache_meta = {"hit": False, "type": "signal_reference"}
    payload["llm_used"] = bool(llm_result)
    payload["llm_error"] = llm_error
    payload["llm_policy"] = policy
    payload["llm_cache"] = cache_meta
    payload["scenario_analysis"] = llm_result or {
        "summary": payload["local_view"]["summary"],
        "scenarios": [
            {
                "horizon": "T+1",
                "base_case": "短线主要观察当日事件延续、成交量和大盘方向。",
                "upside_watch": "若放量并继续跑赢指数，说明资金承接较强。",
                "downside_watch": "若缩量下跌或利空事件发酵，短线风险偏高。",
            },
            {
                "horizon": "T+5",
                "base_case": "一周维度关注事件是否被更多公告、新闻或财报数据验证。",
                "upside_watch": "利好事件获得基本面或资金面确认时，走势可能更稳。",
                "downside_watch": "若相似历史样本表现偏弱，应降低统计参考权重。",
            },
            {
                "horizon": "T+10",
                "base_case": "两周维度更依赖行业、大盘和基本面预期是否共振。",
                "upside_watch": "行业和指数同步改善时，个股预期更容易延续。",
                "downside_watch": "若行业走弱或新闻覆盖不足，归因和情景推演可信度下降。",
            },
        ],
        "do_not_trade_on": ["这是统计参考和情景推演，不是买卖建议。"],
        "evidence_quality": payload["local_view"]["evidence_quality"],
    }
    return payload


@router.post("/{symbol}/stock-report")
def stock_report(symbol: str, req: Optional[StockReportRequest] = Body(default=None)):
    norm = _norm(symbol)
    request = req or StockReportRequest()
    request.lookback_days = max(30, min(request.lookback_days, 366))
    report_date = _latest_trade_date(norm, request.date)
    quick_cached = None if request.refresh_cache else _cached_stock_report_by_day(norm, report_date)
    if quick_cached:
        payload = quick_cached["payload"]
        return {
            "symbol": norm,
            "name": payload.get("name"),
            "display_name": payload.get("display_name") or _display_symbol(norm, payload.get("name") or _get_ticker_name(norm)),
            "date": payload.get("date") or report_date,
            "start_date": payload.get("start_date"),
            "report": payload["report"],
            "context": payload["context"],
            "llm_used": quick_cached["llm_used"],
            "llm_error": payload.get("llm_error"),
            "cache": quick_cached["cache"],
        }
    context = _stock_report_context(norm, request)
    local_report = _local_stock_report(context)
    response_context = _stock_report_response_context(context)
    cache_parts = {
        "symbol": norm,
        "date": context["date"],
        "lookback_days": request.lookback_days,
        "question": request.question or "",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "context_hash": stable_hash(
            {
                "price": context.get("price"),
                "event_summary": context.get("event_summary"),
                "financial_reports": context.get("financial_reports"),
                "market_context": context.get("market_context"),
                "industry_context": context.get("industry_context"),
                "analyst_latest": (context.get("analyst_context") or {}).get("latest"),
                "macro_chain_context": context.get("macro_chain_context"),
            }
        ),
    }
    cached = None if request.refresh_cache else get_analysis_cache("stock_report", cache_parts)
    if cached:
        report = cached.get("report") or local_report
        llm_used = bool(cached.get("llm_used", True))
        llm_error = cached.get("llm_error")
        cache_meta = cached.get("cache")
        response_context = cached.get("context") or response_context
    elif request.force_llm:
        report, llm_error = _llm_stock_report(context, request.question)
        llm_used = bool(report)
        if not report:
            report = local_report
        cache_meta = {"hit": False, "type": "stock_report"}
        if llm_used:
            store_analysis_cache(
                "stock_report",
                cache_parts,
                {
                    "symbol": norm,
                    "name": context.get("name"),
                    "display_name": context["display_name"],
                    "date": context["date"],
                    "start_date": context["start_date"],
                    "report": report,
                    "context": response_context,
                    "llm_error": llm_error,
                    "llm_used": llm_used,
                },
                ttl_hours=settings.llm_stock_report_cache_ttl_hours,
                llm_used=True,
                meta={"symbol": norm, "date": context["date"]},
            )
    else:
        report = local_report
        llm_used = False
        llm_error = None
        cache_meta = {"hit": False, "type": "stock_report"}

    return {
        "symbol": norm,
        "name": context.get("name"),
        "display_name": context["display_name"],
        "date": context["date"],
        "start_date": context["start_date"],
        "report": report,
        "context": response_context,
        "llm_used": llm_used,
        "llm_error": llm_error,
        "cache": cache_meta,
    }


@router.post("/{symbol}/sync")
def sync(symbol: str, start: Optional[str] = None, end: Optional[str] = None, use_llm: bool = False):
    return sync_symbol(symbol, start, end, use_llm=use_llm)


@router.post("/{symbol}/analyze")
def analyze(symbol: str, req: AnalyzeRequest):
    from backend.api.routers.analysis import analyze_symbol_range

    return analyze_symbol_range(
        _norm(symbol),
        req.start_date,
        req.end_date,
        req.question,
        use_llm=req.use_llm,
        force_llm=req.force_llm,
        refresh_cache=req.refresh_cache,
    )


@router.post("")
def add_ticker(req: AddTickerRequest, background_tasks: BackgroundTasks):
    norm = _norm(req.symbol)
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO tickers (symbol, name, market) VALUES (?, ?, ?)",
        (norm, req.name or get_stock_name(norm) or norm, get_market(norm) if _is_ashare(norm) else None),
    )
    conn.commit()
    conn.close()
    background_tasks.add_task(sync_symbol, norm)
    return {"symbol": norm, "status": "added", "message": "A-share data sync started"}
