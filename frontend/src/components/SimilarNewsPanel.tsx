import { useState, useEffect } from 'react';
import axios from 'axios';

interface SimilarArticle {
  news_id: string;
  symbol: string;
  title: string;
  trade_date: string | null;
  similarity: number;
  sentiment: string | null;
  ret_t0: number | null;
  ret_t1: number | null;
  ret_t3: number | null;
  ret_t5: number | null;
  ret_t10: number | null;
}

interface SimilarStats {
  total: number;
  cross_ticker_count: number;
  positive_t1_pct: number | null;
  positive_t5_pct: number | null;
  avg_ret_t1: number | null;
  avg_ret_t5: number | null;
  median_ret_t1: number | null;
  median_ret_t5: number | null;
}

interface SimilarResponse {
  query: { news_id: string; symbol: string; title: string | null; trade_date: string | null } | null;
  stats: SimilarStats;
  similar_articles: SimilarArticle[];
}

interface Props {
  newsId: string;
  symbol: string;
  onClose: () => void;
}

function pct(v: number | null) {
  if (v === null || v === undefined) return <span style={{ color: '#666' }}>-</span>;
  const pctVal = v * 100;
  const color = pctVal > 0 ? '#ef4444' : pctVal < 0 ? '#22c55e' : '#888';
  return <span style={{ color, fontWeight: 600 }}>{pctVal > 0 ? '+' : ''}{pctVal.toFixed(2)}%</span>;
}

function statPct(v: number | null) {
  if (v === null) return '-';
  return `${v.toFixed(1)}%`;
}

export default function SimilarNewsPanel({ newsId, symbol, onClose }: Props) {
  const [data, setData] = useState<SimilarResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    axios
      .post<SimilarResponse>('/api/analysis/similar', { news_id: newsId, symbol, top_k: 20 })
      .then((res) => setData(res.data))
      .catch((err) => setError(err.response?.data?.detail || '相似事件暂时不可用'))
      .finally(() => setLoading(false));
  }, [newsId, symbol]);

  return (
    <div className="news-panel similar-panel">
      <div className="news-panel-header">
        <h2>相似事件</h2>
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      {loading ? (
        <div className="news-empty">正在查找相似事件...</div>
      ) : error ? (
        <div className="news-empty">{error}</div>
      ) : !data || data.similar_articles.length === 0 ? (
        <div className="news-empty">暂未找到足够相似事件</div>
      ) : (
        <div className="news-list">
          {data.query?.title && (
            <div className="similar-query-card">
              <div className="similar-query-label">当前事件</div>
              <div className="similar-query-title">{data.query.title}</div>
              <div className="similar-query-meta">
                {data.query.symbol} · {data.query.trade_date || '未知日期'}
              </div>
            </div>
          )}

          <div className="similar-stats-card">
            <div className="similar-stats-header">
              找到 {data.stats.total} 条相似事件，覆盖 {data.stats.cross_ticker_count} 只股票
            </div>
            <div className="similar-stats-grid">
              <div className="similar-stat">
                <span className="similar-stat-label">T+1 上涨占比</span>
                <span className={`similar-stat-val ${(data.stats.positive_t1_pct ?? 0) > 50 ? 'up' : 'down'}`}>
                  {statPct(data.stats.positive_t1_pct)}
                </span>
              </div>
              <div className="similar-stat">
                <span className="similar-stat-label">T+5 上涨占比</span>
                <span className={`similar-stat-val ${(data.stats.positive_t5_pct ?? 0) > 50 ? 'up' : 'down'}`}>
                  {statPct(data.stats.positive_t5_pct)}
                </span>
              </div>
              <div className="similar-stat">
                <span className="similar-stat-label">平均 T+1</span>
                <span className="similar-stat-val">{pct(data.stats.avg_ret_t1)}</span>
              </div>
              <div className="similar-stat">
                <span className="similar-stat-label">平均 T+5</span>
                <span className="similar-stat-val">{pct(data.stats.avg_ret_t5)}</span>
              </div>
            </div>
          </div>

          {/* Similar articles list */}
          {data.similar_articles.map((art) => (
            <div key={`${art.news_id}-${art.symbol}`} className="news-card similar-card">
              <div className="similar-card-top">
                <span className="similar-ticker-badge">{art.symbol}</span>
                <span className="similar-score">相似度 {(art.similarity * 100).toFixed(0)}%</span>
              </div>
              <div className="news-title similar-title">{art.title}</div>
              <div className="news-card-footer">
                <span className="news-publisher">{art.trade_date || '未知日期'}</span>
                <div className="returns-chips">
                  <span className="ret-chip">T+1 {pct(art.ret_t1)}</span>
                  <span className="ret-chip">T+5 {pct(art.ret_t5)}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
