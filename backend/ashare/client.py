"""AKShare-backed data access for the A-share v1 research workflow."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from backend.ashare.symbol import get_limit_pct, get_market, normalize, to_akshare

logger = logging.getLogger(__name__)

_STOCK_LIST_CACHE: Optional[pd.DataFrame] = None
_STOCK_LIST_TIME = 0.0
_STOCK_LIST_TTL = 3600 * 6

MARKET_INDEXES = {
    "sh000001": {"ak_symbol": "000001", "em_symbol": "sh000001", "name": "上证指数"},
    "sz399001": {"ak_symbol": "399001", "em_symbol": "sz399001", "name": "深证成指"},
    "sz399006": {"ak_symbol": "399006", "em_symbol": "sz399006", "name": "创业板指"},
}

INDUSTRY_BOARD_ALIASES = {
    "白酒": "白酒",
    "白酒Ⅱ": "白酒",
    "银行Ⅱ": "银行",
    "证券Ⅱ": "证券",
    "保险Ⅱ": "保险",
}

EM_INDUSTRY_BOARD_ALIASES = {
    "白酒": "酿酒行业",
}

STOCK_PROFILE_FALLBACKS = {
    "sh600519": {"name": "贵州茅台", "industry_name": "白酒Ⅱ", "board_name": "白酒"},
    "sz000001": {"name": "平安银行", "industry_name": "银行Ⅱ", "board_name": "银行"},
    "sz002339": {"name": "积成电子", "industry_name": "电网设备", "board_name": "电网设备"},
}


def _get_ak():
    # AKShare uses requests internally. On some Windows hosts inherited proxy
    # settings break domestic endpoints, so v1 opts out of proxies by default.
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(key, None)
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")
    import akshare as ak

    return ak


def _value(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            value = row.get(name)
        except AttributeError:
            value = None
        if value is not None and value == value:
            return value
    return default


def _text(row: Any, *names: str, default: str = "") -> str:
    return str(_value(row, *names, default=default) or "").strip()


def _float(row: Any, *names: str, default: float = 0.0) -> float:
    value = _value(row, *names, default=default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if not text:
        return ""
    if " " in text:
        return text.split(" ", 1)[0]
    return text[:10]


def _published_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    text = str(value or "").strip()
    if len(text) == 10:
        return f"{text}T00:00:00"
    return text


def _event_id(symbol: str, event_type: str, title: str, event_date: str, url: str = "") -> str:
    raw = f"{normalize(symbol)}:{event_type}:{event_date}:{title}:{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_stock_list() -> pd.DataFrame:
    global _STOCK_LIST_CACHE, _STOCK_LIST_TIME
    now = time.time()
    if _STOCK_LIST_CACHE is not None and now - _STOCK_LIST_TIME < _STOCK_LIST_TTL:
        return _STOCK_LIST_CACHE

    ak = _get_ak()
    try:
        df = ak.stock_zh_a_spot_em()
        _STOCK_LIST_CACHE = df[["代码", "名称"]].copy()
        _STOCK_LIST_TIME = now
        return _STOCK_LIST_CACHE
    except Exception as exc:
        logger.warning("Failed to fetch A-share stock list: %s", exc)
    try:
        df = ak.stock_info_a_code_name()
        _STOCK_LIST_CACHE = df[["code", "name"]].rename(columns={"code": "代码", "name": "名称"}).copy()
        _STOCK_LIST_TIME = now
        return _STOCK_LIST_CACHE
    except Exception as exc:
        logger.warning("Failed to fetch fallback A-share stock list: %s", exc)
        return _STOCK_LIST_CACHE if _STOCK_LIST_CACHE is not None else pd.DataFrame(columns=["代码", "名称"])


def search_tickers(query: str, limit: int = 20) -> list[dict]:
    q = query.strip()
    results = []
    seen = set()
    if re.fullmatch(r"(sh|sz|bj)?\d{6}", q, flags=re.IGNORECASE):
        symbol = normalize(q)
        name = STOCK_PROFILE_FALLBACKS.get(symbol, {}).get("name") or symbol
        results.append({"symbol": symbol, "name": name, "market": get_market(symbol)})
        seen.add(symbol)
        return results[:limit]
    df = _get_stock_list()
    if not df.empty:
        mask = df["代码"].astype(str).str.contains(q, na=False) | df["名称"].astype(str).str.contains(q, na=False)
        for _, row in df[mask].head(limit).iterrows():
            code = str(row["代码"])
            symbol = normalize(code)
            if symbol in seen:
                continue
            seen.add(symbol)
            results.append(
                {
                    "symbol": symbol,
                    "name": str(row["名称"]),
                    "market": get_market(code),
                }
            )
    for symbol, profile in STOCK_PROFILE_FALLBACKS.items():
        code = to_akshare(symbol)
        name = profile.get("name") or symbol
        if symbol in seen:
            continue
        if q.lower() in symbol.lower() or q in code or q in name:
            results.append({"symbol": symbol, "name": name, "market": get_market(symbol)})
            if len(results) >= limit:
                break
    return results


def get_stock_name(symbol: str) -> Optional[str]:
    norm = normalize(symbol)
    fallback = STOCK_PROFILE_FALLBACKS.get(norm)
    if fallback and fallback.get("name"):
        return fallback["name"]
    code = to_akshare(symbol)
    df = _get_stock_list()
    if df.empty:
        return None
    row = df[df["代码"].astype(str) == code]
    if row.empty:
        return None
    return str(row.iloc[0]["名称"])


def fetch_ohlc(symbol: str, start: str, end: str, adjust: str = "qfq") -> list[dict]:
    ak = _get_ak()
    code = to_akshare(symbol)
    norm = normalize(symbol)
    limit_pct = get_limit_pct(symbol)
    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")

    df = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_fmt,
                end_date=end_fmt,
                adjust=adjust,
            )
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    if df is None:
        logger.warning("Failed to fetch OHLC for %s: %s", symbol, last_exc)
        df = None

    if df is None or df.empty:
        fallback_start = (date.fromisoformat(start) - timedelta(days=14)).isoformat().replace("-", "")
        try:
            df = ak.stock_zh_a_daily(
                symbol=norm,
                start_date=fallback_start,
                end_date=end_fmt,
                adjust=adjust,
            )
        except Exception as exc:
            logger.warning("Failed to fetch fallback OHLC for %s: %s", symbol, exc)
            return []
        if df is None or df.empty:
            return []

    rows = []
    prev_close = None
    for _, row in df.iterrows():
        trade_date = _date_text(_value(row, "日期", "date"))
        if not trade_date:
            continue
        volume = _float(row, "成交量", "volume")
        amount = _float(row, "成交额", "amount")
        close = _float(row, "收盘", "close")
        open_price = _float(row, "开盘", "open")
        high = _float(row, "最高", "high")
        low = _float(row, "最低", "low")
        change_pct = _float(row, "涨跌幅", "change_pct", default=None)
        if change_pct is None and prev_close:
            change_pct = (close - prev_close) / prev_close * 100
        amplitude = _float(row, "振幅", "amplitude", default=0.0)
        if not amplitude and open_price:
            amplitude = (high - low) / open_price * 100
        turnover_rate = _float(row, "换手率", "turnover_rate", default=0.0)
        if not turnover_rate:
            turnover_rate = _float(row, "turnover", default=0.0) * 100
        if trade_date < start:
            prev_close = close
            continue
        threshold = (limit_pct * 100) - 0.2
        rows.append(
            {
                "symbol": norm,
                "date": trade_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "vwap": round(amount / volume, 4) if volume > 0 else close,
                "turnover_rate": turnover_rate,
                "change_pct": change_pct or 0.0,
                "amplitude": amplitude,
                "is_limit_up": 1 if (change_pct or 0) >= threshold else 0,
                "is_limit_down": 1 if (change_pct or 0) <= -threshold else 0,
                "is_suspended": 0,
            }
        )
        prev_close = close
    return rows


def _clean_industry_name(industry: str) -> str:
    text = str(industry or "").strip()
    for suffix in ("Ⅰ", "Ⅱ", "Ⅲ", "I", "II", "III"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip()


def industry_board_name(industry: str) -> str:
    text = str(industry or "").strip()
    return INDUSTRY_BOARD_ALIASES.get(text) or INDUSTRY_BOARD_ALIASES.get(_clean_industry_name(text)) or _clean_industry_name(text)


def fetch_stock_profile(symbol: str) -> dict:
    ak = _get_ak()
    code = to_akshare(symbol)
    try:
        df = ak.stock_individual_info_em(symbol=code)
    except Exception as exc:
        logger.warning("Failed to fetch stock profile for %s: %s", symbol, exc)
        return STOCK_PROFILE_FALLBACKS.get(normalize(symbol), {})
    if df is None or df.empty:
        return {}
    data = {}
    for _, row in df.iterrows():
        item = _text(row, "item")
        if item:
            data[item] = _value(row, "value")
    industry = str(data.get("行业") or "").strip()
    return {
        "symbol": normalize(symbol),
        "name": str(data.get("股票简称") or "").strip(),
        "industry_name": industry,
        "board_name": industry_board_name(industry),
        "list_date": _date_text(data.get("上市时间")),
        "raw": data,
    }


def fetch_market_index_ohlc(index_symbol: str, start: str, end: str) -> list[dict]:
    """Fetch daily OHLC for major A-share indices."""
    ak = _get_ak()
    info = MARKET_INDEXES.get(index_symbol)
    if not info:
        raise ValueError(f"Unsupported market index: {index_symbol}")

    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")
    df = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            df = ak.index_zh_a_hist(
                symbol=info["ak_symbol"],
                period="daily",
                start_date=start_fmt,
                end_date=end_fmt,
            )
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))

    if df is None or df.empty:
        try:
            if hasattr(ak, "stock_zh_index_daily"):
                df = ak.stock_zh_index_daily(symbol=info["em_symbol"])
        except Exception as exc:
            last_exc = exc
    if df is None or df.empty:
        try:
            if hasattr(ak, "stock_zh_index_daily_em"):
                df = ak.stock_zh_index_daily_em(
                    symbol=info["em_symbol"],
                    start_date=start_fmt,
                    end_date=end_fmt,
                )
        except Exception as exc:
            last_exc = exc

    if df is None or df.empty:
        logger.warning("Failed to fetch index OHLC for %s: %s", index_symbol, last_exc)
        return []

    rows = []
    prev_close = None
    for _, row in df.iterrows():
        trade_date = _date_text(_value(row, "日期", "date"))
        if not trade_date:
            continue
        open_price = _float(row, "开盘", "open")
        high = _float(row, "最高", "high")
        low = _float(row, "最低", "low")
        close = _float(row, "收盘", "close")
        change_pct = _float(row, "涨跌幅", "change_pct", default=None)
        if change_pct is None and prev_close:
            change_pct = (close - prev_close) / prev_close * 100
        amplitude = _float(row, "振幅", "amplitude", default=0.0)
        if not amplitude and open_price:
            amplitude = (high - low) / open_price * 100
        if trade_date < start:
            prev_close = close
            continue
        if trade_date > end:
            prev_close = close
            continue
        rows.append(
            {
                "index_symbol": index_symbol,
                "index_name": info["name"],
                "date": trade_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _float(row, "成交量", "volume"),
                "amount": _float(row, "成交额", "amount"),
                "change_pct": change_pct or 0.0,
                "amplitude": amplitude,
            }
        )
        prev_close = close
    return rows


def fetch_industry_board_ohlc(board_name: str, start: str, end: str) -> list[dict]:
    ak = _get_ak()
    name = industry_board_name(board_name)
    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")
    df = None
    last_exc: Exception | None = None
    em_names = [EM_INDUSTRY_BOARD_ALIASES.get(name, name)]
    if name not in em_names:
        em_names.append(name)
    for em_name in em_names:
        try:
            df = ak.stock_board_industry_hist_em(
                symbol=em_name,
                start_date=start_fmt,
                end_date=end_fmt,
                period="日k",
                adjust="",
            )
            if df is not None and not df.empty:
                break
        except Exception as exc:
            last_exc = exc
    if df is None or df.empty:
        ths_start = (date.fromisoformat(start) - timedelta(days=14)).isoformat().replace("-", "")
        try:
            df = ak.stock_board_industry_index_ths(symbol=name, start_date=ths_start, end_date=end_fmt)
        except Exception as exc:
            last_exc = exc
    if df is None or df.empty:
        logger.warning("Failed to fetch industry OHLC for %s: %s", name, last_exc)
        return []

    rows = []
    prev_close = None
    for _, row in df.iterrows():
        trade_date = _date_text(_value(row, "日期", "date"))
        if not trade_date:
            continue
        open_price = _float(row, "开盘", "开盘价", "open")
        high = _float(row, "最高", "最高价", "high")
        low = _float(row, "最低", "最低价", "low")
        close = _float(row, "收盘", "收盘价", "close")
        change_pct = _float(row, "涨跌幅", "change_pct", default=None)
        if change_pct is None and prev_close:
            change_pct = (close - prev_close) / prev_close * 100
        amplitude = _float(row, "振幅", "amplitude", default=0.0)
        if not amplitude and open_price:
            amplitude = (high - low) / open_price * 100
        if trade_date < start:
            prev_close = close
            continue
        rows.append(
            {
                "board_name": name,
                "date": trade_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _float(row, "成交量", "volume"),
                "amount": _float(row, "成交额", "amount"),
                "change_pct": change_pct or 0.0,
                "amplitude": amplitude,
            }
        )
        prev_close = close
    return rows


def fetch_industry_board_constituents(board_name: str) -> list[dict]:
    """Fetch current constituents for an EastMoney industry board."""
    ak = _get_ak()
    name = industry_board_name(board_name)
    em_names = [EM_INDUSTRY_BOARD_ALIASES.get(name, name)]
    if name not in em_names:
        em_names.append(name)

    df = None
    last_exc: Exception | None = None
    for em_name in em_names:
        try:
            df = ak.stock_board_industry_cons_em(symbol=em_name)
            if df is not None and not df.empty:
                break
        except Exception as exc:
            last_exc = exc

    if df is None or df.empty:
        logger.warning("Failed to fetch industry constituents for %s: %s", name, last_exc)
        return []

    rows = []
    for _, row in df.iterrows():
        code = _text(row, "代码", "code", "股票代码")
        if not code:
            continue
        symbol = normalize(code)
        rows.append(
            {
                "symbol": symbol,
                "code": to_akshare(symbol),
                "name": _text(row, "名称", "name", "股票简称", default=symbol),
                "board_name": name,
                "latest_price": _float(row, "最新价", "最新", "price", "close", default=0.0),
                "change_pct": _float(row, "涨跌幅", "change_pct", "涨幅", default=0.0),
                "amount": _float(row, "成交额", "amount", default=0.0),
                "volume": _float(row, "成交量", "volume", default=0.0),
                "turnover_rate": _float(row, "换手率", "turnover_rate", default=0.0),
                "amplitude": _float(row, "振幅", "amplitude", default=0.0),
                "market_cap": _float(row, "总市值", "流通市值", "market_cap", default=0.0),
                "source": "akshare.stock_board_industry_cons_em",
            }
        )
    return rows


def fetch_news(symbol: str, start: str, end: str) -> list[dict]:
    ak = _get_ak()
    code = to_akshare(symbol)
    norm = normalize(symbol)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as exc:
        logger.warning("Failed to fetch news for %s: %s", symbol, exc)
        return []

    if df is None or df.empty:
        return []

    events = []
    for _, row in df.iterrows():
        published = _published_text(_value(row, "发布时间", "新闻时间", "time", "date"))
        event_date = _date_text(published)
        if not event_date:
            continue
        parsed_date = date.fromisoformat(event_date)
        if parsed_date < start_date or parsed_date > end_date:
            continue
        title = _text(row, "新闻标题", "标题", "title")
        if not title:
            continue
        content = _text(row, "新闻内容", "内容", "摘要", "description")
        source = _text(row, "文章来源", "来源", "publisher", default="东方财富")
        url = _text(row, "新闻链接", "链接", "url")
        event_id = _event_id(norm, "news", title, event_date, url)
        events.append(
            {
                "id": event_id,
                "symbol": norm,
                "event_type": "news",
                "event_date": event_date,
                "published_at": published,
                "title": title,
                "summary": content[:800] if content else title,
                "source": source,
                "url": url,
                "sentiment": "neutral",
                "impact": "medium",
                "raw": {"source": "akshare.stock_news_em"},
            }
        )
    return events


def fetch_announcements(symbol: str, start: str, end: str) -> list[dict]:
    """Fetch company announcements when the installed AKShare exposes them."""
    ak = _get_ak()
    norm = normalize(symbol)
    code = to_akshare(symbol)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    candidate_calls = []
    if hasattr(ak, "stock_individual_notice_report"):
        candidate_calls.append(
            lambda: ak.stock_individual_notice_report(
                security=code,
                symbol="全部",
                begin_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
        )
    if hasattr(ak, "stock_notice_report"):
        candidate_calls.append(lambda: ak.stock_notice_report(symbol="全部", date=end.replace("-", "")))
    if hasattr(ak, "stock_zh_a_disclosure_report_cninfo"):
        candidate_calls.append(lambda: ak.stock_zh_a_disclosure_report_cninfo(symbol=code))

    df = None
    for call in candidate_calls:
        try:
            df = call()
            if df is not None and not df.empty:
                break
        except Exception as exc:
            logger.debug("Announcement endpoint failed for %s: %s", symbol, exc)

    if df is None or df.empty:
        return []

    events = []
    for _, row in df.iterrows():
        event_date = _date_text(_value(row, "公告日期", "披露日期", "date", "日期"))
        if not event_date:
            continue
        parsed_date = date.fromisoformat(event_date)
        if parsed_date < start_date or parsed_date > end_date:
            continue
        row_code = _text(row, "代码", "证券代码", default=code)
        if row_code and to_akshare(row_code) != code:
            continue
        title = _text(row, "公告标题", "标题", "title")
        if not title:
            continue
        notice_type = _text(row, "公告类型", "类型", default="公告")
        url = _text(row, "网址", "公告链接", "链接", "url")
        event_id = _event_id(norm, "announcement", title, event_date, url)
        events.append(
            {
                "id": event_id,
                "symbol": norm,
                "event_type": "announcement",
                "event_date": event_date,
                "published_at": f"{event_date}T00:00:00",
                "title": title,
                "summary": f"{notice_type}: {title}" if notice_type else title,
                "source": notice_type or "公告",
                "url": url,
                "sentiment": "neutral",
                "impact": "high",
                "raw": {"source": "akshare.stock_individual_notice_report", "notice_type": notice_type},
            }
        )
    return events


def fetch_financial_reports(symbol: str) -> list[dict]:
    """Fetch structured financial-report indicators as event rows."""
    ak = _get_ak()
    code = to_akshare(symbol)
    norm = normalize(symbol)

    df = None
    try:
        if hasattr(ak, "stock_financial_abstract"):
            df = ak.stock_financial_abstract(symbol=code)
        elif hasattr(ak, "stock_financial_abstract_ths"):
            df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    except Exception as exc:
        logger.warning("Failed to fetch financial reports for %s: %s", symbol, exc)
        return []

    if df is None or df.empty:
        return []

    if "指标" in df.columns:
        indicators = {str(row["指标"]): row for _, row in df.iterrows()}

        def pick(period: str, *names: str) -> float:
            for name in names:
                row = indicators.get(name)
                if row is not None:
                    try:
                        return float(row.get(period) or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0

        periods = [col for col in df.columns if str(col).isdigit() and len(str(col)) == 8]
        reports = []
        for period in periods[:24]:
            report_period = f"{period[:4]}-{period[4:6]}-{period[6:]}"
            announcement_date = report_period
            revenue = pick(period, "营业总收入", "营业收入")
            net_profit = pick(period, "归母净利润", "净利润")
            non_gaap_net_profit = pick(period, "扣非净利润")
            operating_cash_flow = pick(period, "经营现金流量净额", "经营现金流净额")
            roe = pick(period, "净资产收益率", "加权净资产收益率")
            yoy_revenue = pick(period, "营业总收入同比增长率", "营业收入同比增长率", "营收同比")
            yoy_net_profit = pick(period, "归母净利润同比增长率", "净利润同比增长率", "净利同比")

            metrics = {
                "report_period": report_period,
                "announcement_date": announcement_date,
                "revenue": revenue,
                "net_profit": net_profit,
                "non_gaap_net_profit": non_gaap_net_profit,
                "operating_cash_flow": operating_cash_flow,
                "roe": roe,
                "yoy_revenue": yoy_revenue,
                "yoy_net_profit": yoy_net_profit,
            }
            title = f"{code} 财报 {report_period}"
            summary = (
                f"营收 {revenue:g}，归母净利润 {net_profit:g}，"
                f"扣非净利润 {non_gaap_net_profit:g}，经营现金流 {operating_cash_flow:g}，ROE {roe:g}。"
            )
            event_id = _event_id(norm, "financial_report", title, announcement_date)
            reports.append(
                {
                    "id": event_id,
                    "symbol": norm,
                    "event_type": "financial_report",
                    "event_date": announcement_date,
                    "published_at": f"{announcement_date}T00:00:00",
                    "title": title,
                    "summary": summary,
                    "source": "财报",
                    "url": "",
                    "sentiment": "neutral",
                    "impact": "high",
                    "metrics": metrics,
                    "raw": {"source": "akshare.stock_financial_abstract"},
                }
            )
        return reports

    reports = []
    for _, row in df.head(24).iterrows():
        report_period = _date_text(_value(row, "报告期", "公告日期", "日期", "date"))
        announcement_date = _date_text(_value(row, "公告日期", "披露日期", "报告期", "日期", "date"))
        if not announcement_date:
            continue
        revenue = _float(row, "营业总收入", "营业收入", "营业收入-营业收入")
        net_profit = _float(row, "归母净利润", "归属于母公司所有者的净利润", "净利润")
        non_gaap_net_profit = _float(row, "扣非净利润", "扣除非经常性损益后的净利润")
        operating_cash_flow = _float(row, "经营现金流量净额", "经营活动产生的现金流量净额")
        roe = _float(row, "净资产收益率", "ROE", "加权净资产收益率")
        yoy_revenue = _float(row, "营业总收入同比增长率", "营业收入同比增长率", "营收同比")
        yoy_net_profit = _float(row, "归母净利润同比增长率", "净利润同比增长率", "净利同比")

        metrics = {
            "report_period": report_period,
            "announcement_date": announcement_date,
            "revenue": revenue,
            "net_profit": net_profit,
            "non_gaap_net_profit": non_gaap_net_profit,
            "operating_cash_flow": operating_cash_flow,
            "roe": roe,
            "yoy_revenue": yoy_revenue,
            "yoy_net_profit": yoy_net_profit,
        }
        title = f"{code} 财报 {report_period or announcement_date}"
        summary = f"营收 {revenue:g}，归母净利润 {net_profit:g}，扣非净利润 {non_gaap_net_profit:g}，ROE {roe:g}。"
        event_id = _event_id(norm, "financial_report", title, announcement_date)
        reports.append(
            {
                "id": event_id,
                "symbol": norm,
                "event_type": "financial_report",
                "event_date": announcement_date,
                "published_at": f"{announcement_date}T00:00:00",
                "title": title,
                "summary": summary,
                "source": "财报",
                "url": "",
                "sentiment": "neutral",
                "impact": "high",
                "metrics": metrics,
                "raw": {"source": "akshare.financial_abstract"},
            }
        )
    return reports


def fetch_northbound_flow(days: int = 365) -> list[dict]:
    ak = _get_ak()
    try:
        if hasattr(ak, "stock_hsgt_north_net_flow_in_em"):
            df = ak.stock_hsgt_north_net_flow_in_em()
        else:
            df = ak.stock_hsgt_hist_em(symbol="北向资金")
    except Exception as exc:
        logger.warning("Failed to fetch northbound flow: %s", exc)
        return []
    if df is None or df.empty:
        return []
    rows = []
    for _, row in df.tail(days).iterrows():
        rows.append(
            {
                "date": _date_text(_value(row, "日期", "date")),
                "sh_net_flow": _float(row, "沪股通净流入"),
                "sz_net_flow": _float(row, "深股通净流入"),
                "total_flow": _float(row, "北向资金净流入", "当日成交净买额", "当日资金流入"),
            }
        )
    return rows


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def fetch_analyst_ratings(symbol: str, start: str, end: str, max_days: int = 90) -> list[dict]:
    """Fetch recent analyst rating / target-price rows from CNInfo via AKShare.

    巨潮的接口按发布日期查询全市场研究报告，很多报告只有评级、没有目标价。
    这里保留评级，同时把未披露目标价映射为 None，前端会明确展示。
    """
    norm = normalize(symbol)
    code = to_akshare(symbol)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    script = r"""
