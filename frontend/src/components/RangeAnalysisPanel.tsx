import { useEffect, useRef, useState } from 'react';
import axios from 'axios';

interface RangeAnalysis {
  symbol: string;
  name?: string | null;
  display_name?: string | null;
  start_date: string;
  end_date: string;
  price_change_pct: number;
  open_price: number;
  close_price: number;
  high_price: number;
  low_price: number;
  news_count: number;
  trading_days: number;
  question?: string;
  llm_used?: boolean;
  llm_error?: string;
  llm_policy?: {
    use_llm: boolean;
    reason: string;
    recent_auto_days?: number;
    cache_hit?: boolean;
  };
  llm_cache?: {
    hit: boolean;
    created_at?: string;
    expires_at?: string;
  };
  analysis: {
    summary: string;
    key_events: string[];
    bullish_factors: string[];
    bearish_factors: string[];
    trend_analysis: string;
    model_consensus?: string | null;
    model_disagreements?: string[];
    analysis_mode?: string;
    reviewer_provider?: string;
    model_reviews?: Array<{
      provider: string;
      model: string;
      ok: boolean;
      error?: string;
      analysis?: {
        summary?: string;
        trend_analysis?: string;
      };
    }>;
  };
  error?: string;
}

interface Props {
  symbol: string;
  displayName?: string;
  startDate: string;
  endDate: string;
  question?: string;
  onClear: () => void;
}

function providerLabel(provider: string) {
  if (provider === 'deepseek') return 'DeepSeek';
  if (provider === 'openai') return 'OpenAI';
  return provider;
}

function llmCostLabel(data: RangeAnalysis) {
  if (data.llm_cache?.hit) return 'LLM 结果来自缓存';
  if (data.llm_used) return 'LLM 已使用';
  if (data.llm_policy?.reason === 'historical_local_only') return '历史区间默认本地归因';
  if (data.llm_policy?.reason === 'request_disabled') return '已按设置跳过 LLM';
  return '本地归因';
}

export default function RangeAnalysisPanel({ symbol, displayName, startDate, endDate, question, onClear }: Props) {
  const [data, setData] = useState<RangeAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    setData(null);

    axios
      .post<RangeAnalysis>(
        `/api/stocks/${symbol}/analyze`,
        { start_date: startDate, end_date: endDate, question },
        { signal: controller.signal },
      )
      .then((res) => {
        if (res.data.error) setError(res.data.error);
        else setData(res.data);
      })
      .catch((err) => {
        if (!axios.isCancel(err)) setError('分析失败，请稍后重试');
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [symbol, startDate, endDate, question]);

  const changePct = data?.price_change_pct ?? 0;
  const isUp = changePct >= 0;
  const titleName = data?.display_name || displayName || symbol;

  return (
    <div className="news-panel range-panel">
      <div className="news-panel-header">
        <h2>{titleName} 区间归因</h2>
        <button className="range-clear-btn" onClick={onClear}>清除</button>
      </div>

      {loading ? (
        <div className="news-empty">正在基于行情和事件生成解释...</div>
      ) : error ? (
        <div className="news-empty">{error}</div>
      ) : data ? (
        <div className="news-list">
          {question && (
            <div className="range-question-card">
              <span className="range-question-icon">?</span>
              <span className="range-question-text">{question}</span>
            </div>
          )}

          <div className="range-price-card">
            <div className="range-dates">{data.start_date} 至 {data.end_date}</div>
            <div className="range-price-row">
              <span className="range-price">¥{data.open_price.toFixed(2)} → ¥{data.close_price.toFixed(2)}</span>
              <span className={`range-change ${isUp ? 'up' : 'down'}`}>
                {isUp ? '+' : ''}{changePct.toFixed(2)}%
              </span>
            </div>
            <div className="range-meta">
              {data.trading_days} 个交易日 · {data.news_count} 条事件 · {
                data.analysis.analysis_mode === 'parallel_review'
                  ? `双模型复核 · ${providerLabel(data.analysis.reviewer_provider || 'openai')} 汇总`
                  : llmCostLabel(data)
              }
              {data.llm_cache?.expires_at ? ` · 缓存至 ${data.llm_cache.expires_at.slice(0, 16).replace('T', ' ')}` : ''}
            </div>
          </div>

          {data.analysis.summary && (
            <div className="range-section">
              <p className="range-summary">{data.analysis.summary}</p>
            </div>
          )}

          {data.analysis.key_events?.length > 0 && (
            <div className="range-section">
              <h3 className="range-section-title">关键事件</h3>
              <ul className="range-events">
                {data.analysis.key_events.map((event, index) => <li key={index}>{event}</li>)}
              </ul>
            </div>
          )}

          {data.analysis.bullish_factors?.length > 0 && (
            <div className="range-section">
              <h3 className="range-section-title">利好因素</h3>
              {data.analysis.bullish_factors.map((factor, index) => (
                <div key={index} className="reason up">
                  <span className="reason-icon">+</span> {factor}
                </div>
              ))}
            </div>
          )}

          {data.analysis.bearish_factors?.length > 0 && (
            <div className="range-section">
              <h3 className="range-section-title">利空因素</h3>
              {data.analysis.bearish_factors.map((factor, index) => (
                <div key={index} className="reason down">
                  <span className="reason-icon">-</span> {factor}
                </div>
              ))}
            </div>
          )}

          {data.analysis.trend_analysis && (
            <div className="range-section">
              <h3 className="range-section-title">走势解释</h3>
              <p className="range-trend">{data.analysis.trend_analysis}</p>
            </div>
          )}

          {(data.analysis.model_consensus || (data.analysis.model_disagreements || []).length > 0 || (data.analysis.model_reviews || []).length > 0) && (
            <div className="range-section">
              <h3 className="range-section-title">模型复核</h3>
              {data.analysis.model_consensus && <p className="range-summary">{data.analysis.model_consensus}</p>}
              {(data.analysis.model_disagreements || []).length > 0 && (
                <ul className="range-events">
                  {(data.analysis.model_disagreements || []).map((item, index) => <li key={index}>{item}</li>)}
                </ul>
              )}
              {(data.analysis.model_reviews || []).length > 0 && (
                <div className="daily-model-grid">
                  {(data.analysis.model_reviews || []).map((review) => (
                    <div className={`daily-model-card ${review.ok ? 'ok' : 'failed'}`} key={`${review.provider}-${review.model}`}>
                      <div className="daily-context-label">{providerLabel(review.provider)}</div>
                      <div className="daily-model-main">{review.ok ? '已完成独立分析' : '分析失败'}</div>
                      <div className="daily-context-sub">
                        {review.ok ? review.analysis?.summary || review.analysis?.trend_analysis || review.model : review.error || review.model}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
