import { useEffect, useMemo, useState } from 'react';
import axios from 'axios';

interface MacroSource {
  id?: string;
  date?: string;
  type?: string;
  title: string;
  summary?: string | null;
  source?: string | null;
  url?: string | null;
}

interface SectorPerformance {
  available: boolean;
  board_name?: string;
  date?: string;
  change_pct?: number | null;
  excess_return_pct?: number | null;
  amount?: number | null;
  error?: string | null;
}

interface SectorCompany {
  symbol: string;
  code?: string | null;
  name?: string | null;
  display_name?: string | null;
  board_name?: string | null;
  date?: string | null;
  close?: number | null;
  change_pct?: number | null;
  return_5d_pct?: number | null;
  return_20d_pct?: number | null;
  amount?: number | null;
  turnover_rate?: number | null;
  source?: string | null;
}

interface CompanyGroup {
  relation_type: string;
  relation_label?: string;
  board_name?: string | null;
  reason?: string | null;
  performance?: SectorPerformance;
  companies?: SectorCompany[];
  companies_count?: number;
  quality?: string;
  note?: string | null;
}

interface CompanyBucket {
  board_name?: string | null;
  date?: string;
  items?: SectorCompany[];
  count?: number;
  quality?: string;
  note?: string;
}

interface MacroChainData {
  symbol: string;
  display_name?: string | null;
  date: string;
  available: boolean;
  cached?: boolean;
  generated_at?: string | null;
  expires_at?: string | null;
  sources_count?: number;
  summary?: string | null;
  policy_summary?: string[];
  global_summary?: string[];
  supply_chain?: {
    upstream?: string[];
    current_position?: string;
    downstream?: string[];
    complementary?: string[];
    substitute?: string[];
  };
  sector_relations?: {
    industry_name?: string | null;
    board_name?: string | null;
    base_performance?: SectorPerformance;
    base_companies?: CompanyBucket;
    relations?: Array<{
      related_board_name: string;
      relation_type: string;
      reason?: string | null;
      performance?: SectorPerformance;
    }>;
    related_groups?: CompanyGroup[];
    leaders?: CompanyBucket;
  };
  transmission_paths?: string[];
  watch_points?: string[];
  risks?: string[];
  sources?: MacroSource[];
  evidence_quality?: string;
  llm_used?: boolean;
  llm_error?: string | null;
  warnings?: string[];
}

interface Props {
  symbol: string;
  displayName?: string;
  date: string;
  onClose: () => void;
}

const EVENT_LABELS: Record<string, string> = {
  policy: '政策',
  global_macro: '国际',
  sector: '板块',
  supply_chain: '产业链',
  capital: '资金',
  news: '资讯',
};

const RELATION_LABELS: Record<string, string> = {
  same_sector: '同板块',
  upstream: '上游',
  downstream: '下游',
  complementary: '互补',
  substitute: '替代',
  competitive: '竞争',
};

const TABS = [
  { key: 'overview', label: '总览' },
  { key: 'companies', label: '产业链公司' },
  { key: 'sectors', label: '板块对比' },
  { key: 'sources', label: '证据来源' },
] as const;

type TabKey = typeof TABS[number]['key'];

