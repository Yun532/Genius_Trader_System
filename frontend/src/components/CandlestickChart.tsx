import { useEffect, useRef, useState, useCallback } from 'react';
import * as d3 from 'd3';
import axios from 'axios';

interface OHLCRow {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface ForecastRow {
  date?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number;
}

interface KLineForecast {
  as_of_date: string;
  pred_len: number;
  forecast: ForecastRow[];
}

interface Particle {
  id: string;
  d: string;   // trade_date
  s: string | null;  // sentiment
  r: string | null;  // relevance
  t: string;   // title (truncated)
  rt1: number | null; // ret_t1
  type?: string | null;
}

interface HoverData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  change: number;
}

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
  ohlc?: HoverData;
}

interface Props {
  symbol: string;
  forecast?: KLineForecast | null;
  lockedNewsId?: string | null;
  highlightedArticleIds?: string[] | null;
  highlightColor?: string | null;
  onHover: (date: string | null, ohlc?: HoverData) => void;
  onRangeSelect?: (range: RangeSelection | null) => void;
  onArticleSelect?: (article: ArticleSelection | null) => void;
  onDayClick?: (date: string, ohlc?: HoverData) => void;
}

const A_UP = '#ff3d8b';
const A_DOWN = '#00e5a8';

// Type-first event colors. A-share price colors are red-up/green-down.
const SENTIMENT_COLOR: Record<string, string> = {
  positive: A_UP,
  negative: A_DOWN,
  neutral: '#00e5ff',
};
const EVENT_TYPE_COLOR: Record<string, string> = {
  news: '#38bdf8',
  announcement: '#f59e0b',
  financial_report: '#14b8a6',
  capital: '#a78bfa',
};
const SENTIMENT_COLOR_DEFAULT = '#38bdf8';

function getParticleColor(p: Particle): string {
  return (p.type && EVENT_TYPE_COLOR[p.type]) || (p.s && SENTIMENT_COLOR[p.s]) || SENTIMENT_COLOR_DEFAULT;
}

function getParticleRadius(relevance: string | null, rt1: number | null): number {
  let r = 2;
  if (relevance === 'relevant') r += 0.8;
  if (rt1 !== null) r += Math.min(Math.abs(rt1) * 20, 1.5);
  return Math.min(r, 4.5);
}

function getParticleAlpha(relevance: string | null): number {
  return relevance === 'relevant' ? 0.7 : 0.3;
}

interface PlacedParticle extends Particle {
  px: number; // canvas x
  py: number; // canvas y
  radius: number;
  color: string;
  alpha: number;
}

