# 天才交易员系统

天才交易员系统是一个面向 A 股研究的本地工作台，基于 PokieTicker 的事件驱动 K 线研究思路改造而来。它把日 K、新闻、公告、财报、资金信息、每日涨跌原因、相似历史、个股报告、产业链联动和大盘云图放在同一个界面里，方便做复盘和研究。

> 本项目只用于研究、学习和复盘，不构成投资建议，也不会生成确定性的买卖信号。

[![演示](docs/demo.gif)](docs/demo.gif)

> 当前演示 GIF 仍沿用早期参考素材，A 股界面已经继续演进，后续可重新录制。

## 主要功能

- **A 股日 K 研究**：支持输入 `000001`、`600519`、`002339` 等 A 股代码。
- **事件点对齐 K 线**：将新闻、公告、财报、资金类信息按交易日挂到 K 线上。
- **每日涨跌原因**：点击某一天，查看 OHLC、市场背景、行业表现、事件影响和可能驱动因素。
- **区间归因**：拖选一段 K 线区间，询问这段上涨或下跌由什么驱动。
- **事件深挖**：点击新闻或公告，查看摘要、情绪、影响路径和相似事件。
- **相似历史**：根据技术特征和事件特征寻找历史相似交易日。
- **个股报告**：按交易日生成并缓存 DeepSeek/OpenAI 风格的研究报告。
- **信号参考**：展示统计参考、相似历史和情景观察，但不输出交易指令。
- **产业链联动**：查看政策、宏观、行业、上下游、替代/互补板块和同行表现。
- **大盘云图**：按行业聚类展示全行业热力图，红涨绿跌，点击板块查看成分股、涨停股、强势股、领涨/领跌样本。

## 大盘云图

大盘云图是当前版本新增的市场视图：

- 顶部可在 **个股研究 / 大盘云图** 间切换。
- 面积优先表示行业板块成交额或可用权重。
- 颜色采用 A 股习惯：红色上涨，绿色下跌，灰色表示本地暂无可靠涨跌快照。
- 默认读取本地 SQLite 快照，打开页面不依赖实时联网。
- 交易日 15:20 后后台尝试刷新行业快照，并异步暖成分股缓存。
- 右侧详情面板展示涨停成分股、强势成分股、成交额靠前、领涨和领跌样本。
- 对单个板块可手动点击“联网补齐成分股”，用于在公开行情接口可用时补全样本。

当前云图仍是“行业 + 成分股详情”版本，不是全 A 股个股铺满版本。后续如果需要对齐全市场个股云图，可以升级为“行业分组 -> 行业内全部个股矩形”的结构。

## 技术架构

```text
Frontend: React + Vite + D3
  App
  CandlestickChart
  MarketHeatmap
  DailyReasonPanel
  StockReportPanel
  SignalReferencePanel
  MacroChainPanel
        |
        v
Backend: FastAPI + SQLite
  /api/stocks/*
  /api/market/*
  /api/news/*
  /api/analysis/*
  /api/predict/*
        |
        v
Data and Analysis
  AKShare / 东方财富 / 同花顺公开数据
  SQLite 本地缓存
  DeepSeek 优先，OpenAI 可选回退
  OpenAI Web Search 或 Tavily 可选外部搜索
```

## 常用接口

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

GET  /api/market/heatmap?period=1d|5d|20d
GET  /api/market/sector/{board_name}/constituents

POST /api/analysis/deep
POST /api/analysis/similar
GET  /api/predict/{symbol}/similar-days
```

## 快速开始

### 1. 后端

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m backend.database
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 --reload
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

打开：

```text
http://localhost:7777/PokieTicker/
```

## 环境变量

复制示例文件：

```bash
cp .env.example .env
```

按需填写：

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

说明：

- 日 K、行业快照和本地缓存不需要 LLM key。
- DeepSeek/OpenAI 只用于报告、解释、宏观链路等分析类功能。
- OpenAI Web Search 需要支持 `/responses` 和 `web_search` 的 OpenAI 兼容端点。
- Tavily 可作为外部搜索来源，搜索结果再交给 LLM 做分析。
- `.env` 不应提交到仓库。

## 缓存与成本控制

系统默认尽量少调用外部服务：

- K 线同步和本地事件整理不需要 LLM。
- 每日涨跌原因有本地规则兜底。
- 个股报告、区间分析、信号参考、产业链研究都有缓存。
- 大盘云图默认读 SQLite 快照，页面打开不触发实时联网。
- 盘后后台任务负责尝试刷新行业快照和成分股缓存。
- 成分股“联网补齐”是用户主动触发，不会自动阻塞界面。

## 项目结构

```text
backend/
  api/
    main.py
    routers/
      stocks.py        # A 股同步、报告、产业链、每日原因
      market.py        # 大盘云图和行业成分股
      news.py          # 新闻兼容接口
      analysis.py      # 深度分析和相似事件
      predict.py       # 相似历史 / 预测参考
  ashare/
    client.py          # AKShare 数据访问
    symbol.py          # A 股代码标准化
  database.py          # SQLite schema 和迁移
  llm.py               # DeepSeek/OpenAI provider
  web_search.py        # OpenAI/Tavily 外部搜索

frontend/
  src/
    App.tsx
    App.css
    components/
      CandlestickChart.tsx
      MarketHeatmap.tsx
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

## 数据源说明

本项目主要通过 AKShare 调用东方财富、同花顺等公开数据源。公开行情接口可能受网络、限频、反爬或节假日状态影响而失败。系统设计上会优先使用本地缓存，并在接口失败时保留已有快照，避免页面空白或被慢请求阻塞。

## 当前限制

- 当前是本地研究工具，不是多人在线系统。
- 大盘云图 v1 是行业云图，不是全市场个股矩形云图。
- 成分股覆盖率取决于公开数据接口和本地缓存。
- 分钟级实时行情和盘中 8 秒刷新尚未实现。
- 外部搜索依赖 OpenAI Web Search 或 Tavily 可用性。
- AKShare/东方财富/同花顺接口可能因网络环境失败。
- 预测和信号功能只做研究参考，不构成交易建议。

## 致谢

本项目基于并参考：

- [owengetinfo-design/PokieTicker](https://github.com/owengetinfo-design/PokieTicker)
- [Stanleyzrice/PokieTicker-sookice](https://github.com/Stanleyzrice/PokieTicker-sookice)
- [dapanyuntu/yuntu](https://github.com/dapanyuntu/yuntu) 的大盘云图产品形态启发

`reference-original/` 仅作为本地开发对照目录，不提交到仓库。

## 许可证

MIT。详见 [LICENSE](LICENSE)。
