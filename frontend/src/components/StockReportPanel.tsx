import { useEffect, useState } from 'react';
import axios from 'axios';

interface StockReport {
  symbol: string;
  display_name?: string | null;
  date: string;
  start_date: string;
  report: {
    summary: string;
    current_status: string[];
    future_potential: string[];
    positive_factors: string[];
    risk_factors: string[];
    catalysts: string[];
    watch_points: string[];
    evidence_quality: string;
    disclaimer?: string;
  };
  context: {
    price: {
      latest: {
        close: number;
        change_pct?: number | null;
        turnover_rate?: number | null;
        amplitude?: number | null;
      };
      return_window_pct?: number | null;
      return_20d_pct?: number | null;
    };
    event_summary: {
      count: number;
      positive: number;
      negative: number;
      by_type: Record<string, number>;
    };
    macro_chain_context?: {
      available: boolean;
      cached?: boolean;
      sources_count?: number;
      summary?: string | null;
      policy_summary?: string[];
      transmission_paths?: string[];
      evidence_quality?: string;
    };
  };
  llm_used: boolean;
  llm_error?: string | null;
  cache?: {
    hit?: boolean;
    created_at?: string;
    expires_at?: string;
  };
}

interface Props {
  symbol: string;
  displayName?: string;
  onClose: () => void;
  onOpenMacroChain?: (date: string) => void;
}

function signedPct(value?: number | null) {
  if (value === null || value === undefined) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function SectionList({ title, items, tone }: { title: string; items?: string[]; tone?: 'bullish' | 'bearish' }) {
  const cleanItems = (items || []).filter(Boolean);
  if (cleanItems.length === 0) return null;
  return (
    <div className="range-section">
      <div className={`range-section-title ${tone || ''}`}>{title}</div>
      <ul className="range-events">
        {cleanItems.map((item, index) => <li key={index}>{item}</li>)}
      </ul>
    </div>
  );
}

export default function StockReportPanel({ symbol, displayName, onClose, onOpenMacroChain }: Props) {
  const [data, setData] = useState<StockReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  function loadReport(refresh = false) {
    setLoading(true);
    setError('');
    axios
      .post<StockReport>(`/api/stocks/${symbol}/stock-report`, {
        lookback_days: 180,
        force_llm: true,
        refresh_cache: refresh,
      })
      .then((res) => setData(res.data))
      .catch((err) => setError(err.response?.data?.detail || '股票分析暂时不可用'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadReport(false);
  }, [symbol]);

  const title = data?.display_name || displayName || symbol;
  const latest = data?.context.price.latest;
  const eventSummary = data?.context.event_summary;

  return (
    <div className="news-panel stock-report-panel">
      <div className="news-panel-header">
        <h2>股票分析报告</h2>
        {data?.date && <span className="news-date-badge">{data.date}</span>}
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      {loading ? (
        <div className="news-empty">DeepSeek 正在整理这只股票的现状和潜力...</div>
      ) : error ? (
        <div className="news-empty">{error}</div>
      ) : data ? (
        <div className="news-list daily-list">
          <div className="range-section daily-section-accent">
            <div className="range-section-title">{title}</div>
            <p className="range-summary">{data.report.summary}</p>
            <div className="daily-meta">
              {data.cache?.hit ? '今天已有缓存报告' : data.llm_used ? 'DeepSeek 已生成报告' : '本地报告'}
              {data.cache?.expires_at ? ` · 缓存至 ${data.cache.expires_at.slice(0, 16).replace('T', ' ')}` : ''}
              {data.llm_error ? ` · LLM 降级：${data.llm_error}` : ''}
            </div>
            {data.cache?.hit && (
              <button className="range-news-ai-btn stock-report-refresh" onClick={() => loadReport(true)}>
                重新生成今日报告
              </button>
            )}
          </div>

          {latest && eventSummary && (
            <div className="daily-context-grid">
              <div className="daily-context-card">
                <div className="daily-context-label">最新收盘</div>
                <div className="daily-context-main">
                  <span>{latest.close.toFixed(2)}</span>
                  <span className={(latest.change_pct || 0) >= 0 ? 'up' : 'down'}>{signedPct(latest.change_pct)}</span>
                </div>
                <div className="daily-context-sub">近 20 日 {signedPct(data.context.price.return_20d_pct)}</div>
              </div>
              <div className="daily-context-card">
                <div className="daily-context-label">研究窗口</div>
                <div className="daily-context-main">
                  <span>{eventSummary.count} 条事件</span>
                  <span>{eventSummary.positive}+ / {eventSummary.negative}-</span>
                </div>
                <div className="daily-context-sub">
                  新闻 {eventSummary.by_type.news || 0} · 资金 {eventSummary.by_type.capital || 0} · 公告 {eventSummary.by_type.announcement || 0} · 财报 {eventSummary.by_type.financial_report || 0}
                </div>
              </div>
            </div>
          )}

          <div className="range-section daily-section-accent">
            <div className="range-section-title">
              宏观与产业链
              <span className={`daily-quality ${data.context.macro_chain_context?.evidence_quality || 'low'}`}>
                {data.context.macro_chain_context?.available ? '已缓存' : '待生成'}
              </span>
            </div>
            <p className="range-summary">
              {data.context.macro_chain_context?.summary || '暂无宏观与产业链缓存，可打开联动研究后手动生成。'}
            </p>
            {(data.context.macro_chain_context?.transmission_paths || []).slice(0, 3).map((item, index) => (
              <div className="daily-context-sub" key={index}>传导：{item}</div>
            ))}
            <button
              type="button"
              className="range-news-ai-btn stock-report-refresh"
              onClick={() => onOpenMacroChain?.(data.date)}
            >
              查看联动研究
            </button>
          </div>

          <SectionList title="当前状态" items={data.report.current_status} />
          <SectionList title="未来潜力" items={data.report.future_potential} tone="bullish" />
          <SectionList title="正面因素" items={data.report.positive_factors} tone="bullish" />
          <SectionList title="主要风险" items={data.report.risk_factors} tone="bearish" />
          <SectionList title="后续催化与验证点" items={data.report.catalysts} />
          <SectionList title="观察清单" items={data.report.watch_points} />

          <div className="range-section">
            <div className="range-section-title">
              证据质量
              <span className={`daily-quality ${data.report.evidence_quality || 'low'}`}>{data.report.evidence_quality || 'low'}</span>
            </div>
            <p className="daily-muted">{data.report.disclaimer || '这是研究报告草稿，不构成买卖建议。'}</p>
          </div>
        </div>
      ) : null}
    </div>
  );
}