import json
import os
import sys
from datetime import date, timedelta

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(key, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import akshare as ak

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

code = sys.argv[1]
start_date = date.fromisoformat(sys.argv[2])
end_date = date.fromisoformat(sys.argv[3])
max_days = int(sys.argv[4])

if not hasattr(ak, "stock_rank_forecast_cninfo"):
    print("[]")
    raise SystemExit(0)

def clean(value):
    try:
        if value != value:
            return None
    except Exception:
        pass
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

records = []
checked = 0
current = end_date
while current >= start_date and checked < max_days:
    checked += 1
    day_text = current.strftime("%Y%m%d")
    current = current - timedelta(days=1)
    try:
        df = ak.stock_rank_forecast_cninfo(date=day_text)
    except Exception:
        continue
    if df is None or df.empty:
        continue
    for _, row in df.iterrows():
        record = {str(k): clean(v) for k, v in row.to_dict().items()}
        row_code = str(record.get("证券代码") or record.get("代码") or "").split(".")[0].zfill(6)
        if row_code == code:
            records.append(record)

print(json.dumps(records, ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, code, start, end, str(max_days)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=75,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "unknown analyst rating subprocess failure").strip()
        raise RuntimeError(error[-800:])
    try:
        raw_records = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse analyst rating response: {exc}") from exc

    rows: list[dict] = []
    seen: set[str] = set()

    for row in raw_records:
        row_code = _text(row, "证券代码", "代码", default="")
        if to_akshare(row_code) != code:
            continue
        report_date = _date_text(_value(row, "发布日期", "报告日期", "date"))
        if not report_date:
            continue
        parsed_report_date = date.fromisoformat(report_date)
        if parsed_report_date < start_date or parsed_report_date > end_date:
            continue

        institution = _text(row, "研究机构简称", "机构简称", "研究机构")
        analyst = _text(row, "研究员名称", "分析师")
        rating = _text(row, "投资评级", "评级")
        target_low = _optional_float(_value(row, "目标价格-下限", "目标价下限", "目标价格下限"))
        target_high = _optional_float(_value(row, "目标价格-上限", "目标价上限", "目标价格上限"))
        identity = hashlib.sha256(
            f"{norm}:analyst:{report_date}:{institution}:{analyst}:{rating}:{target_low}:{target_high}".encode("utf-8")
        ).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "id": identity,
                "symbol": norm,
                "stock_name": _text(row, "证券简称", "简称", default=""),
                "report_date": report_date,
                "institution": institution,
                "analyst": analyst,
                "rating": rating,
                "is_first_rating": _text(row, "是否首次评级", default=""),
                "rating_change": _text(row, "评级变化", default=""),
                "previous_rating": _text(row, "前一次投资评级", default=""),
                "target_price_low": target_low,
                "target_price_high": target_high,
                "source": "巨潮资讯-机构评级预测",
                "raw": row,
            }
        )
    rows.sort(key=lambda item: item["report_date"], reverse=True)
    return rows
