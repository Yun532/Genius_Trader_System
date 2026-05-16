import { useEffect, useState } from 'react';
import axios from 'axios';

interface NewsItem {
  news_id: string;
  trade_date: string;
  title: string;
  publisher: string;
  article_url: string;
  news_type: string | null;
  key_discussion: string | null;
  sentiment: string | null;
  reason_growth: string | null;
  reason_decrease: string | null;
  ret_t0: number | null;
  ret_t1: number | null;
}

interface RangeNewsResponse {
  total: number;
  date_range: [string, string];
  articles: NewsItem[];
  top_bullish: NewsItem[];
  top_bearish: NewsItem[];
}

interface Props {
  symbol: string;
  startDate: string;
  endDate: string;
  priceChange?: number;
  onClose: () => void;
  onAskAI: (question: string) => void;
}

function pct(v: number | null) {
  if (v === null || v === undefined) return '-';
  const p = v * 100;
  const color = p > 0 ? '#ff3d8b' : p < 0 ? '#00e5a8' : '#888';
  return <span style={{ color, fontWeight: 600 }}>{p > 0 ? '+' : ''}{p.toFixed(2)}%</span>;
}

const TYPE_LABELS: Record<string, string> = {
  news: '新闻',
  announcement: '公告',
  financial_report: '财报',
  capital: '资金/游资',
};

export default function RangeNewsPanel({ symbol, startDate, endDate, priceChange, onClose, onAskAI }: Props) {
  const [data, setData] = useState<RangeNewsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    setLoading(true);
    setData(null);
    setShowAll(false);
    axios
      .get<RangeNewsResponse>(`/api/news/${symbol}/range?start=${startDate}&end=${endDate}`)
      .then((res) => setData(res.data))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [symbol, startDate, endDate]);

  const change = priceChange ?? 0;
  const isUp = change >= 0;

  return (
    <div className="news-panel">
      <div className="news-panel-header">
        <h2>区间事件</h2>
        <span className={`range-news-change ${isUp ? 'up' : 'down'}`}>
          {isUp ? '+' : ''}{change.toFixed(2)}%
        </span>
        <button className="range-clear-btn" onClick={onClose}>关闭</button>
      </div>

      <div className="range-news-dates">
        {startDate} ~ {endDate}
        {data && <span className="news-count" style={{ marginLeft: 8 }}>{data.total} 条事件</span>}
      </div>

      {loading ? (
        <div className="news-empty">正在加载区间事件...</div>
      ) : !data || data.total === 0 ? (
        <div className="news-empty">这个区间暂无新闻、公告、财报或资金资讯</div>
      ) : (
        <div className="news-list">
          {data.top_bullish.length > 0 && (
            <div className="range-news-section">
              <div className="range-news-section-title bullish">利好事件 ({data.top_bullish.length})</div>
              {data.top_bullish.map((item) => (
                <RangeNewsCard key={item.news_id} item={item} />
              ))}
            </div>
          )}

          {data.top_bearish.length > 0 && (
            <div className="range-news-section">
              <div className="range-news-section-title bearish">利空事件 ({data.top_bearish.length})</div>
              {data.top_bearish.map((item) => (
                <RangeNewsCard key={item.news_id} item={item} />
              ))}
            </div>
          )}

          {data.articles.length > 0 && (
            <div className="range-news-all">
              <button className="range-news-all-btn" onClick={() => setShowAll(!showAll)}>
                {showAll ? '收起' : '查看'}全部 {data.total} 条
                <span className="range-news-all-arrow">{showAll ? '▴' : '▾'}</span>
              </button>
              {showAll && data.articles.map((item) => <RangeNewsCard key={item.news_id} item={item} />)}
            </div>
          )}

          <button className="range-news-ai-btn" onClick={() => onAskAI('请解释这段时间股价变化的主要原因')}>
            AI 解释这段涨跌
          </button>
        </div>
      )}
    </div>
  );
}

function RangeNewsCard({ item }: { item: NewsItem }) {
  const sentiment = item.sentiment || 'neutral';
  const borderClass = sentiment === 'positive' ? 'card-positive' : sentiment === 'negative' ? 'card-negative' : 'card-neutral';

  return (
    <div className={`news-card ${borderClass}`}>
      <div className="news-card-top">
        <span className={`sentiment-dot ${sentiment}`} />
        <a href={item.article_url || '#'} target="_blank" rel="noreferrer" className="news-title">
            [{TYPE_LABELS[item.news_type || ''] || item.news_type || '事件'}] {item.title}
        </a>
      </div>

      {item.key_discussion && <p className="news-summary">{item.key_discussion}</p>}

      {(item.reason_growth || item.reason_decrease) && (
        <div className="news-reasons">
          {item.reason_growth && (
            <div className="reason up">
              <span className="reason-icon">+</span> {item.reason_growth}
            </div>
          )}
          {item.reason_decrease && (
            <div className="reason down">
              <span className="reason-icon">-</span> {item.reason_decrease}
            </div>
          )}
        </div>
      )}

      <div className="news-card-footer">
        <span className="news-publisher">{item.trade_date} · {item.publisher}</span>
        <div className="returns-chips">
          <span className="ret-chip">T+0 {pct(item.ret_t0)}</span>
          <span className="ret-chip">T+1 {pct(item.ret_t1)}</span>
        </div>
      </div>
    </div>
  );
}
