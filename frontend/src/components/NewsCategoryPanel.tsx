import { useEffect, useState } from 'react';
import axios from 'axios';

interface CategoryInfo {
  label: string;
  count: number;
  article_ids: string[];
  positive_ids: string[];
  negative_ids: string[];
  neutral_ids: string[];
}

interface CategoriesResponse {
  categories: Record<string, CategoryInfo>;
  total: number;
}

interface Props {
  symbol: string;
  date?: string | null;
  activeCategory: string | null;
  onCategoryChange: (category: string | null, articleIds: string[], color?: string) => void;
}

const CATEGORY_META: Record<string, { label: string; color: string }> = {
  news: { label: '新闻', color: '#38bdf8' },
  announcement: { label: '公告', color: '#f59e0b' },
  financial_report: { label: '财报', color: '#00e5a8' },
  policy: { label: '政策', color: '#a78bfa' },
  capital: { label: '资金', color: '#ff3d8b' },
};

const PRIMARY_CATEGORY_KEYS = ['news', 'announcement', 'financial_report', 'capital'];

type SentimentFilter = 'all' | 'positive' | 'negative';

export default function NewsCategoryPanel({ symbol, date, activeCategory, onCategoryChange }: Props) {
  const [categories, setCategories] = useState<Record<string, CategoryInfo>>({});
  const [sentimentFilter, setSentimentFilter] = useState<SentimentFilter>('all');

  useEffect(() => {
    if (!symbol) return;
    const params = date ? { date } : undefined;
    axios
      .get<CategoriesResponse>(`/api/news/${symbol}/categories`, { params })
      .then((res) => setCategories(res.data.categories))
      .catch(() => setCategories({}));
  }, [symbol, date]);

  useEffect(() => {
    setSentimentFilter('all');
  }, [activeCategory]);

  const keys = PRIMARY_CATEGORY_KEYS.filter((key) => categories[key]);

  const emptyAllCategory: CategoryInfo = {
    label: '全部事件',
    count: 0,
    article_ids: [],
    positive_ids: [],
    negative_ids: [],
    neutral_ids: [],
  };

  const allIds = new Set<string>();
  const positiveIds = new Set<string>();
  const negativeIds = new Set<string>();
  const neutralIds = new Set<string>();
  for (const key of keys) {
    const category = categories[key];
    category.article_ids.forEach((id) => allIds.add(id));
    category.positive_ids.forEach((id) => positiveIds.add(id));
    category.negative_ids.forEach((id) => negativeIds.add(id));
    category.neutral_ids.forEach((id) => neutralIds.add(id));
  }
  const allCategory: CategoryInfo = {
    ...emptyAllCategory,
    count: allIds.size,
    article_ids: Array.from(allIds),
    positive_ids: Array.from(positiveIds),
    negative_ids: Array.from(negativeIds),
    neutral_ids: Array.from(neutralIds),
  };
  const activeKey = activeCategory && categories[activeCategory]?.count > 0 ? activeCategory : null;

  function idsForFilter(category: CategoryInfo, filter: SentimentFilter) {
    if (filter === 'positive') return category.positive_ids;
    if (filter === 'negative') return category.negative_ids;
    return category.article_ids;
  }

  function colorForFilter(filter: SentimentFilter, fallback: string) {
    if (filter === 'positive') return '#ff3d8b';
    if (filter === 'negative') return '#00e5a8';
    return fallback;
  }

  function handleSentimentClick(filter: SentimentFilter) {
    const category = activeKey ? categories[activeKey] : allCategory;
    const meta = activeKey ? CATEGORY_META[activeKey] || { color: '#667eea', label: activeKey } : { color: '#667eea', label: '全部事件' };
    setSentimentFilter(filter);
    const ids = filter === 'all' && !activeKey ? [] : idsForFilter(category, filter);
    onCategoryChange(activeKey, ids, colorForFilter(filter, meta.color));
  }

  const activeCat = activeKey ? categories[activeKey] : allCategory;

  useEffect(() => {
    if (keys.length === 0) {
      onCategoryChange(null, []);
      return;
    }
    const category = activeKey ? categories[activeKey] : allCategory;
    const meta = activeKey ? CATEGORY_META[activeKey] || { color: '#667eea', label: activeKey } : { color: '#667eea', label: '全部事件' };
    const ids = sentimentFilter === 'all' && !activeKey ? [] : idsForFilter(category, sentimentFilter);
    onCategoryChange(activeKey, ids, colorForFilter(sentimentFilter, meta.color));
  }, [categories, date]);

  if (keys.length === 0) return null;

  return (
    <div className="news-category-wrap">
      <div className="news-category-bar">
        {keys.map((key) => {
          const category = categories[key] || emptyAllCategory;
          const meta = CATEGORY_META[key] || { label: key, color: '#667eea' };
          const isActive = activeKey === key;
          return (
            <button
              key={key}
              className={`category-tag ${isActive ? 'category-tag-active' : ''}`}
              style={{
                '--tag-color': meta.color,
                '--tag-color-bg': `${meta.color}18`,
                '--tag-color-bg-active': `${meta.color}30`,
              } as React.CSSProperties}
              onClick={() => {
                if (isActive) {
                  onCategoryChange(null, []);
                } else {
                  setSentimentFilter('all');
                  onCategoryChange(key, category.article_ids, meta.color);
                }
              }}
            >
              <div className="category-tag-body">
                <span className="category-tag-label">{meta.label}</span>
                <span className="category-tag-count">{category.count} 条</span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="sentiment-sub-bar">
        <button
          className={`sentiment-sub-btn ${sentimentFilter === 'all' ? 'sentiment-sub-active' : ''}`}
          onClick={() => handleSentimentClick('all')}
        >
          全部 <span className="sentiment-sub-count">{activeCat.count}</span>
        </button>
        <button
          className={`sentiment-sub-btn sentiment-sub-up ${sentimentFilter === 'positive' ? 'sentiment-sub-active' : ''}`}
          onClick={() => handleSentimentClick('positive')}
        >
          利好 <span className="sentiment-sub-count">{activeCat.positive_ids.length}</span>
        </button>
        <button
          className={`sentiment-sub-btn sentiment-sub-down ${sentimentFilter === 'negative' ? 'sentiment-sub-active' : ''}`}
          onClick={() => handleSentimentClick('negative')}
        >
          利空 <span className="sentiment-sub-count">{activeCat.negative_ids.length}</span>
        </button>
      </div>
    </div>
  );
}
