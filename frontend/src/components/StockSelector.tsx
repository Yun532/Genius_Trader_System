import { useEffect, useRef, useState } from 'react';
import axios from 'axios';

interface Ticker {
  symbol: string;
  name: string;
  market?: string;
}

interface Props {
  activeTickers: string[];
  tickerNames?: Record<string, string>;
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
  onAdd: (symbol: string) => void;
}

const DEFAULT_GROUPS: Record<string, string[]> = {
  核心观察: ['sh600519', 'sz000001', 'sz300750', 'sh688981'],
  宽基指数: ['sh000001', 'sz399001', 'sz399006'],
};

function tickerLabel(symbol: string, tickerNames?: Record<string, string>) {
  const name = tickerNames?.[symbol];
  return name && name.toLowerCase() !== symbol.toLowerCase() ? `${name}（${symbol}）` : symbol;
}

export default function StockSelector({ activeTickers, tickerNames, selectedSymbol, onSelect, onAdd }: Props) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Ticker[]>([]);
  const [showSearch, setShowSearch] = useState(false);
  const [showPanel, setShowPanel] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (searchRef.current && !searchRef.current.contains(e.target as Node)) {
        setShowSearch(false);
      }
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setShowPanel(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  function handleSearch(q: string) {
    setQuery(q);
    if (timerRef.current) clearTimeout(timerRef.current);
    if (!q.trim()) {
      setResults([]);
      setShowSearch(false);
      return;
    }
    timerRef.current = setTimeout(async () => {
      try {
        const res = await axios.get<Ticker[]>(`/api/stocks/search?q=${encodeURIComponent(q.trim())}`);
        setResults(res.data);
        setShowSearch(true);
      } catch {
        setResults([]);
      }
    }, 250);
  }

  function handlePick(ticker: Ticker) {
    setQuery('');
    setShowSearch(false);
    setShowPanel(false);
    if (!activeTickers.includes(ticker.symbol)) {
      onAdd(ticker.symbol);
    } else {
      onSelect(ticker.symbol);
    }
  }

  function normalizeDirectSymbol(value: string) {
    const text = value.trim();
    const digits = text.replace(/\D/g, '');
    if (/^(sh|sz|bj)\d{6}$/i.test(text)) return text.toLowerCase();
    if (/^\d{6}$/.test(digits) && digits === text) {
      if (digits.startsWith('6')) return `sh${digits}`;
      if (digits.startsWith('8') || digits.startsWith('9')) return `bj${digits}`;
      return `sz${digits}`;
    }
    return text;
  }

  function submitSearch() {
    const text = query.trim();
    if (!text) return;
    const exact = results.find(
      (item) => item.symbol.toLowerCase() === text.toLowerCase()
        || item.symbol.replace(/^(sh|sz|bj)/, '') === text
        || item.name === text
    );
    if (exact) {
      handlePick(exact);
      return;
    }
    if (/^(sh|sz|bj)?\d{6}$/i.test(text)) {
      const symbol = normalizeDirectSymbol(text);
      setQuery('');
      setShowSearch(false);
      setShowPanel(false);
      if (!activeTickers.includes(symbol)) {
        onAdd(symbol);
      } else {
        onSelect(symbol);
      }
    }
  }

  const activeSet = new Set(activeTickers);
  const renderedGroups = Object.entries(DEFAULT_GROUPS)
    .map(([label, symbols]) => ({ label, symbols: symbols.filter((s) => activeSet.has(s)) }))
    .filter((group) => group.symbols.length > 0);

  const assigned = new Set(renderedGroups.flatMap((group) => group.symbols));
  const ungrouped = activeTickers.filter((symbol) => !assigned.has(symbol)).sort();
  if (ungrouped.length > 0) {
    renderedGroups.push({ label: '已同步', symbols: ungrouped });
  }

  return (
    <div className="stock-selector">
      <div className="ticker-dropdown-wrapper" ref={panelRef}>
        <button className="ticker-current" onClick={() => setShowPanel((v) => !v)}>
          <span className="ticker-current-symbol">{selectedSymbol ? tickerLabel(selectedSymbol, tickerNames) : '选择个股'}</span>
          <span className={`ticker-arrow ${showPanel ? 'open' : ''}`}>▾</span>
        </button>

        {showPanel && (
          <div className="ticker-panel">
            {renderedGroups.length === 0 ? (
              <div className="ticker-panel-group">
                <div className="ticker-panel-group-label">暂无已同步股票</div>
              </div>
            ) : (
              renderedGroups.map((group) => (
                <div className="ticker-panel-group" key={group.label}>
                  <div className="ticker-panel-group-label">{group.label}</div>
                  <div className="ticker-panel-group-items">
                    {group.symbols.map((symbol) => (
                      <button
                        key={symbol}
                        className={`ticker-panel-item ${symbol === selectedSymbol ? 'active' : ''}`}
                        onClick={() => {
                          setShowPanel(false);
                          onSelect(symbol);
                        }}
                      >
                        <span className="ticker-panel-item-name">{tickerLabel(symbol, tickerNames)}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <div className="search-wrapper" ref={searchRef}>
        <input
          type="text"
          placeholder="输入代码或名称，如 600519 / 茅台"
          value={query}
          onChange={(e) => handleSearch(e.target.value)}
          onFocus={() => results.length > 0 && setShowSearch(true)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              submitSearch();
            }
          }}
        />
        {showSearch && results.length > 0 && (
          <ul className="search-dropdown">
            {results.map((ticker) => (
              <li key={ticker.symbol} onClick={() => handlePick(ticker)}>
                <strong>{ticker.symbol}</strong>
                <span>{ticker.name}</span>
              </li>
            ))}
          </ul>
        )}
        {showSearch && results.length === 0 && query.trim() && /^(sh|sz|bj)?\d{6}$/i.test(query.trim()) && (
          <div className="search-dropdown search-empty-action">
            <button type="button" onClick={submitSearch}>
              添加并同步 {normalizeDirectSymbol(query)}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
