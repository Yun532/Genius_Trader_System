"""Small OpenAI-compatible LLM client with DeepSeek as the default provider."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

from backend.config import settings

logger = logging.getLogger(__name__)

def configured() -> bool:
    return _api_key(settings.llm_provider) is not None or _api_key(settings.llm_fallback_provider) is not None


def has_api_key(provider: str) -> bool:
    return _api_key(provider) is not None


def _api_key(provider: str) -> Optional[str]:
    provider = (provider or "").lower()
    if provider == "deepseek":
        return settings.deepseek_api_key or None
    if provider == "openai":
        return settings.openai_api_key or None
    return None


def _base_url(provider: str) -> str:
    provider = (provider or "").lower()
    if provider == "deepseek":
        return settings.deepseek_base_url or "https://api.deepseek.com"
    if provider == "openai":
        return settings.openai_base_url or "https://api.openai.com/v1"
    return provider.rstrip("/")


def _model_for(provider: str) -> str:
    if provider == settings.llm_provider:
        return settings.llm_model
    if provider == settings.llm_fallback_provider:
        return settings.llm_fallback_model
    if provider == "openai":
        return settings.llm_fallback_model or settings.llm_model
    return settings.llm_model


def _chat(
    provider: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int = 1200,
    json_mode: bool = False,
) -> str:
    provider = (provider or "").lower()
    api_key = _api_key(provider)
    if not api_key:
        raise RuntimeError(f"Missing API key for {provider}")
    base_url = _base_url(provider)
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if provider == "deepseek" and model.startswith("deepseek-v4"):
        payload["thinking"] = {"type": "disabled"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"]


def chat(messages: list[dict], *, max_tokens: int = 1200) -> str:
    try:
        return _chat(settings.llm_provider, settings.llm_model, messages, max_tokens=max_tokens)
    except Exception as primary_error:
        fallback_provider = (settings.llm_fallback_provider or "").lower()
        primary_provider = (settings.llm_provider or "").lower()
        fallback_model = settings.llm_fallback_model
        if fallback_provider and fallback_provider != primary_provider and _api_key(fallback_provider):
            try:
                return _chat(fallback_provider, fallback_model, messages, max_tokens=max_tokens)
            except Exception as fallback_error:
                logger.warning("LLM fallback failed: %s", fallback_error)
        raise primary_error


def chat_json(messages: list[dict], *, max_tokens: int = 1200) -> dict[str, Any]:
    try:
        text = _chat(settings.llm_provider, settings.llm_model, messages, max_tokens=max_tokens, json_mode=True)
    except Exception as primary_error:
        fallback_provider = (settings.llm_fallback_provider or "").lower()
        primary_provider = (settings.llm_provider or "").lower()
        fallback_model = settings.llm_fallback_model
        if fallback_provider and fallback_provider != primary_provider and _api_key(fallback_provider):
            try:
                text = _chat(fallback_provider, fallback_model, messages, max_tokens=max_tokens, json_mode=True)
            except Exception as fallback_error:
                logger.warning("LLM JSON fallback failed: %s", fallback_error)
                raise primary_error
        else:
            raise primary_error
    return _json_from_text(text)


def _json_from_text(text: str) -> dict[str, Any]:
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end]) if start >= 0 and end > start else {}
    except json.JSONDecodeError:
        return {}


def _chat_json_provider(provider: str, messages: list[dict], *, max_tokens: int = 1200) -> dict[str, Any]:
    return _json_from_text(_chat(provider, _model_for(provider), messages, max_tokens=max_tokens, json_mode=True))


def _analysis_providers() -> list[str]:
    providers = [(settings.llm_provider or "").lower()]
    fallback = (settings.llm_fallback_provider or "").lower()
    if fallback and fallback not in providers:
        providers.append(fallback)
    return [provider for provider in providers if provider and _api_key(provider)]


def _parallel_enabled() -> bool:
    return settings.llm_analysis_mode == "parallel_review" and len(_analysis_providers()) >= 2


def _merge_with_reviewer(
    *,
    task_name: str,
    schema_hint: str,
    context: str,
    reviews: list[dict],
    fallback_result: dict[str, Any],
    max_tokens: int = 1400,
) -> dict[str, Any]:
    reviewer = "openai" if _api_key("openai") else settings.llm_provider
    prompt = f"""你是 A 股投研复核助手。请只基于系统上下文和两个模型的结构化分析，合并成最终结论。
任务: {task_name}
系统上下文:
{context}

模型分析 JSON:
{json.dumps(reviews, ensure_ascii=False)}

要求:
1. 不要添加模型没有依据的新事实。
2. 保留证据最强、最克制的结论。
3. 如果两个模型判断不一致，写入 model_disagreements。
4. 输出 JSON，字段必须兼容下面格式，并额外包含 model_consensus 和 model_disagreements。

