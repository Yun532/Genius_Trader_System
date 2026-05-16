import { useEffect, useState } from 'react';
import axios from 'axios';

interface DeepArticle {
  title?: string | null;
  description?: string | null;
  publisher?: string | null;
  article_url?: string | null;
  news_type?: string | null;
  event_type?: string | null;
  trade_date?: string | null;
  event_date?: string | null;
  sentiment?: string | null;
  ret_t0?: number | null;
  ret_t1?: number | null;
  ret_t5?: number | null;
}

interface DeepResponse {
  news_id: string;
  symbol: string;
  article?: DeepArticle;
  discussion?: string;
  growth_reasons?: string;
  decrease_reasons?: string;
  impact_path?: string[];
  evidence_quality?: string;
  llm_used?: boolean;
  llm_error?: string | null;
  cached?: boolean;
  error?: string;
}

interface Props {
  symbol: string;
  newsId: string;
  onClose: () => void;
  onSimilar: () => void;
}

const TYPE_LABELS: Record<string, string> = {
  news: '新闻',
  announcement: '公告',
  financial_report: '财报',
  capital: '资金/游资',
};

function pct(value?: number | null) {
  if (value === null || value === undefined) return '--';
  const pctValue = value * 100;
  return `${pctValue >= 0 ? '+' : ''}${pctValue.toFixed(2)}%`;
}

function sentimentLabel(value?: string | null) {
  if (value === 'positive') return '利好';
  if (value === 'negative') return '利空';
  return '中性';
}

export default function EventDeepPanel({ symbol, newsId, onClose, onSimilar }: Props) {
  const [data, setData] = useState<DeepResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    axios
      .post<DeepResponse>('/api/analysis/deep', { symbol, news_id: newsId })
      .then((res) => {
        if (res.data.error) {
          setError(res.data.error);
          setData(null);
        } else {
          setData(res.data);
        }
      })
      .catch((err) => setError(err.response?.data?.detail || '事件深挖暂时不可用'))
      .finally(() => setLoading(false));
  }, [symbol, newsId]);

  const article = data?.article;
  const eventType = article?.event_type || article?.news_type || 'event';
  const date = article?.trade_date || article?.event_date || '';

  return (
    <div className="news-panel deep-panel">
      <div className="news-panel-header">
        <h2>事件深挖</h2>
        {date && <span className="news-date-badge">{date}</span>}
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      {loading ? (
        <div className="news-empty">正在整理事件影响...</div>
      ) : error ? (
        <div className="news-empty">{error}</div>
      ) : data && article ? (
        <div className="news-list deep-list">
          <div className="range-section daily-section-accent">
            <div className="daily-event-top">
              <span>{TYPE_LABELS[eventType] || eventType} · {article.publisher || '来源未知'}</span>
              <span className={`daily-sentiment-pill ${article.sentiment || 'neutral'}`}>{sentimentLabel(article.sentiment)}</span>
            </div>
            {article.article_url ? (
              <a className="deep-title" href={article.article_url} target="_blank" rel="noopener noreferrer">
                {article.title}
              </a>
            ) : (
              <div className="deep-title">{article.title}</div>
            )}
            {article.description && <p className="daily-muted">{article.description}</p>}
            <div className="deep-return-row">
              <span>T0 {pct(article.ret_t0)}</span>
              <span>T+1 {pct(article.ret_t1)}</span>
              <span>T+5 {pct(article.ret_t5)}</span>
            </div>
          </div>

          <div className="range-section">
            <div className="range-section-title">
              影响分析
              <span className={`daily-quality ${data.evidence_quality || 'low'}`}>{data.evidence_quality || 'low'}</span>
            </div>
            <p className="range-summary">{data.discussion}</p>
            <div className="daily-meta">
              {data.llm_used ? 'LLM 已参与整理' : '本地规则分析'}
              {data.cached ? ' · 已读缓存' : ''}
              {data.llm_error ? ` · LLM 降级：${data.llm_error}` : ''}
            </div>
          </div>

          <div className="daily-factor-grid">
            <div className="range-section">
              <div className="range-section-title bullish">利好路径</div>
              <p className="daily-muted">{data.growth_reasons || '暂无明确利好路径。'}</p>
            </div>
            <div className="range-section">
              <div className="range-section-title bearish">利空路径</div>
              <p className="daily-muted">{data.decrease_reasons || '暂无明确利空路径。'}</p>
            </div>
          </div>

          {(data.impact_path || []).length > 0 && (
            <div className="range-section">
              <div className="range-section-title">证据路径</div>
              <ul className="range-events">
                {(data.impact_path || []).map((item, index) => <li key={index}>{item}</li>)}
              </ul>
            </div>
          )}

          <button className="range-news-ai-btn" onClick={onSimilar}>查看相似事件</button>
        </div>
      ) : null}
    </div>
  );
}
