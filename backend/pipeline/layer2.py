"""Layer 2: On-demand Sonnet deep analysis.

Triggered when user clicks a news article. Cached in layer2_results.
Cost: ~$0.003/article, only on user click.
"""

import json
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from backend.database import get_conn
from backend.llm import chat, chat_json, configured as llm_configured


def get_cached(news_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Check if a deep analysis is already cached."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM layer2_results WHERE news_id = ? AND symbol = ?",
        (news_id, symbol),
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def analyze_article(news_id: str, symbol: str) -> Dict[str, Any]:
    """Run deep Sonnet analysis on a single article. Returns cached if available."""
    cached = get_cached(news_id, symbol)
    if cached:
        return cached

    # Fetch article data
    conn = get_conn()
    article = conn.execute(
        "SELECT title, description, article_url FROM news_raw WHERE id = ?",
        (news_id,),
    ).fetchone()
    conn.close()

    if not article:
        return {"error": "Article not found"}

    prompt = f"""你是A股投研助理。只基于下面事件文本，分析它对 {symbol} 的可能影响，不要凭记忆补充事实。

TITLE: {article['title']}

DESCRIPTION: {article['description'] or 'No description available'}

请输出 JSON:
{{
  "discussion": "120-200字中文分析",
  "growth_reasons": "可能推动股价上涨的具体因素",
  "decrease_reasons": "可能推动股价下跌的具体风险"
}}

Respond with JSON only."""

    try:
        parsed = chat_json([{"role": "user", "content": prompt}], max_tokens=1024)
    except Exception as exc:
        return {"error": f"LLM unavailable: {exc}"}

    # Cache result
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO layer2_results
           (news_id, symbol, discussion, growth_reasons, decrease_reasons, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            news_id,
            symbol,
            parsed.get("discussion", ""),
            parsed.get("growth_reasons", ""),
            parsed.get("decrease_reasons", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "news_id": news_id,
        "symbol": symbol,
        "discussion": parsed.get("discussion", ""),
        "growth_reasons": parsed.get("growth_reasons", ""),
        "decrease_reasons": parsed.get("decrease_reasons", ""),
    }


def _article_payload(news_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        """SELECT nr.id AS news_id, nr.title, nr.description, nr.publisher,
                  nr.article_url, nr.news_type, nr.published_utc,
                  l1.sentiment, l1.chinese_summary, l1.key_discussion,
                  l1.reason_growth, l1.reason_decrease,
                  na.trade_date, na.ret_t0, na.ret_t1, na.ret_t5,
                  me.event_type, me.event_date, me.source, me.metrics_json
           FROM news_raw nr
           LEFT JOIN layer1_results l1 ON nr.id = l1.news_id AND l1.symbol = ?
           LEFT JOIN news_aligned na ON nr.id = na.news_id AND na.symbol = ?
           LEFT JOIN market_events me ON nr.id = me.id
           WHERE nr.id = ?""",
        (symbol, symbol, news_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _local_article_analysis(article: Dict[str, Any]) -> Dict[str, Any]:
    event_type = article.get("event_type") or article.get("news_type") or "event"
    sentiment = article.get("sentiment") or "neutral"
    type_label = {"news": "新闻", "announcement": "公告", "financial_report": "财报"}.get(event_type, event_type)
    direction = "偏利好" if sentiment == "positive" else "偏利空" if sentiment == "negative" else "中性"
    discussion = (
        f"这是一条{type_label}事件，系统当前将其判定为{direction}。"
        f"标题为“{article.get('title') or ''}”。"
        f"{article.get('chinese_summary') or article.get('key_discussion') or article.get('description') or ''}"
        "该结论只基于已入库文本和相邻交易表现，不能视为确定因果。"
    )
    price_parts = []
    if article.get("ret_t0") is not None:
        price_parts.append(f"当日收益 {article['ret_t0'] * 100:+.2f}%")
    if article.get("ret_t1") is not None:
        price_parts.append(f"T+1 收益 {article['ret_t1'] * 100:+.2f}%")
    if article.get("ret_t5") is not None:
        price_parts.append(f"T+5 收益 {article['ret_t5'] * 100:+.2f}%")
    if price_parts:
        discussion += " 已记录价格反应：" + "，".join(price_parts) + "。"
    growth = article.get("reason_growth") or (
        "若事件改善盈利、订单、政策或市场预期，可能构成上行动力。"
        if sentiment != "negative" else "当前文本未给出明确利好线索。"
    )
    decrease = article.get("reason_decrease") or (
        "若事件暴露经营、监管、业绩或流动性压力，可能压制风险偏好。"
        if sentiment != "positive" else "当前文本未给出明确利空线索。"
    )
    return {
        "discussion": discussion,
        "growth_reasons": growth,
        "decrease_reasons": decrease,
        "impact_path": [
            "事件文本进入统一事件库",
            "Layer 1 生成摘要、情绪和利好利空线索",
            "结合事件日及后续收益观察市场反应",
        ],
        "evidence_quality": "medium" if article.get("chinese_summary") or article.get("description") else "low",
    }


def analyze_article(news_id: str, symbol: str) -> Dict[str, Any]:
    """A-share event deep dive. Uses local analysis when LLM is unavailable."""
    article = _article_payload(news_id, symbol)
    if not article:
        return {"error": "Article not found", "news_id": news_id, "symbol": symbol}

    cached = get_cached(news_id, symbol)
    if cached:
        result = dict(cached)
        result["article"] = article
        result["impact_path"] = [
            "事件文本进入统一事件库",
            "已命中缓存的 Layer 2 深挖结果",
            "结合事件日及后续收益观察市场反应",
        ]
        result["evidence_quality"] = "medium"
        result["llm_used"] = True
        result["cached"] = True
        return result

    local = _local_article_analysis(article)
    if not llm_configured():
        return {"news_id": news_id, "symbol": symbol, "article": article, **local, "llm_used": False, "cached": False}

    prompt = f"""你是 A 股投研助手。请只基于下面已入库事件文本和价格反应，分析它对 {symbol} 的可能影响，不要凭记忆补充外部事实。
事件类型: {article.get('event_type') or article.get('news_type') or 'event'}
日期: {article.get('trade_date') or article.get('event_date') or article.get('published_utc')}
来源: {article.get('publisher') or article.get('source') or '未知'}
标题: {article.get('title')}
摘要: {article.get('chinese_summary') or article.get('description') or ''}
Layer 1 讨论: {article.get('key_discussion') or ''}
情绪: {article.get('sentiment') or 'neutral'}
价格反应: T0={article.get('ret_t0')}, T1={article.get('ret_t1')}, T5={article.get('ret_t5')}
本地规则分析: {json.dumps(local, ensure_ascii=False)}

请输出 JSON:
{{
  "discussion": "120-220字中文分析，明确这是可能影响不是确定因果",
  "growth_reasons": "可能推动股价上行的具体线索",
  "decrease_reasons": "可能压制股价的具体风险",
  "impact_path": ["影响路径1", "影响路径2"],
  "evidence_quality": "high/medium/low"
}}
只返回 JSON。"""
    try:
        parsed = chat_json([{"role": "user", "content": prompt}], max_tokens=1024)
    except Exception as exc:
        return {
            "news_id": news_id,
            "symbol": symbol,
            "article": article,
            **local,
            "llm_used": False,
            "llm_error": str(exc),
            "cached": False,
        }

    discussion = parsed.get("discussion") or local["discussion"]
    growth = parsed.get("growth_reasons") or local["growth_reasons"]
    decrease = parsed.get("decrease_reasons") or local["decrease_reasons"]
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO layer2_results
           (news_id, symbol, discussion, growth_reasons, decrease_reasons, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (news_id, symbol, discussion, growth, decrease, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    return {
        "news_id": news_id,
        "symbol": symbol,
        "article": article,
        "discussion": discussion,
        "growth_reasons": growth,
        "decrease_reasons": decrease,
        "impact_path": parsed.get("impact_path") or local["impact_path"],
        "evidence_quality": parsed.get("evidence_quality") or local["evidence_quality"],
        "llm_used": True,
        "cached": False,
    }


def generate_story(symbol: str, csv_content: str) -> str:
    """Generate an AI story about stock price movements. Port from app.py."""
    text = chat(
        [
            {
                "role": "user",
                "content": f"""下面是 {symbol} 的OHLC数据和相关事件。请只基于这些数据生成中文行情复盘。

Data:
```
{csv_content[-50000:]}
```

要求:
1. 用1-2句概述区间表现
2. 按时间线讲清主要转折
3. 结合新闻、公告、财报事件解释可能原因
4. 明确哪些判断有数据支撑，哪些只是可能性
5. 输出 HTML，使用 <h3>、<p>、<strong>

不要给投资建议，不要编造数据。""",
            }
        ],
        max_tokens=4096,
    )
    return text


def analyze_range(symbol: str, start_date: str, end_date: str, question: Optional[str] = None) -> Dict[str, Any]:
    """Analyze what drove price movement in a date range using Sonnet."""
    conn = get_conn()

    # Get OHLC data for range
    ohlc_rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM ohlc WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date ASC",
        (symbol, start_date, end_date),
    ).fetchall()

    if not ohlc_rows:
        conn.close()
        return {"error": "No OHLC data for this range"}

    open_price = ohlc_rows[0]["open"]
    close_price = ohlc_rows[-1]["close"]
    high_price = max(r["high"] for r in ohlc_rows)
    low_price = min(r["low"] for r in ohlc_rows)
    price_change_pct = round((close_price - open_price) / open_price * 100, 2)

    # Get news in range, prioritize by impact
    news_rows = conn.execute(
        """SELECT nr.title, l1.chinese_summary, l1.key_discussion,
                  l1.sentiment, l1.reason_growth, l1.reason_decrease,
                  na.trade_date, na.ret_t0
           FROM news_aligned na
           JOIN layer1_results l1 ON na.news_id = l1.news_id AND l1.symbol = na.symbol
           JOIN news_raw nr ON na.news_id = nr.id
           WHERE na.symbol = ? AND na.trade_date >= ? AND na.trade_date <= ?
             AND l1.relevance = 'relevant'
           ORDER BY ABS(COALESCE(na.ret_t0, 0)) DESC
           LIMIT 30""",
        (symbol, start_date, end_date),
    ).fetchall()
    conn.close()

    news_count = len(news_rows)

    # Build news context for prompt
    news_context = ""
    for i, row in enumerate(news_rows[:30], 1):
        ret = f"Same-day change: {row['ret_t0']*100:.2f}%" if row["ret_t0"] else ""
        news_context += f"\n{i}. [{row['trade_date']}] {row['title']}\n"
        if row["chinese_summary"]:
            news_context += f"   Summary: {row['chinese_summary']}\n"
        if ret:
            news_context += f"   {ret}\n"

    ohlc_summary = f"开盘 {open_price:.2f}，收盘 {close_price:.2f}，最高 {high_price:.2f}，最低 {low_price:.2f}，涨跌幅 {price_change_pct:+.2f}%，交易日 {len(ohlc_rows)}"
    question_part = f"用户问题: {question}\n\n" if question else ""

    prompt = f"""你是A股投研助理。请只基于系统提供的数据，分析 {symbol} 从 {start_date} 到 {end_date} 的股价变化。

行情数据:
{ohlc_summary}

相关事件（{news_count}条）:
{news_context if news_context else "该区间无相关事件"}

{question_part}请返回 JSON:
{{
  "summary": "1-2句中文总览",
  "key_events": ["关键事件1", "关键事件2"],
  "bullish_factors": ["利好因素"],
  "bearish_factors": ["利空因素"],
  "trend_analysis": "100-150字中文分析"
}}

Return JSON only."""
    try:
        analysis = chat_json([{"role": "user", "content": prompt}], max_tokens=2048)
    except Exception as exc:
        analysis = {
            "summary": f"LLM unavailable: {exc}",
            "key_events": [],
            "bullish_factors": [],
            "bearish_factors": [],
            "trend_analysis": "",
        }

    return {
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "price_change_pct": price_change_pct,
        "open_price": open_price,
        "close_price": close_price,
        "high_price": high_price,
        "low_price": low_price,
        "news_count": news_count,
        "trading_days": len(ohlc_rows),
        "analysis": analysis,
    }
