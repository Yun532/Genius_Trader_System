import { useState, useEffect, useCallback, useRef, type CSSProperties, type PointerEvent } from 'react';
import axios from 'axios';
import StockSelector from './components/StockSelector';
import CandlestickChart from './components/CandlestickChart';
import NewsPanel from './components/NewsPanel';
import NewsCategoryPanel from './components/NewsCategoryPanel';
import RangeAnalysisPanel from './components/RangeAnalysisPanel';
import RangeQueryPopup from './components/RangeQueryPopup';
import RangeNewsPanel from './components/RangeNewsPanel';
import DailyReasonPanel from './components/DailyReasonPanel';
import EventDeepPanel from './components/EventDeepPanel';
import SimilarNewsPanel from './components/SimilarNewsPanel';
import SignalReferencePanel from './components/SignalReferencePanel';
import StockReportPanel from './components/StockReportPanel';
import MacroChainPanel from './components/MacroChainPanel';
import MarketHeatmap from './components/MarketHeatmap';
import './App.css';

interface RangeSelection {
  startDate: string;
  endDate: string;
  priceChange?: number;
  popupX?: number;
  popupY?: number;
}

interface ArticleSelection {
  newsId: string;
  date: string;
  ohlc?: {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
  };
}

interface SyncSource {
  status: string;
  count: number;
  used_cache: boolean;
  error?: string | null;
  min_date?: string | null;
  max_date?: string | null;
}

interface SyncResult {
  symbol: string;
  name?: string | null;
  status: 'success' | 'partial_success' | 'failed';
  events: number;
  sources: Record<string, SyncSource>;
  coverage?: Coverage;
  warnings?: string[];
}

interface WatchlistSyncResult {
  status: 'success' | 'partial_success' | 'failed';
  started_at: string;
  finished_at: string;
  total: number;
  synced: number;
  skipped: number;
  results: SyncResult[];
  skipped_items: Array<{
    symbol: string;
    name?: string | null;
    display_name?: string | null;
    reason: string;
    coverage?: Coverage;
    sources?: Record<string, SyncSource>;
  }>;
}

interface CoverageItem {
  count: number;
  min_date?: string | null;
  max_date?: string | null;
}

interface Coverage {
  symbol: string;
  prices: CoverageItem;
  news: CoverageItem;
  announcements: CoverageItem;
  financial_reports: CoverageItem;
  daily_reason_cache: CoverageItem;
}

interface StockInfo {
  symbol: string;
  name?: string | null;
  display_name?: string | null;
  last_ohlc_fetch?: string | null;
  latest_ohlc_date?: string | null;
  latest_event_date?: string | null;
}

type AppView = 'stock' | 'market';

const SOURCE_LABELS: Record<string, string> = {
  prices: '日 K',
  news: '新闻',
  web_news: '外部资讯',
  announcements: '公告',
  financial_reports: '财报',
};

