import { useEffect, useState } from 'react';
import axios from 'axios';

interface SignalScenario {
  horizon: string;
  base_case: string;
  upside_watch: string;
  downside_watch: string;
}

interface SignalReference {
  symbol: string;
  display_name?: string | null;
  date: string;
  price: {
    close: number;
    change_pct: number;
    ret_20d_pct?: number | null;
  };
  event_summary: {
    count: number;
    positive: number;
    negative: number;
    neutral: number;
    by_type: Record<string, number>;
  };
  similar_days?: {
    stats?: {
      count?: number;
      up_ratio_t1?: number | null;
      up_ratio_t5?: number | null;
      avg_ret_t1?: number | null;
      avg_ret_t5?: number | null;
    };
  };
  local_view: {
    summary: string;
    watch_points: string[];
    risk_points: string[];
    evidence_quality: string;
  };
  scenario_analysis: {
    summary: string;
    scenarios: SignalScenario[];
    do_not_trade_on?: string[];
    evidence_quality?: string;
  };
  llm_used: boolean;
  llm_error?: string | null;
  llm_policy?: {
    use_llm: boolean;
    reason: string;
    recent_auto_days?: number;
  };
  llm_cache?: {
    hit: boolean;
    created_at?: string;
    expires_at?: string;
  };
}

interface Props {
  symbol: string;
  displayName?: string;
  onClose: () => void;
}

function signedPct(value?: number | null) {
  if (value === null || value === undefined) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function statPct(value?: number | null) {
  if (value === null || value === undefined) return '--';
  const pctValue = Math.abs(value) <= 1 ? value * 100 : value;
  return `${pctValue.toFixed(1)}%`;
}

function llmCostLabel(data: SignalReference) {
  if (data.llm_cache?.hit) return 'LLM 结果来自缓存';
  if (data.llm_used) return 'LLM 情景推演已生成';
  if (data.llm_policy?.reason === 'request_disabled') return '已按设置跳过 LLM';
  if (data.llm_policy?.reason === 'historical_local_only') return '历史日期默认本地分析';
  return '本地统计参考';
}

export default function SignalReferencePanel({ symbol, displayName, onClose }: Props) {
  const [data, setData] = useState<SignalReference | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    axios
      .post<SignalReference>(`/api/stocks/${symbol}/signal-reference`, { window_days: 30, top_k: 10 })
      .then((res) => setData(res.data))
      .catch((err) => setError(err.response?.data?.detail || '信号参考暂时不可用'))
      .finally(() => setLoading(false));
  }, [symbol]);

  const title = data?.display_name || displayName || symbol;
  const stats = data?.similar_days?.stats || {};

  return (
    <div className="news-panel signal-panel">
      <div className="news-panel-header">
        <h2>信号参考</h2>
        {data?.date && <span className="news-date-badge">{data.date}</span>}
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      {loading ? (
        <div className="news-empty">正在整理统计参考...</div>
      ) : error ? (
        <div className="news-empty">{error}</div>
      ) : data ? (
        <div className="news-list daily-list">
          <div className="range-section daily-section-accent">
            <div className="range-section-title">{title}</div>
            <p className="range-summary">{data.scenario_analysis.summary || data.local_view.summary}</p>
            <div className="daily-meta">
              {llmCostLabel(data)}
              {data.llm_error ? ` · LLM 降级：${data.llm_error}` : ''}
              {data.llm_cache?.expires_at ? ` · 缓存至 ${data.llm_cache.expires_at.slice(0, 16).replace('T', ' ')}` : ''}
            </div>
          </div>

          <div className="daily-context-grid">
            <div className="daily-context-card">
              <div className="daily-context-label">最新收盘</div>
              <div className="daily-context-main">
                <span>{data.price.close.toFixed(2)}</span>
                <span className={data.price.change_pct >= 0 ? 'up' : 'down'}>{signedPct(data.price.change_pct)}</span>
              </div>
              <div className="daily-context-sub">20 日 {signedPct(data.price.ret_20d_pct)}</div>
            </div>
            <div className="daily-context-card">
              <div className="daily-context-label">近 30 日事件</div>
              <div className="daily-context-main">
                <span>{data.event_summary.count} 条</span>
                <span>{data.event_summary.positive}+ / {data.event_summary.negative}-</span>
              </div>
              <div className="daily-context-sub">
                新闻 {data.event_summary.by_type.news || 0} · 资金 {data.event_summary.by_type.capital || 0} · 公告 {data.event_summary.by_type.announcement || 0} · 财报 {data.event_summary.by_type.financial_report || 0}
              </div>
            </div>
            <div className="daily-context-card">
              <div className="daily-context-label">相似历史</div>
              <div className="daily-context-main">
                <span>{stats.count || 0} 个</span>
                <span>T+5 {statPct(stats.up_ratio_t5)}</span>
              </div>
              <div className="daily-context-sub">平均 T+5 {signedPct(stats.avg_ret_t5)}</div>
            </div>
          </div>

          <div className="range-section">
            <div className="range-section-title">
              情景推演
              <span className={`daily-quality ${data.scenario_analysis.evidence_quality || data.local_view.evidence_quality}`}>
                {data.scenario_analysis.evidence_quality || data.local_view.evidence_quality}
              </span>
            </div>
            <div className="signal-scenario-list">
              {(data.scenario_analysis.scenarios || []).map((scenario) => (
                <div className="signal-scenario-card" key={scenario.horizon}>
                  <div className="signal-scenario-title">{scenario.horizon}</div>
                  <p>{scenario.base_case}</p>
                  <div className="signal-watch up">观察上行：{scenario.upside_watch}</div>
                  <div className="signal-watch down">观察风险：{scenario.downside_watch}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="range-section">
            <div className="range-section-title">观察点</div>
            <ul className="range-events">
              {data.local_view.watch_points.map((item, index) => <li key={index}>{item}</li>)}
            </ul>
          </div>

          <div className="range-section">
            <div className="range-section-title bearish">限制</div>
            <ul className="range-events">
              {[...(data.scenario_analysis.do_not_trade_on || []), ...data.local_view.risk_points].map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
