"""External information discovery providers for A-share research.

These providers only discover source-backed facts. The downstream LLM analysis
layer still receives normal market events and should not invent facts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

from backend.analysis_cache import get_analysis_cache, store_analysis_cache
from backend.ashare.symbol import normalize, to_akshare
from backend.config import settings

logger = logging.getLogger(__name__)

CAPITAL_KEYWORDS = (
    "游资",
    "龙虎榜",
    "主力资金",
    "资金流",
    "净流入",
    "净流出",
    "大单",
    "席位",
    "营业部",
    "北向资金",
    "融资融券",
)

POLICY_KEYWORDS = ("政策", "规划", "监管", "补贴", "招标", "发改委", "工信部", "能源局", "国资委")
GLOBAL_KEYWORDS = ("国际", "海外", "关税", "汇率", "美元", "出口", "制裁", "地缘", "原油", "黄金", "大宗商品")
SUPPLY_CHAIN_KEYWORDS = ("上游", "下游", "产业链", "成本", "原材料", "需求", "订单", "供应商", "客户")
SECTOR_KEYWORDS = ("行业", "板块", "龙头", "景气", "替代", "互补", "竞争格局")


def _event_kind(title: str, summary: str) -> str:
    text = f"{title} {summary}"
    if any(keyword in text for keyword in POLICY_KEYWORDS):
        return "policy"
    if any(keyword in text for keyword in GLOBAL_KEYWORDS):
        return "global_macro"
    if any(keyword in text for keyword in SUPPLY_CHAIN_KEYWORDS):
        return "supply_chain"
    if any(keyword in text for keyword in SECTOR_KEYWORDS):
        return "sector"
    if any(keyword in text for keyword in CAPITAL_KEYWORDS):
        return "capital"
    return "news"


def _event_id(symbol: str, title: str, event_date: str, url: str = "", event_type: str = "news") -> str:
    raw = f"{normalize(symbol)}:web_info:{event_type}:{event_date}:{title}:{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _date_text(value: Any, *, fallback: Optional[str] = None) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if not text:
        return fallback or date.today().isoformat()
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    text = text[:10]
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return fallback or date.today().isoformat()


def _parse_json_array(text: str) -> list[dict]:
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start:end])
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _to_event(symbol: str, item: dict, *, provider: str, fallback_date: str) -> Optional[dict]:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title:
        return None
    event_date = _date_text(item.get("published_date") or item.get("date"), fallback=fallback_date)
    summary = str(item.get("summary") or item.get("content") or item.get("snippet") or "").strip()
    source = str(item.get("source") or item.get("publisher") or provider).strip()
    event_type = str(item.get("event_type") or item.get("type") or "").strip() or _event_kind(title, summary)
    if event_type not in {"news", "capital", "policy", "global_macro", "sector", "supply_chain"}:
        event_type = _event_kind(title, summary)
    return {
        "id": _event_id(symbol, title, event_date, url, event_type),
        "symbol": normalize(symbol),
        "event_type": event_type,
        "event_date": event_date,
        "published_at": f"{event_date}T00:00:00",
        "title": title,
        "summary": summary,
        "source": source,
        "url": url,
        "metrics": {},
        "raw": {"provider": provider, "external_info": True, **item},
    }


def _openai_headers() -> dict[str, str]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}


def _extract_openai_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for output in payload.get("output") or []:
        for content in output.get("content") or []:
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _openai_web_search(symbol: str, name: str, start: str, end: str, max_results: int) -> list[dict]:
    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    query_name = name or symbol
    code = to_akshare(symbol)
    prompt = f"""请搜索 {query_name}（{code}）在 {start} 至 {end} 期间的真实中文财经资讯。
优先返回与这家上市公司直接相关的新闻、公告解读、业绩动态、行业事件、龙虎榜、游资席位、主力资金、资金流向、北向资金、融资融券等信息。
不要返回股吧帖子、行情页面、无来源传闻、纯价格报价页或与公司无直接关系的泛市场内容。
输出 JSON array，每项字段：
title, url, source, published_date, summary, event_type。
event_type 只能是 news 或 capital；游资/龙虎榜/资金流/主力资金/北向资金/融资融券相关请标为 capital，其余公司资讯标为 news。
最多 {max_results} 条。"""
    response = requests.post(
        f"{base_url}/responses",
        headers=_openai_headers(),
        json={
            "model": settings.openai_web_search_model,
            "tools": [{"type": "web_search"}],
            "input": prompt,
            "temperature": 0.1,
        },
        timeout=90,
    )
    response.raise_for_status()
    items = _parse_json_array(_extract_openai_text(response.json()))
    return items[:max_results]


def _tavily_web_search(symbol: str, name: str, start: str, end: str, max_results: int) -> list[dict]:
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured")
    query_name = name or symbol
    code = to_akshare(symbol)
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": settings.tavily_api_key,
            "query": f"{query_name} {code} A股 资讯 新闻 公告 业绩 龙虎榜 游资 主力资金 资金流 北向资金 融资融券 {start} {end}",
            "search_depth": "basic",
            "topic": "news",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=45,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    return [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "source": item.get("source") or "Tavily",
            "published_date": item.get("published_date"),
            "summary": item.get("content"),
            "event_type": _event_kind(str(item.get("title") or ""), str(item.get("content") or "")),
        }
        for item in results[:max_results]
    ]


def _openai_macro_chain_search(symbol: str, name: str, industry: str, board: str, start: str, end: str, max_results: int) -> list[dict]:
    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    query_name = name or symbol
    code = to_akshare(symbol)
    industry_text = industry or board or "所属行业"
    prompt = f"""请搜索 {query_name}（{code}）及其所属行业“{industry_text}”在 {start} 至 {end} 附近的真实中文财经资讯。
