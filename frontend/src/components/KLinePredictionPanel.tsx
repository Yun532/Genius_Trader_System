import { useEffect, useState } from 'react';
import axios from 'axios';

export interface KronosForecastRow {
  date?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number;
}

export interface KronosReference {
  symbol: string;
  model: string;
  as_of_date: string;
  lookback: number;
  pred_len: number;
  last_close: number;
  predicted_close: number;
  predicted_return: number;
  path_up_ratio: number;
  verdict: 'positive' | 'negative' | 'neutral';
  forecast: KronosForecastRow[];
  warning: string;
  notes?: string[];
}

interface Props {
  symbol: string;
  displayName: string;
  predLen: number;
  lookback: number;
  onPredLenChange: (value: number) => void;
  onLookbackChange: (value: number) => void;
  onForecastLoaded: (data: KronosReference | null) => void;
  onClose: () => void;
}

function pct(value: number) {
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`;
}

function verdictLabel(verdict: KronosReference['verdict']) {
  if (verdict === 'positive') return '偏正';
  if (verdict === 'negative') return '偏负';
  return '中性';
}

export default function KLinePredictionPanel({
  symbol,
  displayName,
  predLen,
  lookback,
  onPredLenChange,
  onLookbackChange,
  onForecastLoaded,
  onClose,
}: Props) {
  const [data, setData] = useState<KronosReference | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!symbol) return;
    setLoading(true);
    setError('');
    setData(null);
    onForecastLoaded(null);
    axios
      .get<KronosReference>(`/api/predict/${symbol}/kronos-reference`, {
        params: { lookback, pred_len: predLen, sample_count: 1 },
      })
      .then((res) => {
        setData(res.data);
        onForecastLoaded(res.data);
      })
      .catch((err) => {
        const detail = err.response?.data?.detail;
        setError(typeof detail === 'string' ? detail : 'Kronos 日线预测暂不可用');
        onForecastLoaded(null);
      })
      .finally(() => setLoading(false));
  }, [symbol, lookback, predLen, onForecastLoaded]);

  const ret = data?.predicted_return ?? 0;
  const retClass = ret >= 0 ? 'up' : 'down';

  return (
    <div className="news-panel kline-pred-panel">
      <div className="news-panel-header kline-pred-header">
        <div>
          <h2>日线预测参考</h2>
          <div className="kline-pred-subtitle">{displayName || symbol}</div>
        </div>
        <button className="panel-close-btn" type="button" onClick={onClose} aria-label="关闭">
          ×
        </button>
      </div>

      <div className="kline-pred-controls">
        <label>
          周期
          <select value={predLen} onChange={(event) => onPredLenChange(Number(event.target.value))}>
            <option value={3}>T+3</option>
            <option value={5}>T+5</option>
            <option value={10}>T+10</option>
            <option value={20}>T+20</option>
          </select>
        </label>
        <label>
          回看
          <select value={lookback} onChange={(event) => onLookbackChange(Number(event.target.value))}>
            <option value={60}>60 日</option>
            <option value={120}>120 日</option>
            <option value={256}>256 日</option>
            <option value={512}>512 日</option>
          </select>
        </label>
      </div>

      {loading && (
        <div className="kline-pred-state">
          <span className="pred-loading-dot" />
          Kronos 正在生成日线参考...
        </div>
      )}

      {!loading && error && (
        <div className="kline-pred-unavailable">
          <div className="kline-pred-unavailable-title">Kronos 尚未配置</div>
          <div className="kline-pred-unavailable-text">{error}</div>
          <div className="kline-pred-unavailable-text">
            需要安装 Kronos/PyTorch，并在 `.env` 中配置 `KRONOS_REPO_PATH`、模型名和设备。当前页面已接好接口，配置完成后会自动显示预测结果。
          </div>
        </div>
      )}

      {!loading && data && (
        <>
          <div className={`kline-pred-summary ${retClass}`}>
            <div>
              <div className="kline-pred-summary-label">Kronos {data.pred_len} 日参考</div>
              <div className="kline-pred-summary-value">{verdictLabel(data.verdict)}</div>
            </div>
            <div className={`kline-pred-return ${retClass}`}>{pct(data.predicted_return)}</div>
          </div>

          <div className="kline-pred-metrics">
            <div>
              <span>最新收盘</span>
              <strong>{data.last_close.toFixed(2)}</strong>
            </div>
            <div>
              <span>预测收盘</span>
              <strong>{data.predicted_close.toFixed(2)}</strong>
            </div>
            <div>
              <span>路径上行占比</span>
              <strong>{(data.path_up_ratio * 100).toFixed(0)}%</strong>
            </div>
            <div>
              <span>截至日期</span>
              <strong>{data.as_of_date}</strong>
            </div>
          </div>

          <div className="kline-pred-path">
            <div className="kline-pred-section-title">预测路径</div>
            {data.forecast.slice(0, 10).map((row, index) => {
              const rowRet = row.close != null ? row.close / data.last_close - 1 : 0;
              return (
                <div key={`${row.date}-${index}`} className="kline-pred-row">
                  <span>{row.date || `T+${index + 1}`}</span>
                  <strong className={rowRet >= 0 ? 'up' : 'down'}>
                    {row.close != null ? row.close.toFixed(2) : '--'}
                  </strong>
                  <em className={rowRet >= 0 ? 'up' : 'down'}>{row.close != null ? pct(rowRet) : '--'}</em>
                </div>
              );
            })}
          </div>

          <div className="kline-pred-warning">{data.warning}</div>
          {data.notes?.map((note) => (
            <div className="kline-pred-note" key={note}>{note}</div>
          ))}
        </>
      )}
    </div>
  );
}
