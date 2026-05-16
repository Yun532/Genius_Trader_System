# Genius Trader System

**Genius Trader System** is an A-share research workstation adapted from PokieTicker. It keeps the original event-driven chart idea, then rebuilds the data and analysis layer for Chinese equities: daily K lines, news, announcements, financial reports, capital-flow style information, daily explanations, similar days, stock reports, and macro/industry-chain linkage research.

> This is a research and learning tool, not financial advice. It does not generate deterministic buy/sell signals.

[![Demo](docs/demo.gif)](docs/demo.gif)

> The demo GIF is kept from the original PokieTicker reference material for now. The current A-share interface has evolved; a fresh demo GIF should be recorded later.

## Credits and References

This project is built on and inspired by:

- [owengetinfo-design/PokieTicker](https://github.com/owengetinfo-design/PokieTicker) — the original event-driven candlestick research app.
- [Stanleyzrice/PokieTicker-sookice](https://github.com/Stanleyzrice/PokieTicker-sookice) — the A-share-oriented fork used as the first working base.

`reference-original/` is kept locally as a read-only comparison copy during development, but is not committed to this repository.

## What It Does

- **A-share daily K chart** — Search or add Chinese stocks such as `000001`, `600519`, `002339`.
- **Event dots on K lines** — News, announcements, financial reports, and capital-flow style information are aligned to trading dates.
- **Daily reason panel** — Click one trading day to view OHLC, market background, events, and possible drivers.
- **Range analysis** — Select a date range and ask why the stock moved during that period.
- **Event deep dive** — Click an event to inspect summary, sentiment, possible impact path, and similar events.
- **Similar days** — Compare a selected date with historical days using technical and event features.
- **Stock report** — Generate a cached DeepSeek/OpenAI stock research report for the selected trading day.
- **Signal reference** — Show statistical reference, similar-history context, and LLM scenario observations without turning them into trading advice.
- **Macro and industry-chain research** — Explore policy, global macro, sector moves, supply chain, complementary/substitute sectors, and peer-company performance.

## A-share Research Workflow

1. Search or enter an A-share code.
2. The backend syncs daily K data and available events on demand.
3. The chart shows price action and event dots.
4. Click a date to see daily reason analysis.
5. Open stock report or signal reference for broader context.
6. Open macro-chain research for industry, policy, and peer-company linkage.
7. Use "补全同行行情" in the macro-chain panel to explicitly fill peer-company daily K performance.

## Macro and Industry-Chain Panel

The macro-chain view is organized into four tabs:

- **总览** — Core summary, policy, global macro, transmission paths, risks.
- **产业链公司** — Same-sector and related-company table with daily, 5-day, and 20-day returns.
- **板块对比** — Sector and related-sector performance.
- **证据来源** — Collapsed source list; only the first few items are shown by default.

Peer-company data follows this order:

1. Use cached local daily K if available.
2. Use AKShare/EastMoney industry constituents when the upstream endpoint works.
3. Fall back to companies explicitly mentioned in cached source articles.
4. Leave missing returns as `--`; the system does not invent price moves.
5. The "补全同行行情" button hydrates peer-company K data on demand and refreshes the table.

## Architecture

```text
Frontend (React + Vite + D3)
  CandlestickChart
  DailyReasonPanel
  StockReportPanel
  SignalReferencePanel
  MacroChainPanel
        |
        v
Backend (FastAPI + SQLite)
  /api/stocks/{symbol}/sync
  /api/stocks/{symbol}/prices
  /api/stocks/{symbol}/events
  /api/stocks/{symbol}/daily-reason
  /api/stocks/{symbol}/stock-report
  /api/stocks/{symbol}/signal-reference
  /api/stocks/{symbol}/macro-chain
  /api/stocks/{symbol}/sector-relations
        |
        v
Data and Analysis
  AKShare daily K / announcements / reports / boards
  External info search: OpenAI web search or Tavily
  LLM analysis: DeepSeek first, OpenAI optional fallback
  SQLite caches for reports, daily reasons, events, sector relations
```

## Key Backend Interfaces

```text
POST /api/stocks/{symbol}/sync
GET  /api/stocks/{symbol}/prices
GET  /api/stocks/{symbol}/events
GET  /api/stocks/{symbol}/coverage

GET  /api/stocks/{symbol}/daily-reason?date=YYYY-MM-DD
GET  /api/stocks/{symbol}/daily-reasons?start=YYYY-MM-DD&end=YYYY-MM-DD
POST /api/stocks/{symbol}/analyze

POST /api/stocks/{symbol}/refresh-web-info
POST /api/stocks/{symbol}/stock-report
POST /api/stocks/{symbol}/signal-reference

GET  /api/stocks/{symbol}/macro-chain?date=YYYY-MM-DD
POST /api/stocks/{symbol}/macro-chain/refresh?date=YYYY-MM-DD
GET  /api/stocks/{symbol}/sector-relations?date=YYYY-MM-DD
POST /api/stocks/{symbol}/sector-relations/hydrate?date=YYYY-MM-DD

POST /api/analysis/deep
POST /api/analysis/similar
GET  /api/predict/{symbol}/similar-days
```

See [docs/ASHARE_RESEARCH_SYSTEM_SUMMARY.md](docs/ASHARE_RESEARCH_SYSTEM_SUMMARY.md) for a fuller handoff document.

## Quick Start

### 1. Backend

```bash
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m backend.database
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 --reload
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:7777/PokieTicker/
```

## Environment Variables

Copy the example file and fill in only the providers you want to use:

```bash
cp .env.example .env
```

Important variables:

```env
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=

LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_MODEL=gpt-5.4-mini
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1

NEWS_WEB_SEARCH_ENABLED=false
NEWS_WEB_SEARCH_PROVIDER=openai
NEWS_WEB_SEARCH_MAX_RESULTS=8
NEWS_WEB_SEARCH_CACHE_TTL_HOURS=24

TAVILY_API_KEY=
```

Notes:

- If you use OpenAI web search, `OPENAI_BASE_URL` should point to an endpoint that supports `/responses` and `web_search`.
- A local OpenAI-compatible proxy may work for chat completions but fail for web search.
- DeepSeek is used for analysis, not for finding news facts by itself.
- Tavily can be used as a search provider; its results are then analyzed by the LLM layer.

## Cache and Cost Control

The system is intentionally conservative with LLM/search usage:

- Syncing K lines and local events does not require an LLM key.
- Daily reason has local-rule fallback.
- Stock reports are cached by stock/date/context/model.
- Range analysis and signal reference use TTL caches.
- Macro-chain research reads cache by default.
- External search and LLM macro-chain refresh should be user-triggered.
- The peer-company hydration button fetches price data only; it does not call LLM or web search.

## Project Structure

```text
backend/
  api/
    main.py
    routers/
      stocks.py        # A-share sync, reports, macro-chain, daily reason
      news.py          # Original-compatible news endpoints
      analysis.py      # Deep dive and similar events
      predict.py       # Similar days / compatibility forecast
  ashare/
    client.py          # AKShare data access
    symbol.py          # A-share symbol normalization
  database.py          # SQLite schema and migrations
  llm.py               # DeepSeek/OpenAI provider layer
  web_search.py        # OpenAI/Tavily external information search

frontend/
  src/
    App.tsx
    App.css
    components/
      CandlestickChart.tsx
      DailyReasonPanel.tsx
      StockReportPanel.tsx
      SignalReferencePanel.tsx
      MacroChainPanel.tsx
      SimilarDaysPanel.tsx
      RangeAnalysisPanel.tsx

docs/
  ASHARE_RESEARCH_SYSTEM_SUMMARY.md
  demo.gif
  screenshot.png
```

## Current Limitations

- Single-stock, on-demand research only.
- Daily-level analysis only; no intraday minute-level reasoning yet.
- No automatic trading or deterministic buy/sell recommendation.
- External search depends on OpenAI web search or Tavily availability.
- AKShare/EastMoney endpoints may fail depending on network conditions.
- Industry-chain relations are still early-stage and should be treated as research references.
- Qlib/FinRL are not integrated yet; prediction remains statistical/reference-oriented.

## License

MIT. See [LICENSE](LICENSE).