{schema_hint}
"""
    try:
        merged = _chat_json_provider(reviewer, [{"role": "user", "content": prompt}], max_tokens=max_tokens)
    except Exception as exc:
        logger.warning("LLM review merge failed: %s", exc)
        merged = dict(fallback_result)
    merged["analysis_mode"] = "parallel_review"
    merged["reviewer_provider"] = reviewer
    merged["model_reviews"] = reviews
    merged.setdefault("model_consensus", "")
    merged.setdefault("model_disagreements", [])
    return merged


def _run_parallel_analysis(
    *,
    task_name: str,
    prompt: str,
    schema_hint: str,
    context: str,
    max_tokens: int,
) -> dict[str, Any]:
    reviews = []
    first_success: Optional[dict[str, Any]] = None
    for provider in _analysis_providers():
        review = {"provider": provider, "model": _model_for(provider), "ok": False}
        try:
            result = _chat_json_provider(provider, [{"role": "user", "content": prompt}], max_tokens=max_tokens)
            review["ok"] = bool(result)
            review["analysis"] = result
            if result and first_success is None:
                first_success = result
        except Exception as exc:
            review["error"] = str(exc)
        reviews.append(review)

    successful = [review for review in reviews if review.get("ok") and review.get("analysis")]
    if not successful:
        error = "; ".join(f"{review['provider']}: {review.get('error') or 'empty response'}" for review in reviews)
        raise RuntimeError(f"All LLM reviews failed: {error}")
    if len(successful) < 2:
        result = dict(first_success or successful[0]["analysis"])
        result["analysis_mode"] = "single_available"
        result["model_reviews"] = reviews
        return result
    return _merge_with_reviewer(
        task_name=task_name,
        schema_hint=schema_hint,
        context=context,
        reviews=reviews,
        fallback_result=first_success or successful[0]["analysis"],
        max_tokens=max_tokens,
    )


def summarize_event(event: dict) -> dict[str, Any]:
    prompt = f"""你是 A 股投研助手。只基于下面事件文本输出 JSON，不要凭记忆补充事实。
股票: {event.get("symbol")}
事件类型: {event.get("event_type")}
日期: {event.get("event_date")}
标题: {event.get("title")}
内容: {event.get("summary") or ""}

JSON 格式:
{{
  "summary": "80 字以内中文摘要",
  "sentiment": "positive/negative/neutral",
  "impact": "high/medium/low",
  "reason_growth": "可能利好原因，若无则为空字符串",
  "reason_decrease": "可能利空原因，若无则为空字符串"
}}"""
    return chat_json([{"role": "user", "content": prompt}], max_tokens=700)


def analyze_price_range(
    *,
    symbol: str,
    start_date: str,
    end_date: str,
    price_summary: str,
    event_context: str,
    question: Optional[str] = None,
) -> dict[str, Any]:
    focus = f"\n用户问题: {question}\n" if question else ""
    prompt = f"""你是 A 股投研助手。请只基于系统提供的数据解释区间行情，不要凭记忆添加外部事实。
股票: {symbol}
区间: {start_date} 至 {end_date}
行情摘要: {price_summary}
相关事件:
{event_context or "无相关事件"}
{focus}
输出 JSON:
{{
  "summary": "1-2 句中文总览",
  "key_events": ["关键事件1", "关键事件2"],
  "bullish_factors": ["利好因素"],
  "bearish_factors": ["利空因素"],
  "trend_analysis": "100-180 字中文趋势分析"
}}"""
    if _parallel_enabled():
        return _run_parallel_analysis(
            task_name="区间行情归因",
            prompt=prompt,
            schema_hint="""{
  "summary": "1-2 句中文总览",
  "key_events": ["关键事件1", "关键事件2"],
  "bullish_factors": ["利好因素"],
  "bearish_factors": ["利空因素"],
  "trend_analysis": "100-180 字中文趋势分析",
  "model_consensus": "两个模型一致的判断",
  "model_disagreements": ["分歧点"]
}""",
            context=f"股票: {symbol}\n区间: {start_date} 至 {end_date}\n行情摘要: {price_summary}\n相关事件:\n{event_context or '无相关事件'}",
            max_tokens=1600,
        )
    return chat_json([{"role": "user", "content": prompt}], max_tokens=1400)


def analyze_daily_reason(
    *,
    symbol: str,
    date: str,
    price_summary: str,
    event_context: str,
    local_summary: str,
    background_context: str = "",
) -> dict[str, Any]:
    prompt = f"""你是 A 股投研助手。请只基于系统提供的数据，解释 {symbol} 在 {date} 当天涨跌的可能原因。
行情摘要: {price_summary}
市场/资金背景:
{background_context or "无可用市场/资金背景"}
本地规则判断: {local_summary}
相关事件:
{event_context or "无相关事件"}

要求:
1. 不要凭记忆添加外部事实。
2. 明确这是“可能原因”，不是确定因果。
3. 如果证据不足，要直接说明。

输出 JSON:
{{
  "summary": "1-2 句中文解释",
  "possible_reasons": ["可能原因1", "可能原因2"],
  "bullish_factors": ["利好因素"],
  "bearish_factors": ["利空因素"],
  "evidence_quality": "high/medium/low"
}}"""
    if _parallel_enabled():
        return _run_parallel_analysis(
            task_name="单日涨跌归因",
            prompt=prompt,
            schema_hint="""{
  "summary": "1-2 句中文解释",
  "possible_reasons": ["可能原因1", "可能原因2"],
  "bullish_factors": ["利好因素"],
  "bearish_factors": ["利空因素"],
  "evidence_quality": "high/medium/low",
  "model_consensus": "两个模型一致的判断",
  "model_disagreements": ["分歧点"]
}""",
            context=f"股票: {symbol}\n日期: {date}\n行情摘要: {price_summary}\n市场/资金背景:\n{background_context or '无可用市场/资金背景'}\n本地规则判断: {local_summary}\n相关事件:\n{event_context or '无相关事件'}",
            max_tokens=1400,
        )
    return chat_json([{"role": "user", "content": prompt}], max_tokens=1200)