function formatDateTime(value?: string | null) {
  if (!value) return '暂无更新';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ').slice(0, 16);
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function describeSyncResult(result: SyncResult) {
  if (result.status === 'success') {
    return `同步完成 · 事件 ${result.events}`;
  }
  if (result.status === 'failed') {
    return '同步失败 · 请稍后重试';
  }
  if (result.sources?.prices?.used_cache) {
    return `部分完成 · 行情使用缓存 · 事件 ${result.events}`;
  }
  return `部分完成 · 事件 ${result.events}`;
}

function describeSource(source: SyncSource) {
  const range = source.min_date && source.max_date ? ` · ${source.min_date}~${source.max_date}` : '';
  if (source.used_cache) return `使用缓存 · ${source.count} 条${range}`;
  if (source.status === 'success' && source.count === 0) return `暂无事件${range}`;
  if (source.status === 'success') return `成功 · ${source.count} 条${range}`;
  if (source.status === 'partial_success') return `部分成功 · ${source.count} 条${range}`;
  return source.error ? `失败 · ${source.error}${range}` : `失败${range}`;
}

function needsCoverageSync(coverage: Coverage) {
  const priceCount = Number(coverage.prices?.count || 0);
  const maxDate = coverage.prices?.max_date;
  if (priceCount < 300) return true;
  if (!maxDate) return true;
  const latest = new Date(`${maxDate}T00:00:00`);
  const ageDays = (Date.now() - latest.getTime()) / 86400000;
  return ageDays > 10;
}

function stockDisplayName(symbol: string, names: Record<string, string>) {
  const name = names[symbol];
  return name && name.toLowerCase() !== symbol.toLowerCase() ? `${name}（${symbol}）` : symbol;
}

function latestPriceDateFromStock(stock: StockInfo) {
  return stock.latest_ohlc_date || stock.last_ohlc_fetch || '';
}

function App() {
  const [appView, setAppView] = useState<AppView>('stock');
  const [activeTickers, setActiveTickers] = useState<string[]>([]);
  const [tickerNames, setTickerNames] = useState<Record<string, string>>({});
  const [tickerLastUpdates, setTickerLastUpdates] = useState<Record<string, string>>({});
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [hoveredDate, setHoveredDate] = useState<string | null>(null);
  const [hoveredOhlc, setHoveredOhlc] = useState<{
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
  } | null>(null);
  const [selectedRange, setSelectedRange] = useState<RangeSelection | null>(null);
  const [rangeQuestion, setRangeQuestion] = useState<string | null>(null);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [selectedDayOhlc, setSelectedDayOhlc] = useState<{
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
  } | null>(null);
  const [selectedArticle, setSelectedArticle] = useState<ArticleSelection | null>(null);
  const [deepArticle, setDeepArticle] = useState<ArticleSelection | null>(null);
  const [similarArticle, setSimilarArticle] = useState<ArticleSelection | null>(null);
  const [showSignalReference, setShowSignalReference] = useState(false);
  const [showStockReport, setShowStockReport] = useState(false);
  const [macroChainDate, setMacroChainDate] = useState<string | null>(null);
  const [syncingSymbol, setSyncingSymbol] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [watchlistSync, setWatchlistSync] = useState<WatchlistSyncResult | null>(null);
  const [watchlistSyncing, setWatchlistSyncing] = useState(false);
  const [watchlistError, setWatchlistError] = useState<string | null>(null);
  const [webNewsSearching, setWebNewsSearching] = useState(false);
  const [webNewsMessage, setWebNewsMessage] = useState<string | null>(null);
  const [webNewsStatus, setWebNewsStatus] = useState<'success' | 'partial_success' | 'failed' | null>(null);
  const [chartRefresh, setChartRefresh] = useState(0);
  const [showSyncDetails, setShowSyncDetails] = useState(false);
  const [lockedArticle, setLockedArticle] = useState<ArticleSelection | null>(null);
  const [activeCategory, setActiveCategory] = useState<string | null>(null);
  const [activeCategoryIds, setActiveCategoryIds] = useState<string[]>([]);
  const [activeCategoryColor, setActiveCategoryColor] = useState<string | null>(null);
  const [rightPanelWidth, setRightPanelWidth] = useState(380);
  const [chartRowHeight, setChartRowHeight] = useState('48vh');

  const chartAreaRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const [chartRect, setChartRect] = useState<DOMRect | undefined>(undefined);
  const autoSyncRef = useRef<Set<string>>(new Set());

  function mergeStockNames(stocks: Array<{ symbol: string; name?: string | null }>) {
    const entries = stocks
      .filter((item) => item.name && item.name.toLowerCase() !== item.symbol.toLowerCase())
      .map((item) => [item.symbol, item.name as string]);
    if (entries.length === 0) return;
    setTickerNames((prev) => ({ ...prev, ...Object.fromEntries(entries) }));
  }

  useEffect(() => {
    axios
      .get('/api/stocks')
      .then((res) => {
        const stocks = (res.data as StockInfo[]).filter((t) => t.last_ohlc_fetch);
        const tickers = stocks.map((t) => t.symbol);
        const names = Object.fromEntries(
          stocks
            .filter((t) => t.name && t.name.toLowerCase() !== t.symbol.toLowerCase())
            .map((t) => [t.symbol, t.name as string])
        );
        setTickerNames(names);
        setTickerLastUpdates(Object.fromEntries(stocks.map((t) => [t.symbol, latestPriceDateFromStock(t)])));
        setActiveTickers(tickers);
        if (tickers.length > 0 && !selectedSymbol) {
          setSelectedSymbol(tickers[0]);
        }
      })
      .catch(console.error);
  }, []);

  useEffect(() => {
    if (selectedRange && chartAreaRef.current) {
      setChartRect(chartAreaRef.current.getBoundingClientRect());
    }
  }, [selectedRange]);

  useEffect(() => {
    if (!selectedSymbol || syncingSymbol) return;
    if (autoSyncRef.current.has(selectedSymbol)) return;
    autoSyncRef.current.add(selectedSymbol);
    axios
      .get<Coverage>(`/api/stocks/${selectedSymbol}/coverage`)
      .then((res) => {
        const maxDate = res.data.prices?.max_date;
        if (maxDate) {
          setTickerLastUpdates((prev) => ({ ...prev, [selectedSymbol]: maxDate }));
        }
        if (!needsCoverageSync(res.data)) return;
        setSyncingSymbol(selectedSymbol);
        setSyncResult(null);
        setSyncError(null);
        setShowSyncDetails(false);
        return axios.post(`/api/stocks/${selectedSymbol}/sync`).then((syncRes) => {
          setSyncResult(syncRes.data);
          setChartRefresh((v) => v + 1);
        });
      })
      .catch((err) => {
        console.error(err);
      })
      .finally(() => {
        setSyncingSymbol(null);
      });
  }, [selectedSymbol, syncingSymbol]);

  const handleHover = useCallback(
    (date: string | null, ohlc?: { date: string; open: number; high: number; low: number; close: number; change: number }) => {
      if (!lockedArticle) {
        setHoveredDate(date);
      }
      setHoveredOhlc(ohlc || null);
    },
    [lockedArticle]
  );

  const handleRangeSelect = useCallback((range: RangeSelection | null) => {
    setSelectedRange(range);
    setRangeQuestion(null);
    if (range) {
      setSelectedDay(null);
      setSelectedDayOhlc(null);
      setSelectedArticle(null);
      setLockedArticle(null);
      setDeepArticle(null);
      setSimilarArticle(null);
      setShowSignalReference(false);
      setShowStockReport(false);
      setMacroChainDate(null);
    }
  }, []);

  const handleArticleSelect = useCallback((article: ArticleSelection | null) => {
    if (article === null) {
      setLockedArticle(null);
      setSelectedArticle(null);
      setDeepArticle(null);
      setSimilarArticle(null);
      return;
    }
    setLockedArticle((prev) => {
      if (prev && prev.newsId === article.newsId) {
        setSelectedArticle(null);
        setSelectedDay(null);
        setSelectedDayOhlc(null);
        setDeepArticle(null);
        setSimilarArticle(null);
        return null;
      }
      setSelectedArticle(article);
      setDeepArticle(article);
      setSimilarArticle(null);
      setSelectedRange(null);
      setRangeQuestion(null);
      setSelectedDay(null);
      setSelectedDayOhlc(article.ohlc || null);
      setShowSignalReference(false);
      setShowStockReport(false);
      setMacroChainDate(null);
      setHoveredDate(article.date);
      return article;
    });
  }, []);

  const handleDayClick = useCallback((date: string, ohlc?: {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
  }) => {
    setSelectedDay(date);
    setSelectedDayOhlc(ohlc || (hoveredOhlc && hoveredOhlc.date === date ? hoveredOhlc : null));
    setSelectedRange(null);
    setRangeQuestion(null);
    setSelectedArticle(null);
    setLockedArticle(null);
    setDeepArticle(null);
    setSimilarArticle(null);
    setShowSignalReference(false);
    setShowStockReport(false);
  }, [hoveredOhlc]);

  const handleOpenDeep = useCallback((newsId: string, date: string) => {
    const next = { newsId, date };
    setSelectedArticle(next);
    setLockedArticle(next);
    setDeepArticle(next);
    setSimilarArticle(null);
    setSelectedDay(null);
    setSelectedDayOhlc((prev) => {
      if (prev?.date === date) return prev;
      if (hoveredOhlc?.date === date) return hoveredOhlc;
      return null;
    });
    setSelectedRange(null);
    setRangeQuestion(null);
    setShowSignalReference(false);
    setShowStockReport(false);
    setHoveredDate(date);
  }, [hoveredOhlc]);

  const handleOpenSimilar = useCallback((newsId: string, date: string) => {
    const next = { newsId, date };
    setSelectedArticle(next);
    setLockedArticle(next);
    setSimilarArticle(next);
    setDeepArticle(null);
    setSelectedDay(null);
    setSelectedDayOhlc((prev) => {
      if (prev?.date === date) return prev;
      if (hoveredOhlc?.date === date) return hoveredOhlc;
      return null;
    });
    setSelectedRange(null);
    setRangeQuestion(null);
    setShowSignalReference(false);
    setShowStockReport(false);
    setHoveredDate(date);
  }, [hoveredOhlc]);

  const handleRangeAsk = useCallback((question: string) => {
    setRangeQuestion(question);
  }, []);

  const handleCategoryChange = useCallback((category: string | null, articleIds: string[], color?: string) => {
    setActiveCategory(category);
    setActiveCategoryIds(articleIds);
    setActiveCategoryColor(color ?? null);
  }, []);

  const handleDailyPriceLoaded = useCallback((ohlc: {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
  } | null) => {
    setSelectedDayOhlc(ohlc);
  }, []);

  function handleSelectSymbol(symbol: string) {
    setAppView('stock');
    setSelectedSymbol(symbol);
    setHoveredDate(null);
    setHoveredOhlc(null);
    setSelectedRange(null);
    setRangeQuestion(null);
    setSelectedDay(null);
    setSelectedDayOhlc(null);
    setSelectedArticle(null);
    setLockedArticle(null);
    setDeepArticle(null);
    setSimilarArticle(null);
    setShowSignalReference(false);
    setShowStockReport(false);
    setActiveCategory(null);
    setActiveCategoryIds([]);
    setActiveCategoryColor(null);
  }

  function handleAddTicker(symbol: string) {
    if (activeTickers.includes(symbol)) {
      handleSelectSymbol(symbol);
      return;
    }
    setSyncingSymbol(symbol);
    setSyncResult(null);
    setSyncError(null);
    setShowSyncDetails(false);
    axios
      .post(`/api/stocks/${symbol}/sync`)
      .then((res) => {
        const normalized = res.data.symbol || symbol;
        const name = res.data.name;
        setSyncResult(res.data);
        if (name && name.toLowerCase() !== normalized.toLowerCase()) {
          setTickerNames((prev) => ({ ...prev, [normalized]: name }));
        }
        const maxDate = res.data.coverage?.prices?.max_date || res.data.sources?.prices?.max_date;
        if (maxDate) {
          setTickerLastUpdates((prev) => ({ ...prev, [normalized]: maxDate }));
        }
        setActiveTickers((prev) => (prev.includes(normalized) ? prev : [...prev, normalized]));
        handleSelectSymbol(normalized);
        setChartRefresh((v) => v + 1);
      })
      .catch((err) => {
        console.error(err);
        setSyncError(err.response?.data?.detail || '同步失败');
      })
      .finally(() => setSyncingSymbol(null));
  }

  function handleSyncWatchlist() {
    setWatchlistSyncing(true);
    setWatchlistError(null);
    setShowSyncDetails(false);
    axios
      .post<WatchlistSyncResult>('/api/stocks/sync-watchlist', null, { params: { stale_only: false } })
      .then((res) => {
        setWatchlistSync(res.data);
        mergeStockNames(res.data.results || []);
        const syncedSymbols = (res.data.results || []).map((item) => item.symbol);
        const updates = Object.fromEntries(
          [...(res.data.results || []), ...(res.data.skipped_items || [])]
            .map((item) => [item.symbol, item.coverage?.prices?.max_date || item.sources?.prices?.max_date])
            .filter((entry): entry is [string, string] => Boolean(entry[1]))
        );
        if (Object.keys(updates).length > 0) {
          setTickerLastUpdates((prev) => ({ ...prev, ...updates }));
        }
        if (syncedSymbols.length > 0) {
          setActiveTickers((prev) => Array.from(new Set([...prev, ...syncedSymbols])).sort());
          setChartRefresh((v) => v + 1);
        }
      })
      .catch((err) => {
        console.error(err);
        setWatchlistError(err.response?.data?.detail || '更新全部失败');
      })
      .finally(() => setWatchlistSyncing(false));
  }

  function handleRefreshWebInfo() {
    if (!selectedSymbol) return;
    setWebNewsSearching(true);
    setWebNewsMessage(null);
    setWebNewsStatus(null);
    const params = selectedDay
      ? { start: selectedDay, end: selectedDay }
      : { max_results: 8 };
    axios
      .post(`/api/stocks/${selectedSymbol}/refresh-web-info`, null, { params })
      .then((res) => {
        const byType = res.data.by_type || {};
        const capitalCount = Number(byType.capital || 0);
        const suffix = capitalCount > 0 ? `，资金 ${capitalCount} 条` : '';
        const prefix = res.data.cached ? '资讯缓存' : '搜索资讯';
        setWebNewsMessage(`${prefix} ${res.data.found} 条${suffix}`);
        setWebNewsStatus(Number(res.data.found || 0) > 0 ? 'success' : 'partial_success');
        setChartRefresh((v) => v + 1);
      })
      .catch((err) => {
        console.error(err);
        setWebNewsMessage(err.response?.data?.detail || '外部资讯搜索失败');
        setWebNewsStatus('failed');
      })
      .finally(() => setWebNewsSearching(false));
  }

  const handleLayoutResize = useCallback((axis: 'vertical' | 'horizontal', event: PointerEvent<HTMLDivElement>) => {
    if (!mainRef.current) return;
    event.preventDefault();
    const rect = mainRef.current.getBoundingClientRect();
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = axis === 'vertical' ? 'col-resize' : 'row-resize';
    document.body.style.userSelect = 'none';

    function clamp(value: number, min: number, max: number) {
      return Math.min(Math.max(value, min), max);
    }

    function onMove(moveEvent: PointerEvent | globalThis.PointerEvent) {
      if (axis === 'vertical') {
        const maxRight = Math.max(320, Math.min(680, rect.width - 460));
        setRightPanelWidth(clamp(rect.right - moveEvent.clientX, 320, maxRight));
      } else {
        const maxChart = Math.max(260, rect.height - 190);
        const nextHeight = clamp(moveEvent.clientY - rect.top, 240, maxChart);
        setChartRowHeight(`${Math.round(nextHeight)}px`);
        if (chartAreaRef.current) {
          setChartRect(chartAreaRef.current.getBoundingClientRect());
        }
      }
    }

    function onUp() {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    }

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }, []);

  const effectiveDate = selectedDay ?? lockedArticle?.date ?? hoveredDate;
  const isLocked = lockedArticle !== null;
  const headerOhlc = selectedDay || lockedArticle ? selectedDayOhlc : hoveredOhlc;
  const categoryStatsDate = selectedDay ?? lockedArticle?.date ?? null;
  const selectedDisplayName = selectedSymbol ? stockDisplayName(selectedSymbol, tickerNames) : '';
  const selectedWatchlistResult = watchlistSync?.results?.find((item) => item.symbol === selectedSymbol);
  const latestUpdate = syncResult?.coverage?.prices?.max_date
    || selectedWatchlistResult?.coverage?.prices?.max_date
    || tickerLastUpdates[selectedSymbol]
    || null;

  function renderResearchPanel() {
    if (showSignalReference) {
      return (
        <SignalReferencePanel
          symbol={selectedSymbol}
          displayName={selectedDisplayName}
          onClose={() => setShowSignalReference(false)}
        />
      );
    }
    if (similarArticle) {
      return (
        <SimilarNewsPanel
          symbol={selectedSymbol}
          newsId={similarArticle.newsId}
          onClose={() => {
            setSimilarArticle(null);
            setDeepArticle(similarArticle);
          }}
        />
      );
    }
    if (deepArticle) {
      return (
        <EventDeepPanel
          symbol={selectedSymbol}
          newsId={deepArticle.newsId}
          onClose={() => {
            setDeepArticle(null);
            setSelectedArticle(null);
            setLockedArticle(null);
          }}
          onSimilar={() => {
            setSimilarArticle(deepArticle);
            setDeepArticle(null);
          }}
        />
      );
    }
    if (selectedRange && rangeQuestion) {
      return (
        <RangeAnalysisPanel
          key={`range-analysis-${selectedSymbol}-${selectedRange.startDate}-${selectedRange.endDate}-${rangeQuestion}-${chartRefresh}`}
          symbol={selectedSymbol}
          displayName={selectedDisplayName}
          startDate={selectedRange.startDate}
          endDate={selectedRange.endDate}
          question={rangeQuestion}
          onClear={() => {
            setSelectedRange(null);
            setRangeQuestion(null);
          }}
        />
      );
    }
    if (selectedRange && !rangeQuestion) {
      return (
        <RangeNewsPanel
          key={`range-news-${selectedSymbol}-${selectedRange.startDate}-${selectedRange.endDate}-${chartRefresh}`}
          symbol={selectedSymbol}
          startDate={selectedRange.startDate}
          endDate={selectedRange.endDate}
          priceChange={selectedRange.priceChange}
          onClose={() => setSelectedRange(null)}
          onAskAI={handleRangeAsk}
        />
      );
    }
    if (selectedDay) {
      return (
        <DailyReasonPanel
          symbol={selectedSymbol}
          displayName={selectedDisplayName}
          date={selectedDay}
          onLoadedPrice={handleDailyPriceLoaded}
          onOpenMacroChain={(date) => setMacroChainDate(date)}
          onClose={() => {
            setSelectedDay(null);
            setSelectedDayOhlc(null);
          }}
        />
      );
    }
    return (
      <div className="news-panel research-placeholder">
        <div className="news-panel-header">
          <h2>研究面板</h2>
        </div>
        <div className="research-empty">
          <div className="research-empty-title">选择一根 K 线或拖选区间</div>
          <div className="research-empty-text">
            这里会显示每日涨跌原因、市场背景、关联事件影响和区间归因。
          </div>
          <div className="research-empty-hint">鼠标滚轮缩放 K 线，按住 Shift 滚轮左右平移，双击图表重置。</div>
          <button
            type="button"
            className="range-news-ai-btn research-signal-btn"
            onClick={() => {
              setShowSignalReference(true);
              setShowStockReport(false);
            }}
          >
            查看信号参考
          </button>
          <button
            type="button"
            className="range-news-ai-btn research-signal-btn"
            onClick={() => setShowStockReport(true)}
          >
            DeepSeek 股票分析
          </button>
          <button
            type="button"
            className="range-news-ai-btn research-signal-btn"
            onClick={() => setMacroChainDate(selectedDay || hoveredDate || latestUpdate || new Date().toISOString().slice(0, 10))}
          >
            联动研究
          </button>
        </div>
      </div>
    );
  }

  function renderEventPanel() {
    return (
      <NewsPanel
        key={`events-${selectedSymbol}-${chartRefresh}`}
        symbol={selectedSymbol}
        hoveredDate={effectiveDate}
        onOpenDeep={handleOpenDeep}
        onFindSimilar={(newsId: string) => {
          if (effectiveDate) handleOpenSimilar(newsId, effectiveDate);
        }}
        highlightedNewsId={selectedArticle?.newsId || null}
        isLocked={isLocked}
        onUnlock={() => {
          setLockedArticle(null);
          setSelectedArticle(null);
        }}
        highlightedCategoryIds={activeCategoryIds.length > 0 ? activeCategoryIds : undefined}
      />
    );
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <h1>天才交易员系统</h1>
          <div className="app-view-toggle" role="tablist" aria-label="工作视图">
            <button
              type="button"
              className={appView === 'stock' ? 'active' : ''}
              onClick={() => setAppView('stock')}
            >
              个股研究
            </button>
            <button
              type="button"
              className={appView === 'market' ? 'active' : ''}
              onClick={() => setAppView('market')}
            >
              大盘云图
            </button>
          </div>
        </div>
        <StockSelector
          activeTickers={activeTickers}
          tickerNames={tickerNames}
          selectedSymbol={selectedSymbol}
          onSelect={handleSelectSymbol}
          onAdd={handleAddTicker}
        />
        {appView === 'stock' && selectedRange ? (
          <div className="header-ohlc">
            <span className="ohlc-date">{selectedRange.startDate} ~ {selectedRange.endDate}</span>
            <span className="range-badge">已选择区间</span>
          </div>
        ) : appView === 'stock' && headerOhlc ? (
          <div className="header-ohlc">
            <span className="ohlc-date">{headerOhlc.date}</span>
            <span className="ohlc-label">O</span>
            <span className="ohlc-val">¥{headerOhlc.open.toFixed(2)}</span>
            <span className="ohlc-label">H</span>
            <span className="ohlc-val">¥{headerOhlc.high.toFixed(2)}</span>
            <span className="ohlc-label">L</span>
            <span className="ohlc-val">¥{headerOhlc.low.toFixed(2)}</span>
            <span className="ohlc-label">C</span>
            <span className="ohlc-val">¥{headerOhlc.close.toFixed(2)}</span>
            <span className={`ohlc-change ${headerOhlc.change >= 0 ? 'up' : 'down'}`}>
              {headerOhlc.change >= 0 ? '+' : ''}
              {headerOhlc.change.toFixed(2)}%
            </span>
          </div>
        ) : null}
        <div className="header-right">
          {selectedSymbol && (
            <span className="last-update-badge">最新行情 {latestUpdate || '待同步'}</span>
          )}
          <button
            className="watchlist-sync-btn"
            type="button"
            onClick={handleSyncWatchlist}
            disabled={watchlistSyncing || activeTickers.length === 0}
            title="同步所有已添加股票"
          >
            {watchlistSyncing ? '更新中...' : '更新全部'}
          </button>
          {selectedSymbol && (
            <button
              className="watchlist-sync-btn"
              type="button"
              onClick={() => {
                setShowStockReport(true);
                setShowSignalReference(false);
                setSelectedDay(null);
                setSelectedDayOhlc(null);
                setSelectedRange(null);
                setRangeQuestion(null);
                setDeepArticle(null);
                setSimilarArticle(null);
                setSelectedArticle(null);
                setLockedArticle(null);
              }}
              title="让 DeepSeek 基于已入库行情、事件、财报、市场背景生成股票分析报告"
            >
              股票分析
            </button>
          )}
          {selectedSymbol && (
            <button
              className="watchlist-sync-btn"
              type="button"
              onClick={handleRefreshWebInfo}
              disabled={webNewsSearching}
              title="用 OpenAI/Tavily 搜索并入库外部资讯，包括新闻、游资、龙虎榜和资金流"
            >
              {webNewsSearching ? '搜索中...' : '搜索资讯'}
            </button>
          )}
          {webNewsMessage && (
            <span className={`sync-badge sync-${webNewsStatus || (webNewsMessage.includes('失败') ? 'failed' : 'success')}`}>
              {webNewsMessage}
            </span>
          )}
          {watchlistSync && !watchlistSyncing && (
            <span className={`sync-badge sync-${watchlistSync.status}`}>
              全部更新 {watchlistSync.synced} 只 · 跳过 {watchlistSync.skipped} · {formatDateTime(watchlistSync.finished_at)}
            </span>
          )}
          {watchlistError && <span className="sync-badge sync-failed">{watchlistError}</span>}
          {syncingSymbol && <span className="sync-badge sync-running">同步 {stockDisplayName(syncingSymbol, tickerNames)}...</span>}
          {!syncingSymbol && syncResult && (
            <div className="sync-wrap">
              <button
                className={`sync-badge sync-${syncResult.status}`}
                type="button"
                onClick={() => setShowSyncDetails((v) => !v)}
              >
                {describeSyncResult(syncResult)}
              </button>
              {showSyncDetails && (
                <div className="sync-detail-popover">
                  <div className="sync-detail-title">
                    {syncResult.name && syncResult.name.toLowerCase() !== syncResult.symbol.toLowerCase()
                      ? `${syncResult.name}（${syncResult.symbol}）`
                      : stockDisplayName(syncResult.symbol, tickerNames)} 同步详情
                  </div>
                  {Object.entries(syncResult.sources || {}).map(([key, source]) => (
                    <div className="sync-detail-row" key={key}>
                      <span className="sync-detail-label">{SOURCE_LABELS[key] || key}</span>
                      <span className={`sync-detail-value source-${source.status}`}>{describeSource(source)}</span>
                    </div>
                  ))}
                  {syncResult.warnings && syncResult.warnings.length > 0 && (
                    <div className="sync-detail-warnings">
                      {syncResult.warnings.map((warning, index) => (
                        <div key={index}>{warning}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {!syncingSymbol && syncError && <span className="sync-badge sync-failed">{syncError}</span>}
        </div>
      </header>

      {appView === 'market' ? (
        <MarketHeatmap
          onSelectStock={(symbol) => {
            setAppView('stock');
            handleSelectSymbol(symbol);
          }}
        />
      ) : (
      <main
        className="app-main"
        ref={mainRef}
        style={{
          '--right-panel-width': `${rightPanelWidth}px`,
          '--chart-row-height': chartRowHeight,
        } as CSSProperties}
      >
        <div className="chart-area" ref={chartAreaRef}>
          {selectedSymbol ? (
            <>
              <CandlestickChart
                key={`${selectedSymbol}-${chartRefresh}`}
                symbol={selectedSymbol}
                lockedNewsId={lockedArticle?.newsId ?? null}
                highlightedArticleIds={activeCategoryIds.length > 0 ? activeCategoryIds : null}
                highlightColor={activeCategoryColor}
                onHover={handleHover}
                onRangeSelect={handleRangeSelect}
                onArticleSelect={handleArticleSelect}
                onDayClick={handleDayClick}
              />
              {selectedRange && !rangeQuestion && (
                <RangeQueryPopup
                  range={selectedRange}
                  chartRect={chartRect}
                  onAsk={handleRangeAsk}
                  onClose={() => setSelectedRange(null)}
                />
              )}
            </>
          ) : (
            <div className="chart-placeholder">输入 A 股代码或名称开始研究</div>
          )}
        </div>
        <div className="news-area">
          {selectedSymbol && (
            <NewsCategoryPanel
              key={`cat-${selectedSymbol}-${categoryStatsDate || 'all'}-${chartRefresh}`}
              symbol={selectedSymbol}
              date={categoryStatsDate}
              activeCategory={activeCategory}
              onCategoryChange={handleCategoryChange}
            />
          )}
          {selectedSymbol ? renderEventPanel() : <div className="news-empty">输入 A 股代码或名称开始研究</div>}
        </div>
        <div className="prediction-area">
          {selectedSymbol ? renderResearchPanel() : (
            <div className="news-panel research-placeholder">
              <div className="news-panel-header">
                <h2>研究面板</h2>
              </div>
              <div className="research-empty">输入 A 股代码后开始研究。</div>
            </div>
          )}
        </div>
        <div
          className="layout-resize-handle layout-resize-vertical"
          title="拖动调整右侧研究面板宽度"
          onPointerDown={(event) => handleLayoutResize('vertical', event)}
        />
        <div
          className="layout-resize-handle layout-resize-horizontal"
          title="拖动调整图表和下方资讯区域高度"
          onPointerDown={(event) => handleLayoutResize('horizontal', event)}
        />
      </main>
      )}
      {showStockReport && selectedSymbol && (
        <div className="stock-report-modal-backdrop" role="dialog" aria-modal="true" aria-label="股票分析报告">
          <div className="stock-report-modal">
            <StockReportPanel
              symbol={selectedSymbol}
              displayName={selectedDisplayName}
              onOpenMacroChain={(date) => setMacroChainDate(date)}
              onClose={() => setShowStockReport(false)}
            />
          </div>
        </div>
      )}
      {macroChainDate && selectedSymbol && (
        <div className="stock-report-modal-backdrop" role="dialog" aria-modal="true" aria-label="联动研究">
          <div className="stock-report-modal">
            <MacroChainPanel
              symbol={selectedSymbol}
              displayName={selectedDisplayName}
              date={macroChainDate}
              onClose={() => setMacroChainDate(null)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
