import { useEffect, useState } from 'react';
import axios from 'axios';
import SimilarDaysPanel from './SimilarDaysPanel';

interface DailyEvent {
  id: string;
  event_type: string;
  event_date: string;
  title: string;
  summary?: string | null;
  source?: string | null;
  sentiment?: string | null;
  url?: string | null;
}

interface AnalystRating {
  id: string;
  report_date: string;
  institution?: string | null;
  analyst?: string | null;
  rating?: string | null;
  rating_change?: string | null;
  previous_rating?: string | null;
  is_first_rating?: string | null;
  target_price_low?: number | null;
  target_price_high?: number | null;
  source?: string | null;
}

interface DailyReasonData {
  symbol: string;
  name?: string | null;
  display_name?: string | null;
  date: string;
  price: {
    open: number;
    high: number;
    low: number;
    close: number;
    volume?: number | null;
    amount?: number | null;
    turnover_rate?: number | null;
    amplitude?: number | null;
  };
  previous_close: number | null;
  change_pct: number;
  event_window: {
    start: string;
    end: string;
  };
  market_context?: {
    available: boolean;
    index_symbol?: string | null;
    index_name?: string | null;
    index_date?: string | null;
    index_change_pct?: number | null;
    stock_change_pct?: number | null;
    excess_return_pct?: number | null;
    relationship?: string | null;
    used_cache?: boolean;
    error?: string | null;
  };
  fund_context?: {
    available: boolean;
    date?: string | null;
    total_flow?: number | null;
    sh_net_flow?: number | null;
    sz_net_flow?: number | null;
    direction?: string | null;
    relationship?: string | null;
    used_cache?: boolean;
    error?: string | null;
  };
  industry_context?: {
    available: boolean;
    industry_name?: string | null;
    board_name?: string | null;
    board_date?: string | null;
    industry_change_pct?: number | null;
    stock_change_pct?: number | null;
    excess_return_pct?: number | null;
    relationship?: string | null;
    used_cache?: boolean;
    mapping_used_cache?: boolean;
    error?: string | null;
  };
  analyst_context?: {
    available: boolean;
    ratings?: AnalystRating[];
    latest?: AnalystRating | null;
    latest_with_target?: AnalystRating | null;
    current_close?: number | null;
    target_price_low?: number | null;
    target_price_high?: number | null;
    target_price_mid?: number | null;
    target_upside_pct?: number | null;
    used_cache?: boolean;
    error?: string | null;
  };
  macro_chain_context?: {
    available: boolean;
    cached?: boolean;
    generated_at?: string | null;
    expires_at?: string | null;
    sources_count?: number;
    summary?: string | null;
    policy_summary?: string[];
    global_summary?: string[];
    transmission_paths?: string[];
    watch_points?: string[];
    evidence_quality?: string;
  };
  events: DailyEvent[];
  analysis: {
    summary: string;
    possible_reasons: string[];
    bullish_factors: string[];
    bearish_factors: string[];
    evidence_quality: 'high' | 'medium' | 'low' | string;
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
        evidence_quality?: string;
      };
    }>;
  };
  llm_used: boolean;
  llm_error?: string | null;
  llm_policy?: {
    use_llm: boolean;
    reason: string;
    recent_auto_days?: number;
    cache_hit?: boolean;
  };
  cached?: boolean;
  generated_at?: string | null;
}

interface Props {
  symbol: string;
  displayName?: string;
  date: string;
  onClose: () => void;
  onOpenMacroChain?: (date: string) => void;
  onLoadedPrice?: (ohlc: { date: string; open: number; high: number; low: number; close: number; change: number } | null) => void;
}

const EVENT_LABELS: Record<string, string> = {
  news: '新闻',
  announcement: '公告',
  financial_report: '财报',
  capital: '资金',
};

const QUALITY_LABELS: Record<string, string> = {
  high: '证据较强',
  medium: '证据一般',
  low: '证据不足',
};

const signedPct = (value?: number | null) => (
  value === null || value === undefined ? '--' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
);