重点找国家政策、监管/产业规划、招投标、国际局势、出口/关税/汇率/大宗商品、上游成本、下游需求、行业龙头、替代/互补/竞争行业。
不要返回股吧帖子、无来源传闻、纯行情报价页。输出 JSON array，最多 {max_results} 条，每项字段：
title, url, source, published_date, summary, event_type。
event_type 只能是 policy、global_macro、sector、supply_chain、capital、news 之一。"""
    response = requests.post(
        f"{base_url}/responses",
        headers=_openai_headers(),
        json={
            "model": settings.openai_web_search_model,
            "tools": [{"type": "web_search"}],
            "input": prompt,
            "temperature": 0.1,
        },
        timeout=90,
    )
    response.raise_for_status()
    return _parse_json_array(_extract_openai_text(response.json()))[:max_results]


def _tavily_macro_chain_search(symbol: str, name: str, industry: str, board: str, start: str, end: str, max_results: int) -> list[dict]:
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured")
    query_name = name or symbol
    code = to_akshare(symbol)
    industry_text = industry or board or ""
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": settings.tavily_api_key,
            "query": f"{query_name} {code} {industry_text} A股 政策 产业链 上游 下游 龙头 互补 替代 国际局势 关税 汇率 大宗商品 {start} {end}",
            "search_depth": "basic",
            "topic": "news",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=45,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    return [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "source": item.get("source") or "Tavily",
            "published_date": item.get("published_date"),
            "summary": item.get("content"),
            "event_type": _event_kind(str(item.get("title") or ""), str(item.get("content") or "")),
        }
        for item in results[:max_results]
    ]


def discover_macro_chain_info(
    symbol: str,
    name: str = "",
    *,
    industry: str = "",
    board: str = "",
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = None,
) -> dict:
    norm = normalize(symbol)
    end_date = datetime.fromisoformat(end).date() if end else date.today()
    start_date = datetime.fromisoformat(start).date() if start else end_date - timedelta(days=30)
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    selected = (provider or settings.news_web_search_provider or "openai").lower()
    limit = max_results or max(settings.news_web_search_max_results, 10)

    if selected == "tavily":
        raw_items = _tavily_macro_chain_search(norm, name, industry, board, start_text, end_text, limit)
    elif selected == "openai":
        raw_items = _openai_macro_chain_search(norm, name, industry, board, start_text, end_text, limit)
    else:
        raise RuntimeError(f"Unsupported web search provider: {selected}")

    events: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        event = _to_event(norm, item, provider=selected, fallback_date=end_text)
        if not event:
            continue
        key = f"{event['event_type']}|{event['title']}|{event.get('source')}|{event['event_date']}|{event.get('url')}"
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return {
        "symbol": norm,
        "provider": selected,
        "start": start_text,
        "end": end_text,
        "events": events,
        "found": len(events),
        "cached": False,
    }


def discover_external_info(
    symbol: str,
    name: str = "",
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = None,
    refresh_cache: bool = False,
) -> dict:
    norm = normalize(symbol)
    end_date = datetime.fromisoformat(end).date() if end else date.today()
    start_date = datetime.fromisoformat(start).date() if start else end_date - timedelta(days=settings.news_web_search_lookback_days)
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    selected = (provider or settings.news_web_search_provider or "openai").lower()
    limit = max_results or settings.news_web_search_max_results
    cache_parts = {
        "symbol": norm,
        "name": name or "",
        "start": start_text,
        "end": end_text,
        "provider": selected,
        "max_results": limit,
        "schema": 2,
    }

    if not refresh_cache:
        cached = get_analysis_cache("external_info_search", cache_parts)
        if cached and isinstance(cached.get("events"), list):
            events = cached["events"]
            logger.info("External info discovery cache hit for %s via %s returned %s events", norm, selected, len(events))
            return {
                "symbol": norm,
                "provider": selected,
                "start": start_text,
                "end": end_text,
                "events": events,
                "found": len(events),
                "cached": True,
            }

    if selected == "tavily":
        raw_items = _tavily_web_search(norm, name, start_text, end_text, limit)
    elif selected == "openai":
        raw_items = _openai_web_search(norm, name, start_text, end_text, limit)
    else:
        raise RuntimeError(f"Unsupported web search provider: {selected}")

    events: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        event = _to_event(norm, item, provider=selected, fallback_date=end_text)
        if not event:
            continue
        key = f"{event['event_type']}|{event['title']}|{event.get('source')}|{event['event_date']}|{event.get('url')}"
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    logger.info("External info discovery for %s via %s returned %s events", norm, selected, len(events))
    payload = {
        "symbol": norm,
        "provider": selected,
        "start": start_text,
        "end": end_text,
        "events": events,
        "found": len(events),
    }
    store_analysis_cache(
        "external_info_search",
        cache_parts,
        payload,
        ttl_hours=settings.news_web_search_cache_ttl_hours,
        llm_used=False,
        meta={"provider": selected, "max_results": limit},
    )
    payload["cached"] = False
    return payload


def discover_external_news(
    symbol: str,
    name: str = "",
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    provider: Optional[str] = None,
    max_results: Optional[int] = None,
) -> list[dict]:
    """Compatibility wrapper for older code paths that still say "news"."""
    result = discover_external_info(
        symbol,
        name,
        start=start,
        end=end,
        provider=provider,
        max_results=max_results,
    )
    return result["events"]