function signedPct(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function pctClass(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return '';
  return value >= 0 ? 'up' : 'down';
}

function formatAmount(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  if (Math.abs(value) >= 100000000) return `${(value / 100000000).toFixed(2)} 亿`;
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return value.toFixed(0);
}

function shortText(value?: string | null, max = 150) {
  if (!value) return '';
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

function BulletList({ title, items }: { title: string; items?: string[] }) {
  const list = (items || []).filter(Boolean);
  return (
    <div className="macro-compact-section">
      <div className="range-section-title">{title}</div>
      {list.length > 0 ? (
        <ul className="macro-bullets">
          {list.slice(0, 5).map((item, index) => <li key={index}>{item}</li>)}
        </ul>
      ) : (
        <p className="daily-muted">暂无直接证据。</p>
      )}
    </div>
  );
}

function CompanyTable({ companies }: { companies?: SectorCompany[] }) {
  const rows = companies || [];
  if (!rows.length) {
    return <p className="daily-muted">暂无可靠成分股数据。</p>;
  }
  return (
    <div className="macro-company-table-wrap">
      <table className="macro-company-table">
        <thead>
          <tr>
            <th>公司</th>
            <th>当日</th>
            <th>近 5 日</th>
            <th>近 20 日</th>
            <th>成交额</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr key={item.symbol}>
              <td>
                <strong>{item.name || item.display_name || item.symbol}</strong>
                <span>{item.code || item.symbol}</span>
              </td>
              <td className={pctClass(item.change_pct)}>{signedPct(item.change_pct)}</td>
              <td className={pctClass(item.return_5d_pct)}>{signedPct(item.return_5d_pct)}</td>
              <td className={pctClass(item.return_20d_pct)}>{signedPct(item.return_20d_pct)}</td>
              <td>{formatAmount(item.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MacroChainPanel({ symbol, displayName, date, onClose }: Props) {
  const [data, setData] = useState<MacroChainData | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [hydrating, setHydrating] = useState(false);
  const [hydrateMessage, setHydrateMessage] = useState('');
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState<TabKey>('overview');
  const [expandedSummary, setExpandedSummary] = useState(false);
  const [showAllSources, setShowAllSources] = useState(false);

  function load() {
    setLoading(true);
    setError('');
    axios
      .get<MacroChainData>(`/api/stocks/${symbol}/macro-chain`, { params: { date } })
      .then((res) => setData(res.data))
      .catch((err) => setError(err.response?.data?.detail || '联动研究暂时不可用'))
      .finally(() => setLoading(false));
  }

  function refresh(force = false) {
    setRefreshing(true);
    setError('');
    axios
      .post<MacroChainData>(`/api/stocks/${symbol}/macro-chain/refresh`, null, {
        params: { date, refresh_cache: force },
      })
      .then((res) => setData(res.data))
      .catch((err) => setError(err.response?.data?.detail || '生成联动研究失败'))
      .finally(() => setRefreshing(false));
  }

  function hydratePeers() {
    setHydrating(true);
    setHydrateMessage('');
    setError('');
    axios
      .post(`/api/stocks/${symbol}/sector-relations/hydrate`, null, {
        params: { date, max_companies: 12 },
      })
      .then((res) => {
        const nextRelations = res.data?.sector_relations;
        if (nextRelations) {
          setData((prev) => prev ? { ...prev, sector_relations: nextRelations } : prev);
        }
        setHydrateMessage(res.data?.message || '同行行情补全完成。');
      })
      .catch((err) => setError(err.response?.data?.detail || '补全同行行情失败'))
      .finally(() => setHydrating(false));
  }

  useEffect(() => {
    setActiveTab('overview');
    setExpandedSummary(false);
    setShowAllSources(false);
    load();
  }, [symbol, date]);

  const title = data?.display_name || displayName || symbol;
  const base = data?.sector_relations?.base_performance;
  const sourceList = data?.sources || [];
  const visibleSources = showAllSources ? sourceList : sourceList.slice(0, 5);
  const baseCompanies = data?.sector_relations?.base_companies;
  const companyGroups = useMemo<CompanyGroup[]>(() => {
    const groups: CompanyGroup[] = [];
    if (baseCompanies) {
      groups.push({
        relation_type: 'same_sector',
        board_name: data?.sector_relations?.board_name || baseCompanies.board_name,
        performance: base,
        companies: baseCompanies.items || [],
        companies_count: baseCompanies.count,
        quality: baseCompanies.quality,
        note: baseCompanies.note,
      });
    }
    (data?.sector_relations?.related_groups || []).forEach((group) => groups.push(group));
    return groups;
  }, [data, baseCompanies, base]);

  return (
    <div className="news-panel stock-report-panel macro-chain-panel">
      <div className="macro-sticky-head">
        <div className="news-panel-header">
          <h2>联动研究</h2>
          <span className="news-date-badge">{date}</span>
          <button className="range-clear-btn" onClick={onClose}>关闭</button>
        </div>

        {data && (
          <div className="macro-head-grid">
            <div>
              <span>股票</span>
              <strong>{title}</strong>
            </div>
            <div>
              <span>所属板块</span>
              <strong>{data.sector_relations?.board_name || data.sector_relations?.industry_name || '未知'}</strong>
            </div>
            <div>
              <span>板块涨跌</span>
              <strong className={pctClass(base?.change_pct)}>{signedPct(base?.change_pct)}</strong>
            </div>
            <div>
              <span>来源</span>
              <strong>{data.sources_count || 0} 条</strong>
            </div>
          </div>
        )}
      </div>

      {loading && !data ? (
        <div className="news-empty">正在读取宏观与产业链缓存...</div>
      ) : error && !data ? (
        <div className="news-empty">{error}</div>
      ) : data ? (
        <div className="macro-chain-body">
          <div className="macro-toolbar">
            <div className="macro-cache-meta">
              {data.available ? (data.cached ? '来自缓存' : '本次生成') : '暂无缓存'}
              {data.generated_at ? ` · ${data.generated_at.slice(0, 16).replace('T', ' ')}` : ''}
              {data.llm_used ? ' · LLM 已整理' : ' · 本地规则'}
              <span className={`daily-quality ${data.evidence_quality || 'low'}`}>{data.evidence_quality || 'low'}</span>
            </div>
            <button
              type="button"
              className="range-news-ai-btn stock-report-refresh"
              onClick={() => refresh(Boolean(data.available))}
              disabled={refreshing}
            >
              {refreshing ? '生成中...' : data.available ? '重新生成联动研究' : '生成联动研究'}
            </button>
          </div>

          {error && <div className="daily-error-banner">{error}</div>}
          {data.llm_error && <div className="daily-error-banner">LLM 降级：{data.llm_error}</div>}
          {(data.warnings || []).map((warning, index) => (
            <div className="macro-warning-banner" key={index}>{warning}</div>
          ))}

          <div className="macro-tabs" role="tablist" aria-label="联动研究分区">
            {TABS.map((tab) => (
              <button
                key={tab.key}
                type="button"
                className={activeTab === tab.key ? 'active' : ''}
                onClick={() => setActiveTab(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activeTab === 'overview' && (
            <div className="macro-tab-panel">
              <section className="range-section daily-section-accent">
                <div className="range-section-title">核心摘要</div>
                <p className={`range-summary macro-summary ${expandedSummary ? 'expanded' : ''}`}>
                  {expandedSummary ? data.summary : shortText(data.summary, 180)}
                </p>
                {(data.summary || '').length > 180 && (
                  <button type="button" className="macro-link-btn" onClick={() => setExpandedSummary(!expandedSummary)}>
                    {expandedSummary ? '收起摘要' : '展开摘要'}
                  </button>
                )}
              </section>

              <div className="daily-context-grid">
                <div className="daily-context-card">
                  <div className="daily-context-label">板块相对表现</div>
                  <div className="daily-context-main">
                    <span>{base?.date || data.date}</span>
                    <span className={pctClass(base?.excess_return_pct)}>{signedPct(base?.excess_return_pct)}</span>
                  </div>
                  <div className="daily-context-sub">
                    {base?.available ? '个股相对所属板块的当日超额收益。' : base?.error || '暂无板块行情'}
                  </div>
                </div>
                <div className="daily-context-card">
                  <div className="daily-context-label">产业链位置</div>
                  <div className="daily-context-main">
                    <span>{data.supply_chain?.current_position || data.sector_relations?.board_name || '待识别'}</span>
                  </div>
                  <div className="daily-context-sub">关系和影响路径仅作研究参考，不构成交易建议。</div>
                </div>
              </div>

              <div className="macro-two-col">
                <BulletList title="政策影响" items={data.policy_summary} />
                <BulletList title="国际局势" items={data.global_summary} />
                <BulletList title="可能传导路径" items={data.transmission_paths} />
                <BulletList title="观察点" items={data.watch_points} />
              </div>
              <BulletList title="风险与限制" items={data.risks} />
            </div>
          )}

          {activeTab === 'companies' && (
            <div className="macro-tab-panel">
              <div className="macro-company-actions">
                <div className="daily-context-sub">
                  点击后会为当前候选同行/同链公司补最近日 K，用真实行情填充当日、近 5 日和近 20 日表现；不调用 LLM。
                </div>
                <button
                  type="button"
                  className="range-news-ai-btn stock-report-refresh"
                  onClick={hydratePeers}
                  disabled={hydrating}
                >
                  {hydrating ? '补全中...' : '补全同行行情'}
                </button>
              </div>
              {hydrateMessage && <div className="macro-success-banner">{hydrateMessage}</div>}
              {companyGroups.length > 0 ? companyGroups.map((group) => (
                <section className="range-section macro-company-group" key={`${group.relation_type}-${group.board_name}`}>
                  <div className="macro-group-title">
                    <div>
                      <span>{RELATION_LABELS[group.relation_type] || group.relation_type}</span>
                      <strong>{group.board_name || '未知板块'}</strong>
                    </div>
                    <span className={pctClass(group.performance?.change_pct)}>{signedPct(group.performance?.change_pct)}</span>
                  </div>
                  {group.reason && <p className="daily-context-sub">{group.reason}</p>}
                  <CompanyTable companies={group.companies} />
                  {group.note && <div className="daily-meta">{group.note}</div>}
                </section>
              )) : (
                <div className="news-empty">暂无产业链公司数据。点击生成后会优先补齐所属板块公司。</div>
              )}
            </div>
          )}

          {activeTab === 'sectors' && (
            <div className="macro-tab-panel">
              <section className="range-section">
                <div className="range-section-title">产业链图谱</div>
                <div className="daily-context-grid">
                  <div className="daily-context-card"><div className="daily-context-label">上游</div><div className="daily-context-sub">{(data.supply_chain?.upstream || []).join('；') || '暂无直接证据'}</div></div>
                  <div className="daily-context-card"><div className="daily-context-label">下游</div><div className="daily-context-sub">{(data.supply_chain?.downstream || []).join('；') || '暂无直接证据'}</div></div>
                  <div className="daily-context-card"><div className="daily-context-label">互补行业</div><div className="daily-context-sub">{(data.supply_chain?.complementary || []).join('；') || '暂无直接证据'}</div></div>
                  <div className="daily-context-card"><div className="daily-context-label">替代/互斥</div><div className="daily-context-sub">{(data.supply_chain?.substitute || []).join('；') || '暂无直接证据'}</div></div>
                </div>
              </section>

              <section className="range-section">
                <div className="range-section-title">相关板块表现</div>
                <div className="macro-sector-grid">
                  <div className="daily-analyst-card">
                    <div className="daily-event-top">
                      <span>同板块</span>
                      <span className={pctClass(base?.change_pct)}>{signedPct(base?.change_pct)}</span>
                    </div>
                    <div className="daily-event-title">{data.sector_relations?.board_name || '未知板块'}</div>
                    <div className="daily-context-sub">{base?.available ? `${base.date} · 成交额 ${formatAmount(base.amount)}` : base?.error || '暂无板块行情'}</div>
                  </div>
                  {(data.sector_relations?.related_groups || []).map((group) => (
                    <div className="daily-analyst-card" key={`${group.relation_type}-${group.board_name}`}>
                      <div className="daily-event-top">
                        <span>{RELATION_LABELS[group.relation_type] || group.relation_type}</span>
                        <span className={pctClass(group.performance?.change_pct)}>{signedPct(group.performance?.change_pct)}</span>
                      </div>
                      <div className="daily-event-title">{group.board_name}</div>
                      <div className="daily-context-sub">{group.reason || group.performance?.error || '暂无更多说明'}</div>
                    </div>
                  ))}
                </div>
                {!(data.sector_relations?.related_groups || []).length && (
                  <p className="daily-muted">暂无已沉淀的上下游/互补/替代板块关系；有证据后会在这里展示。</p>
                )}
              </section>
            </div>
          )}

          {activeTab === 'sources' && (
            <div className="macro-tab-panel">
              <section className="range-section">
                <div className="range-section-title">证据来源</div>
                {sourceList.length > 0 ? (
                  <>
                    <div className="daily-events macro-source-list">
                      {visibleSources.map((source, index) => (
                        <a
                          className="daily-event-card"
                          href={source.url || undefined}
                          target={source.url ? '_blank' : undefined}
                          rel={source.url ? 'noopener noreferrer' : undefined}
                          key={source.id || index}
                        >
                          <div className="daily-event-top">
                            <span>{source.date} · {EVENT_LABELS[source.type || 'news'] || source.type}</span>
                            <span>{source.source || '来源'}</span>
                          </div>
                          <div className="daily-event-title">{source.title}</div>
                          {source.summary && <div className="daily-context-sub">{shortText(source.summary, 170)}</div>}
                        </a>
                      ))}
                    </div>
                    {sourceList.length > 5 && (
                      <button type="button" className="macro-link-btn" onClick={() => setShowAllSources(!showAllSources)}>
                        {showAllSources ? '收起证据' : `展开全部 ${sourceList.length} 条证据`}
                      </button>
                    )}
                  </>
                ) : (
                  <p className="daily-muted">暂无来源。点击生成后会写入来源证据。</p>
                )}
              </section>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