const signedAmount = (value?: number | null) => (
  value === null || value === undefined ? '--' : `${value >= 0 ? '+' : ''}${value.toFixed(2)} 亿元`
);

const formatPrice = (value?: number | null) => (
  value === null || value === undefined ? '--' : `${value.toFixed(2)} 元`
);

function targetPriceText(item?: AnalystRating | null) {
  if (!item) return '未披露目标价';
  const low = item.target_price_low;
  const high = item.target_price_high;
  if (low !== null && low !== undefined && high !== null && high !== undefined) {
    return low === high ? formatPrice(low) : `${formatPrice(low)} ~ ${formatPrice(high)}`;
  }
  if (low !== null && low !== undefined) return formatPrice(low);
  if (high !== null && high !== undefined) return formatPrice(high);
  return '未披露目标价';
}

function sentimentLabel(sentiment?: string | null) {
  if (sentiment === 'positive') return '利好';
  if (sentiment === 'negative') return '利空';
  return '中性';
}

function eventTone(event: DailyEvent) {
  if (event.sentiment === 'positive') return '可能偏正面';
  if (event.sentiment === 'negative') return '可能偏负面';
  return event.event_type === 'financial_report' || event.event_type === 'announcement'
    ? '需要结合内容判断影响'
    : '短期情绪影响不明确';
}

function EventCard({ event, featured = false }: { event: DailyEvent; featured?: boolean }) {
  const content = (
    <>
      <div className="daily-event-top">
        <span>{EVENT_LABELS[event.event_type] || event.event_type} · {event.source || '来源未知'}</span>
        <span>{event.event_date}</span>
      </div>
      <div className="daily-event-title">{event.title}</div>
      {event.summary && <div className="daily-event-summary">{event.summary}</div>}
      <div className="daily-event-impact">
        <span className={`daily-sentiment-pill ${event.sentiment || 'neutral'}`}>{sentimentLabel(event.sentiment)}</span>
        <span>{eventTone(event)}</span>
      </div>
    </>
  );

  const className = `daily-event-card ${event.sentiment || 'neutral'} ${featured ? 'daily-event-featured' : ''}`;
  if (!event.url) {
    return <div className={className}>{content}</div>;
  }
  return (
    <a className={className} href={event.url} target="_blank" rel="noopener noreferrer">
      {content}
    </a>
  );
}

function providerLabel(provider: string) {
  if (provider === 'deepseek') return 'DeepSeek';
  if (provider === 'openai') return 'OpenAI';
  return provider;
}

function llmCostLabel(data: DailyReasonData) {
  if (data.cached && data.llm_used) return 'LLM 结果来自缓存';
  if (data.cached) return '本地归因来自缓存';
  if (data.llm_used) return 'DeepSeek/OpenAI 已参与整理';
  if (data.llm_policy?.reason === 'historical_local_only') return '历史日期默认本地归因';
  if (data.llm_policy?.reason === 'request_disabled') return '已按设置跳过 LLM';
  return '当前为本地规则归因';
}