export default function CandlestickChart({ symbol, forecast, lockedNewsId, highlightedArticleIds, highlightColor, onHover, onRangeSelect, onArticleSelect, onDayClick }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [latestDate, setLatestDate] = useState<string | null>(null);

  // Refs for interaction state (avoid re-renders)
  const placedRef = useRef<PlacedParticle[]>([]);
  const quadtreeRef = useRef<d3.Quadtree<PlacedParticle> | null>(null);
  const hoveredParticleRef = useRef<PlacedParticle | null>(null);
  const lockedNewsIdRef = useRef<string | null>(null);
  const highlightedIdsRef = useRef<Set<string> | null>(null);
  const highlightColorRef = useRef<string | null>(null);
  const marginRef = useRef({ top: 16, right: 40, bottom: 24, left: 48 });
  const dataRef = useRef<{ rawData: OHLCRow[]; particles: Particle[] }>({ rawData: [], particles: [] });
  const forecastRef = useRef<KLineForecast | null>(null);
  const domainRef = useRef<[number, number] | null>(null);
  const resizeFrameRef = useRef<number | null>(null);
  const chartActionsRef = useRef<{
    pan: (direction: -1 | 1) => void;
    latest: () => void;
    reset: () => void;
  } | null>(null);

  // Keep refs in sync with props
  useEffect(() => {
    lockedNewsIdRef.current = lockedNewsId ?? null;
    drawParticles(hoveredParticleRef.current);
  }, [lockedNewsId]);

  useEffect(() => {
    highlightedIdsRef.current = highlightedArticleIds && highlightedArticleIds.length > 0
      ? new Set(highlightedArticleIds)
      : null;
    highlightColorRef.current = highlightColor ?? null;
    drawParticles(hoveredParticleRef.current);
  }, [highlightedArticleIds, highlightColor]);

  useEffect(() => {
    forecastRef.current = forecast ?? null;
    const { rawData, particles } = dataRef.current;
    if (rawData.length === 0) return;
    drawChart(rawData, particles, domainRef.current);
  }, [forecast]);

  const drawParticles = useCallback((highlight: PlacedParticle | null = null) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const locked = lockedNewsIdRef.current;
    const hlSet = highlightedIdsRef.current; // category highlight set
    const hlColor = highlightColorRef.current;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const placed = placedRef.current;
    for (const p of placed) {
      const isLocked = locked != null && p.id === locked;
      const isHover = p === highlight;
      const isCategoryMatch = hlSet != null && hlSet.has(p.id);
      const hasCategoryFilter = hlSet != null;

      // Category filter: hide non-matching particles entirely
      if (hasCategoryFilter && !isCategoryMatch && !isLocked && !isHover) {
        continue;
      }

      let alpha = p.alpha;
      if (isCategoryMatch && hasCategoryFilter) alpha = 1;
      if (isHover || isLocked) alpha = 1;
      ctx.globalAlpha = alpha;

      // Determine radius: category-matched gets a boost
      let radius = p.radius;
      if (isCategoryMatch && hasCategoryFilter) {
        radius = Math.max(p.radius, 3.5);
      }

      // Use category theme color for matched particles, otherwise original
      ctx.fillStyle = (isCategoryMatch && hasCategoryFilter && hlColor) ? hlColor : p.color;

      if (isHover || isLocked || (isCategoryMatch && hasCategoryFilter)) {
        const glowColor = isLocked ? '#00e5ff' : (isCategoryMatch && hlColor) ? hlColor : p.color;
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = (isLocked || isHover ? 14 : 8) * dpr;
      } else {
        ctx.shadowColor = 'transparent';
        ctx.shadowBlur = 0;
      }

      ctx.beginPath();
      ctx.arc(p.px * dpr, p.py * dpr, radius * dpr, 0, Math.PI * 2);
      ctx.fill();

      // Draw cyan ring for locked particle
      if (isLocked) {
        ctx.shadowColor = '#00e5ff';
        ctx.shadowBlur = 10 * dpr;
        ctx.strokeStyle = '#00e5ff';
        ctx.lineWidth = 1.5 * dpr;
        ctx.beginPath();
        ctx.arc(p.px * dpr, p.py * dpr, (radius + 3) * dpr, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Draw ring for category-highlighted particles using category color
      if (isCategoryMatch && hasCategoryFilter && !isLocked) {
        ctx.shadowColor = 'transparent';
        ctx.shadowBlur = 0;
        ctx.strokeStyle = hlColor ? `${hlColor}99` : 'rgba(102, 126, 234, 0.6)';
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath();
        ctx.arc(p.px * dpr, p.py * dpr, (radius + 2) * dpr, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    ctx.globalAlpha = 1;
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;
  }, []);

  useEffect(() => {
    if (!symbol) return;
    setLoading(true);
    setError('');
    setLatestDate(null);
    chartActionsRef.current = null;

    Promise.all([
      axios.get<OHLCRow[]>(`/api/stocks/${symbol}/ohlc`),
      axios.get<Particle[]>(`/api/news/${symbol}/particles`),
    ])
      .then(([ohlcRes, particlesRes]) => {
        setLatestDate(ohlcRes.data.length > 0 ? ohlcRes.data[ohlcRes.data.length - 1].date : null);
        dataRef.current = { rawData: ohlcRes.data, particles: particlesRes.data };
        domainRef.current = null;
        drawChart(ohlcRes.data, particlesRes.data);
        if (ohlcRes.data.length > 0) {
          const start = ohlcRes.data[0].date;
          const end = ohlcRes.data[ohlcRes.data.length - 1].date;
          axios.get(`/api/stocks/${symbol}/daily-reasons?start=${start}&end=${end}&max_generate=20`).catch(() => {});
        }
      })
      .catch((err) => {
        const svg = d3.select(svgRef.current);
        svg.selectAll('*').remove();
        placedRef.current = [];
        drawParticles();
        setError(err.response?.status === 404 ? '暂无可用日 K，请先同步或等待上游恢复。' : '行情加载失败，请稍后重试。');
      })
      .finally(() => setLoading(false));
  }, [symbol]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => {
      if (resizeFrameRef.current !== null) {
        window.cancelAnimationFrame(resizeFrameRef.current);
      }
      resizeFrameRef.current = window.requestAnimationFrame(() => {
        resizeFrameRef.current = null;
        const { rawData, particles } = dataRef.current;
        if (rawData.length === 0) return;
        drawChart(rawData, particles, domainRef.current);
      });
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      if (resizeFrameRef.current !== null) {
        window.cancelAnimationFrame(resizeFrameRef.current);
      }
    };
  }, []);

  function drawChart(rawData: OHLCRow[], particles: Particle[], initialDomain?: [number, number] | null) {
    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const container = containerRef.current;
    if (!container) return;

    const fullWidth = container.clientWidth;
    const fullHeight = container.clientHeight || 600;
    const margin = marginRef.current;
    const width = fullWidth - margin.left - margin.right;
    const height = fullHeight - margin.top - margin.bottom;

    svg.attr('width', fullWidth).attr('height', fullHeight);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    const data = rawData.map((d, i) => ({
      index: i,
      date: new Date(d.date),
      dateStr: d.date,
      open: +d.open,
      high: +d.high,
      low: +d.low,
      close: +d.close,
      volume: +d.volume,
      change: i > 0 ? ((+d.close - +rawData[i - 1].close) / +rawData[i - 1].close) * 100 : 0,
      isForecast: false,
    }));

    const forecastRows = (forecastRef.current?.forecast || [])
      .filter((d) => d.date && d.open != null && d.high != null && d.low != null && d.close != null)
      .map((d, i) => {
        const close = Number(d.close);
        const prevClose = i === 0 ? data[data.length - 1]?.close : Number(forecastRef.current!.forecast[i - 1]?.close);
        return {
          index: data.length + i,
          date: new Date(d.date as string),
          dateStr: d.date as string,
          open: Number(d.open),
          high: Math.max(Number(d.open), Number(d.high), Number(d.low), close),
          low: Math.min(Number(d.open), Number(d.high), Number(d.low), close),
          close,
          volume: Number(d.volume || 0),
          change: prevClose ? ((close - prevClose) / prevClose) * 100 : 0,
          isForecast: true,
        };
      });
    const chartRows = [...data, ...forecastRows];
    const maxIndex = Math.max(0, chartRows.length - 1);

    // Build a lookup: dateStr → OHLC row
    const dateToOhlc = new Map<string, typeof data[0]>();
    for (const d of data) {
      dateToOhlc.set(d.dateStr, d);
    }

    const xBase = d3.scaleLinear()
      .domain([0, maxIndex])
      .range([0, width]);
    let x = xBase.copy();
    if (initialDomain) {
      x.domain([
        Math.max(0, Math.min(initialDomain[0], maxIndex)),
        Math.max(0, Math.min(initialDomain[1], maxIndex)),
      ]);
    }

    const y = d3.scaleLinear()
      .domain([d3.min(chartRows, (d) => d.low)! * 0.92, d3.max(chartRows, (d) => d.high)! * 1.03])
      .range([height, 0]);

    // Grid lines
    const gridYG = g.append<SVGGElement>('g')
      .attr('class', 'grid-y');
    gridYG.call(
      d3.axisLeft(y)
        .ticks(8)
        .tickSize(-width)
        .tickFormat(() => '')
    );
    gridYG.selectAll('line').style('stroke', '#1a1e2e').style('stroke-width', 1);
    gridYG.selectAll('.domain').remove();

    // X Axis
    const tickStep = Math.max(1, Math.ceil(chartRows.length / 8));
    const xTicks = chartRows.filter((_, i) => i % tickStep === 0).map((d) => d.index);
    const xAxisG = g.append('g')
      .attr('transform', `translate(0,${height})`)
      .call(d3.axisBottom(x).tickValues(xTicks).tickFormat((value) => chartRows[Math.round(Number(value))]?.dateStr.slice(5) || ''))
    xAxisG.selectAll('text').style('font-size', '12px').style('fill', '#555');

    // Y Axis
    const yAxisG = g.append<SVGGElement>('g')
      .call(d3.axisLeft(y).ticks(6).tickFormat((d) => `¥${Number(d).toFixed(0)}`))
    yAxisG.selectAll('text').style('font-size', '12px').style('fill', '#555');

    g.selectAll('.domain').style('stroke', '#1a2030');
    g.selectAll('.tick line').style('stroke', '#1a2030');

    let candleWidth = Math.max(1.5, (width / data.length) * 0.65);

    // Candlesticks
    const candles = g.selectAll('.candle').data(data).enter().append('g').attr('class', 'candle');

    // Wicks
    candles.append('line')
      .attr('x1', (d) => x(d.index))
      .attr('x2', (d) => x(d.index))
      .attr('y1', (d) => y(d.high))
      .attr('y2', (d) => y(d.low))
      .attr('stroke', (d) => (d.close >= d.open ? A_UP : A_DOWN))
      .attr('stroke-width', 1);

    // Bodies
    candles.append('rect')
      .attr('x', (d) => x(d.index) - candleWidth / 2)
      .attr('y', (d) => y(Math.max(d.open, d.close)))
      .attr('width', candleWidth)
      .attr('height', (d) => Math.max(1, Math.abs(y(d.open) - y(d.close))))
      .attr('fill', (d) => (d.close >= d.open ? A_UP : A_DOWN));

    const forecastLayer = g.append('g').attr('class', 'forecast-layer');
    if (forecastRows.length > 0) {
      const splitX = x(data.length - 0.5);
      forecastLayer.append('line')
        .attr('class', 'forecast-separator')
        .attr('x1', splitX)
        .attr('x2', splitX)
        .attr('y1', 0)
        .attr('y2', height)
        .attr('stroke', '#fbbf24')
        .attr('stroke-width', 1)
        .attr('stroke-dasharray', '4,4')
        .attr('opacity', 0.85);
      forecastLayer.append('text')
        .attr('class', 'forecast-label')
        .attr('x', splitX + 8)
        .attr('y', 14)
        .attr('fill', '#fbbf24')
        .attr('font-size', 12)
        .attr('font-weight', 700)
        .text(`Kronos T+${forecastRows.length}`);
    }

    const forecastCandles = forecastLayer.selectAll('.forecast-candle')
      .data(forecastRows)
      .enter()
      .append('g')
      .attr('class', 'forecast-candle');

    forecastCandles.append('line')
      .attr('x1', (d) => x(d.index))
      .attr('x2', (d) => x(d.index))
      .attr('y1', (d) => y(d.high))
      .attr('y2', (d) => y(d.low))
      .attr('stroke', (d) => (d.close >= d.open ? A_UP : A_DOWN))
      .attr('stroke-width', 1.2)
      .attr('stroke-dasharray', '2,2')
      .attr('opacity', 0.8);

    forecastCandles.append('rect')
      .attr('x', (d) => x(d.index) - candleWidth / 2)
      .attr('y', (d) => y(Math.max(d.open, d.close)))
      .attr('width', candleWidth)
      .attr('height', (d) => Math.max(1, Math.abs(y(d.open) - y(d.close))))
      .attr('fill', (d) => (d.close >= d.open ? A_UP : A_DOWN))
      .attr('fill-opacity', 0.28)
      .attr('stroke', (d) => (d.close >= d.open ? A_UP : A_DOWN))
      .attr('stroke-width', 1)
      .attr('stroke-dasharray', '3,2');

    // --- Place particles overlaid on K-line ---
    // Group particles by trade_date
    const particlesByDate = new Map<string, Particle[]>();
    for (const p of particles) {
      const arr = particlesByDate.get(p.d) || [];
      arr.push(p);
      particlesByDate.set(p.d, arr);
    }

    // Particle vertical spacing in pixels
    const pSpacing = Math.max(4.5, Math.min(7, height / 80));

    function rebuildParticles() {
      const placed: PlacedParticle[] = [];
      const [domainStart, domainEnd] = x.domain();
      for (const [dateStr, pArr] of particlesByDate) {
        const ohlc = dateToOhlc.get(dateStr);
        if (!ohlc || ohlc.index < domainStart - 2 || ohlc.index > domainEnd + 2) continue;

        const cx = x(ohlc.index);
        pArr.sort((a, b) => {
          const ra = a.r === 'relevant' ? 0 : 1;
          const rb = b.r === 'relevant' ? 0 : 1;
          if (ra !== rb) return ra - rb;
          return Math.abs(b.rt1 || 0) - Math.abs(a.rt1 || 0);
        });

        for (let i = 0; i < pArr.length; i++) {
          const p = pArr[i];
          const radius = getParticleRadius(p.r, p.rt1);
          const candleLowY = y(ohlc.low);
          const py = margin.top + candleLowY + 6 + i * pSpacing;
          if (py > margin.top + height + 10) break;
          placed.push({
            ...p,
            px: margin.left + cx,
            py,
            radius,
            color: getParticleColor(p),
            alpha: getParticleAlpha(p.r),
          });
        }
      }
      placedRef.current = placed;
      quadtreeRef.current = d3.quadtree<PlacedParticle>()
        .x((d) => d.px)
        .y((d) => d.py)
        .addAll(placed);
    }

    // --- Setup Canvas ---
    const canvas = canvasRef.current;
    if (canvas) {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = fullWidth * dpr;
      canvas.height = fullHeight * dpr;
      canvas.style.width = `${fullWidth}px`;
      canvas.style.height = `${fullHeight}px`;
    }

    // --- Crosshair elements ---
    const crossV = g.append('line')
      .style('stroke', '#333')
      .style('stroke-width', 0.5)
      .style('stroke-dasharray', '4,3')
      .style('display', 'none')
      .style('pointer-events', 'none');

    const crossH = g.append('line')
      .style('stroke', '#333')
      .style('stroke-width', 0.5)
      .style('stroke-dasharray', '4,3')
      .style('display', 'none')
      .style('pointer-events', 'none');

    // Price label on Y axis
    const priceLabel = g.append('g').style('display', 'none');
    priceLabel.append('rect')
      .attr('fill', '#1a1e2e')
      .attr('rx', 3)
      .attr('width', 46)
      .attr('height', 18);
    priceLabel.append('text')
      .attr('fill', '#aaa')
      .attr('font-size', '12px')
      .attr('text-anchor', 'middle')
      .attr('dy', '13px');

    // Date label on X axis
    const dateLabel = g.append('g').style('display', 'none');
    dateLabel.append('rect')
      .attr('fill', '#1a1e2e')
      .attr('rx', 3)
      .attr('width', 75)
      .attr('height', 20);
    dateLabel.append('text')
      .attr('fill', '#aaa')
      .attr('font-size', '13px')
      .attr('text-anchor', 'middle')
      .attr('dy', '14px');

    function snapToData(px: number) {
      const idx = Math.max(0, Math.min(maxIndex, Math.round(x.invert(px))));
      return chartRows[idx];
    }

    function renderChart() {
      const [d0, d1] = x.domain();
      const visibleCount = Math.max(1, d1 - d0 + 1);
      const visibleStart = Math.max(0, Math.floor(d0));
      const visibleEnd = Math.min(maxIndex, Math.ceil(d1));
      const visibleRows = chartRows.slice(visibleStart, visibleEnd + 1);
      const visibleLow = d3.min(visibleRows, (d) => d.low) ?? d3.min(chartRows, (d) => d.low)!;
      const visibleHigh = d3.max(visibleRows, (d) => d.high) ?? d3.max(chartRows, (d) => d.high)!;
      const pad = Math.max((visibleHigh - visibleLow) * 0.12, visibleHigh * 0.01);
      y.domain([visibleLow - pad, visibleHigh + pad]);

      candleWidth = Math.max(2, Math.min(18, (width / visibleCount) * 0.65));
      const visibleTickStep = Math.max(1, Math.ceil(visibleCount / 8));
      const tickStart = Math.max(0, Math.ceil(d0));
      const tickEnd = Math.min(maxIndex, Math.floor(d1));
      const visibleTicks = chartRows
        .slice(tickStart, tickEnd + 1)
        .filter((_, i) => i % visibleTickStep === 0)
        .map((d) => d.index);
      if (tickEnd >= 0 && !visibleTicks.includes(tickEnd)) {
        visibleTicks.push(tickEnd);
      }

      xAxisG.call(d3.axisBottom(x).tickValues(visibleTicks).tickFormat((value) => chartRows[Math.round(Number(value))]?.dateStr.slice(5) || ''));
      xAxisG.selectAll('text').style('font-size', '12px').style('fill', '#555');
      xAxisG.selectAll('.domain').style('stroke', '#1a2030');
      xAxisG.selectAll('.tick line').style('stroke', '#1a2030');

      gridYG.call(
        d3.axisLeft(y)
          .ticks(8)
          .tickSize(-width)
          .tickFormat(() => '')
      );
      gridYG.selectAll('line').style('stroke', '#1a1e2e').style('stroke-width', 1);
      gridYG.selectAll('.domain').remove();

      yAxisG.call(d3.axisLeft(y).ticks(6).tickFormat((d) => `¥${Number(d).toFixed(0)}`));
      yAxisG.selectAll('text').style('font-size', '12px').style('fill', '#555');
      yAxisG.selectAll('.domain').style('stroke', '#1a2030');
      yAxisG.selectAll('.tick line').style('stroke', '#1a2030');

      candles
        .style('display', (d) => (d.index < d0 - 1 || d.index > d1 + 1 ? 'none' : null));
      candles.selectAll('line')
        .attr('x1', (d: any) => x(d.index))
        .attr('x2', (d: any) => x(d.index))
        .attr('y1', (d: any) => y(d.high))
        .attr('y2', (d: any) => y(d.low));
      candles.selectAll('rect')
        .attr('x', (d: any) => x(d.index) - candleWidth / 2)
        .attr('width', candleWidth)
        .attr('y', (d: any) => y(Math.max(d.open, d.close)))
        .attr('height', (d: any) => Math.max(1, Math.abs(y(d.open) - y(d.close))));
      forecastLayer.selectAll<SVGLineElement, any>('.forecast-separator')
        .attr('x1', x(data.length - 0.5))
        .attr('x2', x(data.length - 0.5))
        .attr('y2', height);
      forecastLayer.selectAll<SVGTextElement, any>('.forecast-label')
        .attr('x', x(data.length - 0.5) + 8);
      forecastCandles
        .style('display', (d) => (d.index < d0 - 1 || d.index > d1 + 1 ? 'none' : null));
      forecastCandles.selectAll('line')
        .attr('x1', (d: any) => x(d.index))
        .attr('x2', (d: any) => x(d.index))
        .attr('y1', (d: any) => y(d.high))
        .attr('y2', (d: any) => y(d.low));
      forecastCandles.selectAll('rect')
        .attr('x', (d: any) => x(d.index) - candleWidth / 2)
        .attr('width', candleWidth)
        .attr('y', (d: any) => y(Math.max(d.open, d.close)))
        .attr('height', (d: any) => Math.max(1, Math.abs(y(d.open) - y(d.close))));
      rebuildParticles();
      drawParticles(hoveredParticleRef.current);
    }

    function setVisibleDomain(nextStart: number, nextEnd: number) {
      const minSpan = 12;
      const maxStart = Math.max(0, maxIndex - minSpan);
      let start = Math.max(0, Math.min(nextStart, maxIndex));
      let end = Math.max(0, Math.min(nextEnd, maxIndex));
      if (end - start < minSpan) {
        const center = (start + end) / 2;
        start = center - minSpan / 2;
        end = center + minSpan / 2;
      }
      if (start < 0) {
        end -= start;
        start = 0;
      }
      if (end > maxIndex) {
        start -= end - maxIndex;
        end = maxIndex;
      }
      start = Math.max(0, Math.min(start, maxStart));
      x.domain([start, end]);
      domainRef.current = [start, end];
      renderChart();
    }

    function panBy(direction: -1 | 1) {
      const [d0, d1] = x.domain();
      const span = d1 - d0;
      const step = direction * Math.max(3, span * 0.35);
      setVisibleDomain(d0 + step, d1 + step);
    }

    function jumpToLatest() {
      const [d0, d1] = x.domain();
      const span = d1 - d0;
      setVisibleDomain(maxIndex - span, maxIndex);
    }

    chartActionsRef.current = {
      pan: panBy,
      latest: jumpToLatest,
      reset: () => {
        domainRef.current = null;
        setVisibleDomain(0, maxIndex);
      },
    };

    // --- Particle hit testing ---
    function findParticle(mouseX: number, mouseY: number): PlacedParticle | null {
      const qt = quadtreeRef.current;
      if (!qt) return null;
      const searchRadius = 8;
      let closest: PlacedParticle | null = null;
      let closestDist = searchRadius;
      const hlSet = highlightedIdsRef.current;
      const locked = lockedNewsIdRef.current;

      qt.visit((node, x0, y0, x1, y1) => {
        if (!('data' in node)) {
          return x0 > mouseX + searchRadius || x1 < mouseX - searchRadius ||
                 y0 > mouseY + searchRadius || y1 < mouseY - searchRadius;
        }
        let leaf: typeof node | undefined = node;
        while (leaf) {
          const p = leaf.data;
          // Skip particles hidden by category filter
          if (hlSet != null && !hlSet.has(p.id) && p.id !== locked) {
            leaf = (leaf as any).next;
            continue;
          }
          const dx = p.px - mouseX;
          const dy = p.py - mouseY;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < closestDist) {
            closestDist = dist;
            closest = p;
          }
          leaf = (leaf as any).next;
        }
        return false;
      });

      return closest;
    }

    // D3 Brush for range selection
    let brushMoving = false;
    const brush = d3.brushX<unknown>()
      .extent([[0, 0], [width, height + margin.bottom]])
      .on('end', function (event) {
        if (brushMoving) return; // guard against re-entrancy from brush.move
        if (!event.selection) {
          // Click (not drag) — find similar days or toggle lock
          if (event.sourceEvent) {
            const [mx] = d3.pointer(event.sourceEvent, g.node());
            const d = snapToData(mx);
            const [absX, absY] = d3.pointer(event.sourceEvent, container);
            const hit = findParticle(absX, absY);
            if (hit) {
              const hitOhlc = dateToOhlc.get(hit.d);
              onArticleSelect?.({
                newsId: hit.id,
                date: hit.d,
                ohlc: hitOhlc
                  ? {
                      date: hitOhlc.dateStr,
                      open: hitOhlc.open,
                      high: hitOhlc.high,
                      low: hitOhlc.low,
                      close: hitOhlc.close,
                      change: hitOhlc.change,
                    }
                  : undefined,
              });
            } else {
              if (d.isForecast) {
                onArticleSelect?.(null);
                return;
              }
              // Click on background: unlock any locked article, then show similar days
              onArticleSelect?.(null);
              onDayClick?.(d.dateStr, {
                date: d.dateStr,
                open: d.open,
                high: d.high,
                low: d.low,
                close: d.close,
                change: d.change,
              });
            }
          }
          return;
        }
        const [x0, x1] = event.selection as [number, number];
        const d0 = snapToData(x0);
        const d1 = snapToData(x1);
        if (d0.isForecast || d1.isForecast) {
          brushMoving = true;
          d3.select(this).call(brush.move, null);
          brushMoving = false;
          return;
        }
        if (d0.dateStr === d1.dateStr) {
          brushMoving = true;
          d3.select(this).call(brush.move, null);
          brushMoving = false;
          return;
        }
        brushMoving = true;
        d3.select(this).call(brush.move, [x(d0.index), x(d1.index)]);
        brushMoving = false;
        const priceChange = ((d1.close - d0.open) / d0.open) * 100;
        // Position popup near the right edge of the selection, within the chart container
        const popupX = margin.left + x(d1.index) + 8;
        const popupY = margin.top + Math.min(y(d0.close), y(d1.close)) - 20;
        onRangeSelect?.({ startDate: d0.dateStr, endDate: d1.dateStr, priceChange, popupX, popupY });
      });

    const brushG = g.append('g')
      .attr('class', 'brush')
      .call(brush);

    brushG.selectAll('.selection')
      .attr('fill', '#667eea')
      .attr('fill-opacity', 0.15)
      .attr('stroke', '#667eea')
      .attr('stroke-width', 1);

    svg
      .on('wheel.chartZoom', function (event) {
        event.preventDefault();
        const [mx] = d3.pointer(event, g.node());
        const [d0, d1] = x.domain();
        const span = d1 - d0;
        if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
          const rawDelta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
          const step = (rawDelta > 0 ? 1 : -1) * Math.max(1, span * 0.08);
          setVisibleDomain(d0 + step, d1 + step);
          return;
        }
        const center = x.invert(mx);
        const zoomFactor = event.deltaY > 0 ? 1.18 : 0.82;
        const nextSpan = Math.max(12, Math.min(maxIndex, span * zoomFactor));
        const ratio = span > 0 ? (center - d0) / span : 0.5;
        setVisibleDomain(center - nextSpan * ratio, center + nextSpan * (1 - ratio));
      })
      .on('dblclick.chartZoom', function () {
        chartActionsRef.current?.reset();
      });

    renderChart();

    // Hover events on the brush overlay
    brushG.select('.overlay')
      .style('cursor', 'crosshair')
      .on('mousemove.hover', function (event) {
        const [mx, my] = d3.pointer(event);
        const d = snapToData(mx);
        const cx = x(d.index);
        const priceAtY = y.invert(my);

        // Vertical crosshair
        crossV.attr('x1', cx).attr('x2', cx).attr('y1', 0).attr('y2', height).style('display', null);
        // Horizontal crosshair
        crossH.attr('x1', 0).attr('x2', width).attr('y1', my).attr('y2', my).style('display', null);

        // Price label
        priceLabel.style('display', null)
          .attr('transform', `translate(${-46},${my - 9})`);
        priceLabel.select('text')
          .attr('x', 23)
          .text(`¥${priceAtY.toFixed(2)}`);

        // Date label
        dateLabel.style('display', null)
          .attr('transform', `translate(${cx - 37.5},${height})`);
        dateLabel.select('text')
          .attr('x', 37.5)
          .text(d.dateStr);

        // Emit hover for OHLC
        onHover(d.dateStr, {
          date: d.dateStr,
          open: d.open,
          high: d.high,
          low: d.low,
          close: d.close,
          change: d.change,
        });

        // Check particle hover
        const [absX, absY] = d3.pointer(event, container);
        const hit = findParticle(absX, absY);

        if (hit !== hoveredParticleRef.current) {
          hoveredParticleRef.current = hit;
          drawParticles(hit);

          const tooltip = tooltipRef.current;
          if (tooltip) {
            if (hit) {
              const retStr = hit.rt1 !== null ? `${(hit.rt1 * 100).toFixed(2)}%` : '-';
              const retColor = hit.rt1 !== null ? (hit.rt1 >= 0 ? A_UP : A_DOWN) : '#555';
              tooltip.innerHTML = `
                <div class="pt-title">${hit.t}</div>
                <div class="pt-meta">
                  <span class="pt-sentiment" style="color:${hit.color}">${hit.s || 'unknown'}</span>
                  <span class="pt-ret" style="color:${retColor}">T+1: ${retStr}</span>
                </div>
              `;
              tooltip.style.display = 'block';
              const tipW = 280; // max-width of tooltip
              const onRight = hit.px < fullWidth / 2;
              const tipX = onRight ? hit.px + 12 : hit.px - tipW - 12;
              const tipY = hit.py - 40;
              tooltip.style.left = `${Math.max(4, tipX)}px`;
              tooltip.style.top = `${Math.max(4, tipY)}px`;
            } else {
              tooltip.style.display = 'none';
            }
          }
        }
      })
      .on('mouseleave.hover', function () {
        crossV.style('display', 'none');
        crossH.style('display', 'none');
        priceLabel.style('display', 'none');
        dateLabel.style('display', 'none');
        onHover(null);

        if (hoveredParticleRef.current) {
          hoveredParticleRef.current = null;
          drawParticles();
        }
        const tooltip = tooltipRef.current;
        if (tooltip) tooltip.style.display = 'none';
      });
  }

  return (
    <div ref={containerRef} className="chart-container">
      {loading && <div className="chart-loading">Loading...</div>}
      {!loading && error && <div className="chart-loading">{error}</div>}
      {!loading && latestDate && (
        <div className="chart-meta-bar">
          <span>最新 K 线 {latestDate}</span>
          <button type="button" onClick={() => chartActionsRef.current?.pan(-1)} title="向左移动可见区间">‹</button>
          <button type="button" onClick={() => chartActionsRef.current?.pan(1)} title="向右移动可见区间">›</button>
          <button type="button" onClick={() => chartActionsRef.current?.latest()} title="跳到最新交易日">最新</button>
          <button type="button" onClick={() => chartActionsRef.current?.reset()} title="显示全部 K 线">重置</button>
        </div>
      )}
      <svg ref={svgRef}></svg>
      <canvas
        ref={canvasRef}
        className="particle-layer"
      />
      <div ref={tooltipRef} className="particle-tooltip" style={{ display: 'none' }} />
    </div>
  );
}
