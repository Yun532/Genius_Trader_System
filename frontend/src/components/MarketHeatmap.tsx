import { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import * as d3 from 'd3';

type Period = '1d' | '5d' | '20d';

interface MarketConstituent {
  symbol: string;
  name?: string | null;
  display_name?: string | null;
  change_pct?: number | null;
  amount?: number | null;
  close?: number | null;
  source?: string | null;
}

interface MarketSector {
  board_name: string;
  sector_group?: string;
  board_code?: string;
  date?: string;
  period: Period;
  change_pct?: number | null;
  amount?: number | null;
  weight: number;
  constituent_count: number;
  constituents: MarketConstituent[];
  top_gainers: MarketConstituent[];
  top_losers: MarketConstituent[];
  top_active: MarketConstituent[];
  limit_up?: MarketConstituent[];
  strong_stocks?: MarketConstituent[];
  limit_up_count?: number;
  leader?: {
    name?: string | null;
    change_pct?: number | null;
  };
  quality: 'high' | 'medium' | 'low' | string;
  note?: string;
}

interface MarketHeatmapResponse {
  summary: {
    period: Period;
    date: string;
    sector_count: number;
    breadth: {
      up: number;
      down: number;
      flat: number;
      avg_change_pct?: number | null;
      total_amount?: number | null;
    };
    notes?: string[];
  };
  sectors: MarketSector[];
}

interface Props {
  onSelectStock: (symbol: string) => void;
}

interface TooltipState {
  sector: MarketSector;
  x: number;
  y: number;
}

const PERIOD_LABELS: Record<Period, string> = {
  '1d': '1日',
  '5d': '5日',
  '20d': '20日',
};

type MarketGroupDatum = { name: string; children: MarketSector[] };
type MarketRootDatum = { name: string; children: MarketGroupDatum[] };
type MarketTreeDatum = MarketRootDatum | MarketGroupDatum | MarketSector;
type MarketLeaf = d3.HierarchyRectangularNode<MarketTreeDatum> & { data: MarketSector };
type MarketGroupNode = d3.HierarchyRectangularNode<MarketTreeDatum> & { data: MarketGroupDatum };

const HEATMAP_CACHE_TTL = 5 * 60 * 1000;
const heatmapCache = new Map<Period, { data: MarketHeatmapResponse; fetchedAt: number }>();

function fitText(text: string, width: number, fontSize = 13) {
  if (width <= 18) return '';
  const maxChars = Math.max(1, Math.floor((width - 14) / (fontSize * 0.9)));
  return text.length > maxChars ? `${text.slice(0, Math.max(1, maxChars - 1))}…` : text;
}

function formatAmount(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  if (Math.abs(value) >= 100000000) return `${(value / 100000000).toFixed(1)}亿`;
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return value.toFixed(0);
}

function formatPct(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function sectorColor(change?: number | null) {
  if (change === null || change === undefined || Number.isNaN(change)) return '#293453';
  const intensity = Math.min(1, Math.abs(change) / 7);
  if (change > 0) return d3.interpolateRgb('#753047', '#ff3d8b')(0.28 + intensity * 0.72);
  if (change < 0) return d3.interpolateRgb('#124d47', '#00e5a8')(0.28 + intensity * 0.72);
  return '#32405f';
}

function qualityLabel(quality: string) {
  if (quality === 'high') return '高';
  if (quality === 'medium') return '中';
  return '低';
}

export default function MarketHeatmap({ onSelectStock }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [period, setPeriod] = useState<Period>('1d');
  const [data, setData] = useState<MarketHeatmapResponse | null>(null);
  const [selected, setSelected] = useState<MarketSector | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailRefreshing, setDetailRefreshing] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const updateSize = () => {
      setSize({
        width: container.clientWidth,
        height: container.clientHeight,
      });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const cached = heatmapCache.get(period);
    if (refreshKey === 0 && cached && Date.now() - cached.fetchedAt < HEATMAP_CACHE_TTL) {
      setData(cached.data);
      setSelected((current) => {
        if (!current) return cached.data.sectors[0] || null;
        return cached.data.sectors.find((sector) => sector.board_name === current.board_name) || cached.data.sectors[0] || null;
      });
      setLoading(false);
      setError('');
      return;
    }
    setLoading(true);
    setError('');
    axios
      .get<MarketHeatmapResponse>('/api/market/heatmap', {
        params: { period, max_sectors: 90, hydrate_limit: 24, constituents_limit: 8 },
      })
      .then((res) => {
        heatmapCache.set(period, { data: res.data, fetchedAt: Date.now() });
        setData(res.data);
        setSelected((current) => {
          if (!current) return res.data.sectors[0] || null;
          return res.data.sectors.find((sector) => sector.board_name === current.board_name) || res.data.sectors[0] || null;
        });
      })
      .catch((err) => {
        console.error(err);
        setError(err.response?.data?.detail || '大盘云图加载失败');
      })
      .finally(() => setLoading(false));
  }, [period, refreshKey]);

  const tree = useMemo(() => {
    const width = Math.max(0, size.width);
    const height = Math.max(0, size.height);
    if (!data || width < 80 || height < 80) return null;
    const groups = new Map<string, MarketSector[]>();
    for (const sector of data.sectors) {
      const group = sector.sector_group || '其他行业';
      const items = groups.get(group) || [];
      items.push(sector);
      groups.set(group, items);
    }
    const root = d3
      .hierarchy<MarketTreeDatum>({
        name: 'market',
        children: Array.from(groups.entries()).map(([name, children]) => ({ name, children })),
      })
      .sum((node) => ('weight' in node ? Math.max(1, node.weight || 1) : 0))
      .sort((a, b) => (b.value || 0) - (a.value || 0));

    d3
      .treemap<MarketTreeDatum>()
      .size([width, height])
      .paddingOuter(2)
      .paddingTop((node) => (node.depth === 1 ? 22 : 0))
      .paddingInner((node) => (node.depth === 0 ? 5 : 2))
      .round(true)(root);

    return root;
  }, [data, size]);

  const leaves = useMemo(() => tree?.leaves().map((leaf) => leaf as MarketLeaf) || [], [tree]);
  const groupNodes = useMemo(
    () => tree?.children?.map((node) => node as MarketGroupNode) || [],
    [tree]
  );
  const summary = data?.summary;
  const activeSector = selected || data?.sectors[0] || null;

  useEffect(() => {
    if (!activeSector) return;
    let cancelled = false;
    setDetailLoading(true);
    axios
      .get(`/api/market/sector/${encodeURIComponent(activeSector.board_name)}/constituents`, {
        params: { date: activeSector.date, limit: 30 },
      })
      .then((res) => {
        if (cancelled) return;
        const detail = res.data;
        const patchSector = (sector: MarketSector): MarketSector => {
          if (sector.board_name !== activeSector.board_name) return sector;
          return {
            ...sector,
            constituent_count: detail.constituent_count ?? sector.constituent_count,
            constituents: detail.constituents || [],
            top_gainers: detail.top_gainers || [],
            top_losers: detail.top_losers || [],
            top_active: detail.top_active || [],
            limit_up: detail.limit_up || [],
            strong_stocks: detail.strong_stocks || [],
            limit_up_count: detail.limit_up_count || 0,
            quality: detail.quality || sector.quality,
            note: detail.note || sector.note,
          };
        };
        setSelected((current) => (current ? patchSector(current) : current));
        setData((current) => current ? { ...current, sectors: current.sectors.map(patchSector) } : current);
      })
      .catch((err) => {
        console.error(err);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSector?.board_name, activeSector?.date]);

  function refreshActiveSectorOnline() {
    if (!activeSector || detailRefreshing) return;
    setDetailRefreshing(true);
    axios
      .get(`/api/market/sector/${encodeURIComponent(activeSector.board_name)}/constituents`, {
        params: { date: activeSector.date, limit: 30, refresh: true },
      })
      .then((res) => {
        const detail = res.data;
        const patchSector = (sector: MarketSector): MarketSector => {
          if (sector.board_name !== activeSector.board_name) return sector;
          return {
            ...sector,
            constituent_count: detail.constituent_count ?? sector.constituent_count,
            constituents: detail.constituents || [],
            top_gainers: detail.top_gainers || [],
            top_losers: detail.top_losers || [],
            top_active: detail.top_active || [],
            limit_up: detail.limit_up || [],
            strong_stocks: detail.strong_stocks || [],
            limit_up_count: detail.limit_up_count || 0,
            quality: detail.quality || sector.quality,
            note: detail.note || sector.note,
          };
        };
        setSelected((current) => (current ? patchSector(current) : current));
        setData((current) => current ? { ...current, sectors: current.sectors.map(patchSector) } : current);
      })
      .catch((err) => {
        console.error(err);
      })
      .finally(() => setDetailRefreshing(false));
  }

  function renderStockList(title: string, items: MarketConstituent[], emptyText = '暂无成分股数据') {
    return (
      <div className="market-detail-block">
        <div className="market-detail-subtitle">{title}</div>
        {items.length === 0 ? (
          <div className="market-empty-small">{emptyText}</div>
        ) : (
          <div className="market-stock-list">
            {items.map((item) => (
              <button
                type="button"
                className="market-stock-row"
                key={`${title}-${item.symbol}`}
                onClick={() => onSelectStock(item.symbol)}
                title="打开个股研究"
              >
                <span className="market-stock-name">{item.display_name || item.name || item.symbol}</span>
                <span className="market-stock-symbol">{item.symbol}</span>
                <span className={`market-stock-change ${(item.change_pct || 0) >= 0 ? 'up' : 'down'}`}>
                  {formatPct(item.change_pct)}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <main className="market-page">
      <section className="market-toolbar">
        <div className="market-toolbar-left">
          <h2>大盘云图</h2>
          {summary && (
            <div className="market-summary-line">
              <span>{summary.date}</span>
              <span>{summary.sector_count} 个板块</span>
              <span className="up">上涨 {summary.breadth.up}</span>
              <span className="down">下跌 {summary.breadth.down}</span>
              <span>均值 {formatPct(summary.breadth.avg_change_pct)}</span>
            </div>
          )}
        </div>
        <div className="market-toolbar-actions">
          <div className="market-period-tabs" role="tablist" aria-label="云图周期">
            {(Object.keys(PERIOD_LABELS) as Period[]).map((item) => (
              <button
                type="button"
                key={item}
                className={period === item ? 'active' : ''}
                onClick={() => setPeriod(item)}
              >
                {PERIOD_LABELS[item]}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="market-refresh-btn"
            title="重新读取本地缓存"
            onClick={() => {
              heatmapCache.delete(period);
              setRefreshKey((value) => value + 1);
            }}
          >
            重读缓存
          </button>
        </div>
      </section>

      <section className="market-content">
        <div className="market-heatmap-panel">
          <div ref={containerRef} className="market-heatmap-canvas">
            {loading && <div className="market-loading">Loading...</div>}
            {!loading && error && <div className="market-loading error">{error}</div>}
            {!loading && !error && leaves.length === 0 && <div className="market-loading">暂无可靠板块数据</div>}
            <svg className="market-heatmap-svg" width={size.width} height={size.height}>
              {groupNodes.map((group) => (
                <g
                  key={group.data.name}
                  className="market-group"
                  transform={`translate(${group.x0},${group.y0})`}
                >
                  <rect
                    width={Math.max(0, group.x1 - group.x0)}
                    height={Math.max(0, group.y1 - group.y0)}
                    rx={6}
                  />
                  <text x={9} y={15}>{fitText(group.data.name, Math.max(0, group.x1 - group.x0), 12)}</text>
                </g>
              ))}
              {leaves.map((leaf) => {
                const sector = leaf.data;
                const rectWidth = Math.max(0, leaf.x1 - leaf.x0);
                const rectHeight = Math.max(0, leaf.y1 - leaf.y0);
                const compact = rectWidth < 112 || rectHeight < 58;
                const tiny = rectWidth < 42 || rectHeight < 24;
                const showMeta = rectWidth >= 96 && rectHeight >= 68;
                const nameText = fitText(sector.board_name, rectWidth, compact ? 12 : 13);
                const selectSector = () => setSelected(sector);
                return (
                  <g
                    key={sector.board_name}
                    transform={`translate(${leaf.x0},${leaf.y0})`}
                    className={`market-tile ${activeSector?.board_name === sector.board_name ? 'selected' : ''}`}
                    role="button"
                    tabIndex={0}
                    aria-label={`${sector.board_name} ${formatPct(sector.change_pct)}`}
                    onMouseMove={(event) => {
                      const bounds = event.currentTarget.ownerSVGElement?.getBoundingClientRect();
                      const x = Math.min(Math.max(event.clientX - (bounds?.left || 0) + 12, 8), Math.max(8, size.width - 244));
                      const y = Math.min(Math.max(event.clientY - (bounds?.top || 0) + 12, 8), Math.max(8, size.height - 126));
                      setTooltip({ sector, x, y });
                    }}
                    onMouseLeave={() => setTooltip(null)}
                    onClick={selectSector}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        selectSector();
                      }
                    }}
                  >
                    <rect
                      width={rectWidth}
                      height={rectHeight}
                      rx={4}
                      fill={sectorColor(sector.change_pct)}
                    />
                    <g>
                      {!tiny && (
                        <text x={8} y={compact ? 17 : 21} className="market-tile-name">
                          {nameText}
                        </text>
                      )}
                      {!compact && rectHeight >= 46 && (
                        <>
                          <text x={8} y={43} className={`market-tile-change ${(sector.change_pct || 0) >= 0 ? 'up' : 'down'}`}>
                            {formatPct(sector.change_pct)}
                          </text>
                          {showMeta && (
                            <text x={8} y={63} className="market-tile-meta">
                              {fitText(`${formatAmount(sector.amount)} · ${sector.constituent_count || '-'}股`, rectWidth, 11)}
                            </text>
                          )}
                        </>
                      )}
                    </g>
                  </g>
                );
              })}
            </svg>
            {tooltip && (
              <div className="market-tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
                <div className="market-tooltip-title">{tooltip.sector.board_name}</div>
                <div>涨跌幅 {formatPct(tooltip.sector.change_pct)}</div>
                <div>成交额 {formatAmount(tooltip.sector.amount)}</div>
                <div>成分股 {tooltip.sector.constituent_count || '-'}</div>
                <div>数据质量 {qualityLabel(tooltip.sector.quality)}</div>
              </div>
            )}
          </div>
          {summary?.notes && summary.notes.length > 0 && (
            <div className="market-notes">{summary.notes.join('；')}</div>
          )}
        </div>

        <aside className="market-detail-panel">
          {activeSector ? (
            <>
              <div className="market-detail-header">
                <div>
                  <h3>{activeSector.board_name}</h3>
                  <div className="market-detail-meta">
                    {activeSector.date || '-'} · 数据质量 {qualityLabel(activeSector.quality)}
                  </div>
                </div>
                <div className={`market-detail-change ${(activeSector.change_pct || 0) >= 0 ? 'up' : 'down'}`}>
                  {formatPct(activeSector.change_pct)}
                </div>
              </div>
      <div className="market-detail-stats">
                <div>
                  <span>成交额</span>
                  <strong>{formatAmount(activeSector.amount)}</strong>
                </div>
                <div>
                  <span>成分股</span>
                  <strong>{activeSector.constituent_count || '-'}</strong>
                </div>
                <div>
                  <span>涨停/强势</span>
                  <strong>{activeSector.limit_up_count || 0} / {(activeSector.strong_stocks || []).length}</strong>
                </div>
                <div>
                  <span>领涨股</span>
                  <strong>{activeSector.leader?.name || '-'}</strong>
                </div>
              </div>
              {activeSector.note && <div className="market-sector-note">{activeSector.note}</div>}
              <div className="market-detail-actions">
                <button
                  type="button"
                  className="market-detail-refresh"
                  disabled={detailRefreshing}
                  onClick={refreshActiveSectorOnline}
                >
                  {detailRefreshing ? '联网补齐中...' : '联网补齐成分股'}
                </button>
              </div>
              {detailLoading && <div className="market-detail-loading">正在读取本地成分股...</div>}
              {renderStockList(
                '涨停成分股',
                activeSector.limit_up || [],
                activeSector.constituents.length ? '当前缓存样本中没有涨停成分股' : '本地暂无该板块成分股缓存'
              )}
              {renderStockList(
                '强势成分股',
                activeSector.strong_stocks || [],
                activeSector.constituents.length ? '当前缓存样本中没有强势成分股' : '本地暂无该板块成分股缓存'
              )}
              {renderStockList(
                '成交额靠前',
                activeSector.top_active,
                '本地暂无该板块成分股缓存；盘后后台或首次联网成功后会补齐'
              )}
              {renderStockList(
                '领涨成分股',
                activeSector.top_gainers,
                '本地暂无该板块成分股缓存；盘后后台或首次联网成功后会补齐'
              )}
              {renderStockList(
                '领跌成分股',
                activeSector.top_losers,
                activeSector.constituents.length ? '当前缓存样本中没有下跌成分股' : '本地暂无该板块成分股缓存'
              )}
            </>
          ) : (
            <div className="market-loading">选择一个板块查看详情</div>
          )}
        </aside>
      </section>
    </main>
  );
}