export default function DailyReasonPanel({ symbol, displayName, date, onClose, onOpenMacroChain, onLoadedPrice }: Props) {
  const [data, setData] = useState<DailyReasonData | null>(null);
  const [showSimilar, setShowSimilar] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    setShowSimilar(false);
    axios
      .get(`/api/stocks/${symbol}/daily-reason?date=${date}`)
      .then((res) => {
        const nextData: DailyReasonData = res.data;
        setData(nextData);
        onLoadedPrice?.({
          date: nextData.date,
          open: nextData.price.open,
          high: nextData.price.high,
          low: nextData.price.low,
          close: nextData.price.close,
          change: nextData.change_pct,
        });
      })
      .catch((err) => {
        onLoadedPrice?.(null);
        setError(err.response?.data?.detail || '暂时无法生成当日原因');
      })
      .finally(() => setLoading(false));
  }, [symbol, date, onLoadedPrice]);

  if (showSimilar) {
    return <SimilarDaysPanel symbol={symbol} date={date} onClose={() => setShowSimilar(false)} />;
  }

  const sameDayInfo = data?.events.filter((event) => ['news', 'capital'].includes(event.event_type) && event.event_date === data.date) || [];
  const relatedEvents = data?.events || [];
  const highlightedEvents = relatedEvents.filter((event) => event.sentiment === 'positive' || event.sentiment === 'negative');
  const titleName = data?.display_name || displayName || symbol;

  return (
    <div className="news-panel daily-panel">
      <div className="news-panel-header">
        <h2>{titleName} 当日涨跌原因</h2>
        <span className="news-date-badge">{data?.date || date}</span>
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      {loading && !data ? (
        <div className="news-empty">
          <div className="range-loading">
            <div className="range-spinner" />
            <span>正在整理当日行情与事件...</span>
          </div>
        </div>
      ) : error && !data ? (
        <div className="news-empty">{error}</div>
      ) : data ? (
        <div className="news-list daily-list">
          {loading && (
            <div className="daily-loading-banner">
              <div className="range-spinner" />
              <span>正在更新到 {date}，当前先保留 {data.date} 的分析。</span>
            </div>
          )}
          {error && <div className="daily-error-banner">{error}</div>}
          <div className="range-section daily-section-accent">
            <div className="range-section-title">
              当天新增资讯
              <span className="daily-window">{sameDayInfo.length} 条</span>
            </div>
            {sameDayInfo.length > 0 ? (
              <div className="daily-events">
                {sameDayInfo.map((event) => <EventCard event={event} key={event.id} featured />)}
              </div>
            ) : (
              <p className="daily-muted">
                当前资讯源没有返回这一天的新资讯；下方归因仍会参考前一交易日晚间到当日的公告、财报、新闻和资金动向。
              </p>
            )}
          </div>

          <div className="range-section">
            <div className="range-section-title">市场背景</div>
            <div className="daily-context-grid">
              <div className="daily-context-card">
                <div className="daily-context-label">大盘指数</div>
                {data.market_context?.available ? (
                  <>
                    <div className="daily-context-main">
                      <span>{data.market_context.index_name || data.market_context.index_symbol}</span>
                      <span className={(data.market_context.index_change_pct || 0) >= 0 ? 'up' : 'down'}>
                        {signedPct(data.market_context.index_change_pct)}
                      </span>
                    </div>
                    <div className="daily-context-sub">
                      个股超额 {signedPct(data.market_context.excess_return_pct)} · {data.market_context.relationship}
                      {data.market_context.used_cache ? ' · 使用缓存' : ''}
                    </div>
                  </>
                ) : (
                  <div className="daily-context-empty">
                    {data.market_context?.error || '暂无可用指数行情'}
                  </div>
                )}
              </div>
              <div className="daily-context-card">
                <div className="daily-context-label">北向资金</div>
                {data.fund_context?.available ? (
                  <>
                    <div className="daily-context-main">
                      <span>{data.fund_context.direction || '资金方向'}</span>
                      <span className={(data.fund_context.total_flow || 0) >= 0 ? 'up' : 'down'}>
                        {signedAmount(data.fund_context.total_flow)}
                      </span>
                    </div>
                    <div className="daily-context-sub">
                      {data.fund_context.date} · {data.fund_context.relationship}
                      {data.fund_context.used_cache ? ' · 使用缓存' : ''}
                    </div>
                  </>
                ) : (
                  <div className="daily-context-empty">
                    {data.fund_context?.error || '暂无北向资金背景'}
                  </div>
                )}
              </div>
              <div className="daily-context-card">
                <div className="daily-context-label">行业板块</div>
                {data.industry_context?.available ? (
                  <>
                    <div className="daily-context-main">
                      <span>{data.industry_context.board_name || data.industry_context.industry_name}</span>
                      <span className={(data.industry_context.industry_change_pct || 0) >= 0 ? 'up' : 'down'}>
                        {signedPct(data.industry_context.industry_change_pct)}
                      </span>
                    </div>
                    <div className="daily-context-sub">
                      个股超额 {signedPct(data.industry_context.excess_return_pct)} · {data.industry_context.relationship}
                      {data.industry_context.used_cache ? ' · 使用缓存' : ''}
                    </div>
                  </>
                ) : (
                  <div className="daily-context-empty">
                    {data.industry_context?.error || '暂无行业板块背景'}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="range-section">
            <div className="range-section-title">
              投行预期
              {data.analyst_context?.available && (
                <span className="daily-window">{data.analyst_context.ratings?.length || 0} 条</span>
              )}
            </div>
            {data.analyst_context?.available ? (
              <>
                <div className="daily-analyst-summary">
                  <div>
                    <span>最新评级</span>
                    <strong>
                      {data.analyst_context.latest?.institution || '机构'} · {data.analyst_context.latest?.rating || '未披露评级'}
                    </strong>
                  </div>
                  <div>
                    <span>目标价参考</span>
                    <strong>{targetPriceText(data.analyst_context.latest_with_target || data.analyst_context.latest)}</strong>
                  </div>
                  <div>
                    <span>相对收盘价</span>
                    <strong className={(data.analyst_context.target_upside_pct || 0) >= 0 ? 'up' : 'down'}>
                      {signedPct(data.analyst_context.target_upside_pct)}
                    </strong>
                  </div>
                </div>
                <div className="daily-analyst-list">
                  {(data.analyst_context.ratings || []).slice(0, 4).map((item) => (
                    <div className="daily-analyst-card" key={item.id}>
                      <div className="daily-event-top">
                        <span>{item.report_date} · {item.institution || '机构'}</span>
                        <span>{item.source || '评级数据'}</span>
                      </div>
                      <div className="daily-event-title">
                        {item.rating || '未披露评级'}{item.rating_change ? ` · ${item.rating_change}` : ''}
                      </div>
                      <div className="daily-context-sub">
                        目标价：{targetPriceText(item)}
                        {item.analyst ? ` · 分析师：${item.analyst}` : ''}
                        {item.previous_rating ? ` · 前次：${item.previous_rating}` : ''}
                      </div>
                    </div>
                  ))}
                </div>
                {data.analyst_context.used_cache && <div className="daily-meta">机构预期使用本地缓存。</div>}
              </>
            ) : (
              <p className="daily-muted">{data.analyst_context?.error || '暂无可用机构评级/目标价'}</p>
            )}
          </div>

          <div className="range-section daily-section-accent">
            <div className="range-section-title">
              宏观与产业链
              <span className={`daily-quality ${data.macro_chain_context?.evidence_quality || 'low'}`}>
                {data.macro_chain_context?.available ? '已缓存' : '待生成'}
              </span>
            </div>
            <p className="range-summary">
              {data.macro_chain_context?.summary || '暂无宏观与产业链缓存，点击后可生成政策、上下游和板块联动研究。'}
            </p>
            {(data.macro_chain_context?.policy_summary || []).slice(0, 2).map((item, index) => (
              <div className="daily-context-sub" key={`policy-${index}`}>政策：{item}</div>
            ))}
            {(data.macro_chain_context?.transmission_paths || []).slice(0, 2).map((item, index) => (
              <div className="daily-context-sub" key={`path-${index}`}>传导：{item}</div>
            ))}
            <button
              type="button"
              className="range-news-ai-btn stock-report-refresh"
              onClick={() => onOpenMacroChain?.(data.date)}
            >
              查看联动研究
            </button>
          </div>

          <div className="range-section">
            <div className="range-section-title">
              关联事件影响
              <span className="daily-window">{data.event_window.start} ~ {data.event_window.end}</span>
            </div>
            {relatedEvents.length > 0 ? (
              <>
                {highlightedEvents.length > 0 && (
                  <div className="daily-impact-strip">
                    {highlightedEvents.slice(0, 4).map((event) => (
                      <a
                        key={event.id}
                        className={`daily-impact-chip ${event.sentiment || 'neutral'}`}
                        href={event.url || undefined}
                        target={event.url ? '_blank' : undefined}
                        rel={event.url ? 'noopener noreferrer' : undefined}
                      >
                        {sentimentLabel(event.sentiment)} · {event.title}
                      </a>
                    ))}
                  </div>
                )}
                <div className="daily-events">
                  {relatedEvents.map((event) => <EventCard event={event} key={event.id} />)}
                </div>
              </>
            ) : (
              <p className="daily-muted">这一天附近没有匹配到新闻、公告或财报。</p>
            )}
          </div>

          <div className="range-section">
            <div className="range-section-title">
              可能归因
              <span className={`daily-quality ${data.analysis.evidence_quality}`}>
                {QUALITY_LABELS[data.analysis.evidence_quality] || data.analysis.evidence_quality}
              </span>
            </div>
            <p className="range-summary">{data.analysis.summary}</p>
            <div className="daily-meta">
              {data.analysis.analysis_mode === 'parallel_review'
                ? `双模型复核 · ${providerLabel(data.analysis.reviewer_provider || 'openai')} 汇总`
                : llmCostLabel(data)}
              {data.llm_policy?.reason === 'historical_local_only' && data.llm_policy.recent_auto_days !== undefined
                ? ` · 仅最近 ${data.llm_policy.recent_auto_days} 天自动用 LLM`
                : ''}
              {data.llm_error ? ` · LLM 降级：${data.llm_error}` : ''}
            </div>
          </div>

          {(data.analysis.model_consensus || (data.analysis.model_disagreements || []).length > 0 || (data.analysis.model_reviews || []).length > 0) && (
            <div className="range-section">
              <div className="range-section-title">模型复核</div>
              {data.analysis.model_consensus && (
                <p className="range-summary">{data.analysis.model_consensus}</p>
              )}
              {(data.analysis.model_disagreements || []).length > 0 && (
                <ul className="range-events">
                  {(data.analysis.model_disagreements || []).map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              )}
              {(data.analysis.model_reviews || []).length > 0 && (
                <div className="daily-model-grid">
                  {(data.analysis.model_reviews || []).map((review) => (
                    <div className={`daily-model-card ${review.ok ? 'ok' : 'failed'}`} key={`${review.provider}-${review.model}`}>
                      <div className="daily-context-label">{providerLabel(review.provider)}</div>
                      <div className="daily-model-main">{review.ok ? '已完成独立分析' : '分析失败'}</div>
                      <div className="daily-context-sub">
                        {review.ok ? review.analysis?.summary || review.model : review.error || review.model}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {data.analysis.possible_reasons.length > 0 && (
            <div className="range-section">
              <div className="range-section-title">证据线索</div>
              <ul className="range-events">
                {data.analysis.possible_reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            </div>
          )}

          {(data.analysis.bullish_factors.length > 0 || data.analysis.bearish_factors.length > 0) && (
            <div className="daily-factor-grid">
              <div className="range-section">
                <div className="range-section-title bullish">利好因素</div>
                {data.analysis.bullish_factors.length > 0 ? (
                  <ul className="range-events">
                    {data.analysis.bullish_factors.map((item, index) => <li key={index}>{item}</li>)}
                  </ul>
                ) : (
                  <p className="daily-muted">暂无明确利好。</p>
                )}
              </div>
              <div className="range-section">
                <div className="range-section-title bearish">利空因素</div>
                {data.analysis.bearish_factors.length > 0 ? (
                  <ul className="range-events">
                    {data.analysis.bearish_factors.map((item, index) => <li key={index}>{item}</li>)}
                  </ul>
                ) : (
                  <p className="daily-muted">暂无明确利空。</p>
                )}
              </div>
            </div>
          )}

          <button className="range-news-ai-btn" onClick={() => setShowSimilar(true)}>
            查看相似日期
          </button>
        </div>
      ) : null}
    </div>
  );
}
