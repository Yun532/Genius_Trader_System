# 天才交易员系统：A 股研究版项目说明

## 1. 项目定位

本项目基于 PokieTicker 改造，目标不是直接给出买卖信号，而是做一个 **A 股单股研究工作台**。

当前第一阶段重点是“事件研究闭环”：

- 输入 A 股代码或名称，按需同步单只股票数据。
- 展示日 K 线、事件点、新闻、公告、财报、资金类资讯。
- 点击某根 K 线后，查看当日涨跌可能原因。
- 选择区间后，查看区间事件和区间归因。
- 对单条事件做深挖，并查看相似事件。
- 查看相似日期、信号参考、股票分析报告。
- 新增“宏观与产业链 / 联动研究”，用于理解政策、行业、上下游、互补/替代行业和同行公司表现。

系统所有分析都应基于已抓取或已缓存的事实数据，不允许 LLM 凭记忆补新闻或编造事实。

## 2. 当前核心能力

### 2.1 A 股行情与事件

- 行情数据源优先使用 AKShare。
- 第一版使用日 K，不做分钟线或实时盘中解释。
- 股票代码支持常见格式，例如 `000001`、`600519`、`002339`，后端会规范成 `sz000001`、`sh600519` 等内部格式。
- 事件统一进入 `market_events`，主要类型包括：
  - `news`：新闻/资讯。
  - `announcement`：公告。
  - `financial_report`：财报。
  - `capital`：资金、游资、龙虎榜、主力资金等。
  - `policy`：政策。
  - `global_macro`：国际局势。
  - `sector`：板块资讯。
  - `supply_chain`：产业链资讯。

### 2.2 每日涨跌归因

点击 K 线某一天后，右侧展示“当日涨跌原因”。

上下文包括：

- 当日 OHLC、涨跌幅、成交额、换手率、振幅。
- 当日及前一晚新闻、公告、财报、资金类事件。
- 大盘背景：上证指数、深证成指、创业板指。
- 行业板块表现。
- 北向资金背景。
- 已缓存的宏观与产业链摘要。

无 LLM key 时使用本地规则归因；有 DeepSeek/OpenAI key 时，可基于系统上下文生成中文解释。

### 2.3 搜索资讯

“搜索资讯”不只搜索新闻，也覆盖：

- 资金流向。
- 游资席位。
- 龙虎榜。
- 融资融券。
- 行业资讯。
- 政策和产业链变化。

外部搜索只负责找事实来源。DeepSeek/OpenAI 只负责基于这些事实做整理和归因。

当前外部搜索 provider 主要支持：

- OpenAI web search。
- Tavily。

注意：如果 `OPENAI_BASE_URL` 指向本地 OpenAI 兼容代理，例如 `http://127.0.0.1:8317/v1`，但该代理不支持 `/responses` 和 `web_search`，外部搜索会失败或超时。

### 2.4 股票分析报告

顶部“股票分析”按钮会生成独立报告，适合回答：

- 公司现状如何。
- 后续潜力在哪里。
- 当前积极因素和风险是什么。
- 后续应观察哪些催化或风险。

报告会缓存到当天。同一天再次打开优先读缓存；只有主动刷新才重新调用 LLM。

### 2.5 信号参考

“信号参考”不是买卖信号，而是研究参考。

内容包括：

- 相似历史日期统计。
- 近期事件和情绪。
- 市场、行业、资金背景。
- LLM 情景推演。
- 未来 1/5/10 个交易日观察点。

前端明确避免显示确定涨跌预测或买卖建议。

### 2.6 联动研究：宏观与产业链

联动研究是新增的大视图，用于展示：

- 政策影响。
- 国际局势。
- 所属行业表现。
- 产业链上下游。
- 互补/替代/竞争行业。
- 相关板块表现。
- 板块内或同链公司表现。
- 证据来源。

界面已改成四个分区：

- `总览`
- `产业链公司`
- `板块对比`
- `证据来源`

证据来源默认只展示前 5 条，避免页面被长资讯撑开。

## 3. 关键后端接口

### 3.1 股票与行情

- `POST /api/stocks/{symbol}/sync`
  - 同步单只股票数据。
  - 返回日 K、新闻、公告、财报等分源状态。

- `GET /api/stocks/{symbol}/prices`
  - 返回日 K。

- `GET /api/stocks/{symbol}/events`
  - 返回统一事件列表。

- `GET /api/stocks/{symbol}/coverage`
  - 返回行情、新闻、公告、财报覆盖情况。

### 3.2 每日和区间归因

- `GET /api/stocks/{symbol}/daily-reason?date=YYYY-MM-DD`
  - 返回单日涨跌可能原因。

- `GET /api/stocks/{symbol}/daily-reasons?start=...&end=...`
  - 批量返回每日归因缓存摘要。

- `POST /api/stocks/{symbol}/analyze`
  - 对选定日期区间做归因。

- `POST /api/analysis/range`
  - 兼容原版区间归因接口。

### 3.3 事件深挖和相似能力

- `POST /api/analysis/deep`
  - 单条事件深挖。

- `POST /api/analysis/similar`
  - 相似事件。

- `GET /api/predict/{symbol}/similar-days`
  - 相似日期。

- `GET /api/predict/{symbol}/forecast`
  - 兼容原版预测接口，但前端只作为实验统计参考。

### 3.4 搜索资讯和股票报告

- `POST /api/stocks/{symbol}/refresh-web-info`
  - 搜索外部资讯并入库。

- `POST /api/stocks/{symbol}/stock-report`
  - 生成或读取股票分析报告。

- `POST /api/stocks/{symbol}/signal-reference`
  - 生成信号参考。

### 3.5 联动研究

- `GET /api/stocks/{symbol}/macro-chain?date=YYYY-MM-DD`
  - 读取某日宏观与产业链缓存。
  - 默认不应触发外部搜索或 LLM。

- `POST /api/stocks/{symbol}/macro-chain/refresh?date=YYYY-MM-DD`
  - 手动刷新联动研究。
  - 可使用搜索和 LLM。

- `GET /api/stocks/{symbol}/sector-relations?date=YYYY-MM-DD`
  - 返回所属板块、相关板块、公司列表和板块表现。

- `POST /api/stocks/{symbol}/sector-relations/hydrate?date=YYYY-MM-DD&max_companies=12`
  - 用户点击“补全同行行情”时调用。
  - 只补候选同行/同链公司的日 K，不调用 LLM，不调用外部搜索。
  - 补完后返回新的 `sector_relations`，前端刷新公司表。

## 4. 数据库和缓存

当前继续使用 SQLite，主数据库为 `pokieticker.db`。

重要表包括：

- `tickers`：股票基础信息。
- `ohlc`：个股日 K。
- `market_events`：统一事件。
- `news_raw`、`news_ticker`、`news_aligned`：新闻兼容原版管线。
- `layer1_results`、`layer2_results`：事件摘要、情绪、深挖结果。
- `financial_reports`：财报。
- `market_index_ohlc`：大盘指数日 K。
- `industry_board_ohlc`：行业板块日 K。
- `daily_reason_cache`：每日归因缓存。
- `analysis_cache`：股票报告、区间归因、信号参考、外部搜索等通用缓存。
- `macro_chain_context`：宏观与产业链研究缓存。
- `sector_relation_map`：行业上下游/互补/替代关系。
- `sector_constituents_cache`：板块成分股或同链候选公司缓存。

## 5. LLM 和成本控制

默认模型策略：

- 主力：DeepSeek。
- 可选 fallback：OpenAI。

重要原则：

- LLM 只分析系统提供的上下文。
- LLM 不负责寻找事实。
- LLM 不凭记忆判断 A 股。
- 无 key 时，行情、事件、每日归因、联动研究空态和本地规则仍可用。

成本控制策略：

- 同一天股票分析报告优先读缓存。
- 区间归因和信号参考有 TTL 缓存。
- 历史日期默认不主动调用 LLM。
- 联动研究默认读取缓存。
- 只有用户点击“生成/刷新联动研究”才应调用外部搜索或 LLM。
- “补全同行行情”只补行情，不调用 LLM。

## 6. 关键配置

配置来自 `.env`。

常见配置：

```env
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=...

LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_MODEL=gpt-5.4-mini
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1

NEWS_WEB_SEARCH_ENABLED=false
NEWS_WEB_SEARCH_PROVIDER=openai
NEWS_WEB_SEARCH_MAX_RESULTS=8
NEWS_WEB_SEARCH_CACHE_TTL_HOURS=24

TAVILY_API_KEY=...
```

注意：

- 如果使用 OpenAI web search，`OPENAI_BASE_URL` 最好是官方 `https://api.openai.com/v1`。
- 如果 `OPENAI_BASE_URL` 指向本地代理，必须确认该代理支持 `/responses` 和 `web_search`。
- DeepSeek 原生 API 当前不等同于 web search；如果要让 DeepSeek 使用外部搜索，需要 Tavily 或其他搜索工具先提供事实，再交给 DeepSeek 分析。

## 7. 前端结构

当前页面名和系统名为：

**天才交易员系统**

主要交互：

- 顶部搜索框：输入股票代码或名称，切换/添加股票。
- 主区域：K 线图和事件点。
- 底部：资讯、公告、财报、资金等事件列表和筛选。
- 右侧研究面板：每日归因、事件深挖、相似事件、区间归因、信号参考入口。
- 大弹窗：
  - 股票分析报告。
  - 联动研究。

K 线颜色按 A 股习惯：

- 红色：上涨。
- 绿色：下跌。

## 8. 已知限制

当前仍是第一阶段研究工具，有以下限制：

- 只做单股按需研究，不做全市场初始化。
- 只做日线级别，不做分钟线和盘中实时归因。
- 不生成确定买卖建议。
- 外部搜索依赖 OpenAI web search 或 Tavily，网络或代理不可用时会失败。
- 行业成分股接口依赖 AKShare/东方财富，网络不可用时会降级为“资讯中提到的公司”。
- 候选同行公司如果本地没有日 K，且资讯里也没有明确涨跌，表格会显示 `--`，不会编造。
- 行业上下游/互补/替代关系第一版主要来自搜索证据和 LLM 结构化，后续还需要沉淀更稳定的人工/半自动关系库。

## 9. 最近完成的重点改造

最近一轮主要完成：

- 新增宏观与产业链联动研究。
- 新增 `macro_chain_context`、`sector_relation_map`、`sector_constituents_cache`。
- 联动研究页面改成四个分区，避免长报告难读。
- 产业链公司表新增当日、近 5 日、近 20 日涨跌。
- 新增“补全同行行情”按钮。
- 外部联动搜索失败改成温和提示，不再表现为整个研究失败。
- 搜索资讯覆盖资金、游资、龙虎榜等，不再只叫新闻。
- 股票分析报告加入缓存，同一天重复打开很快。
- 布局支持拖拽调整，K 线会随容器重绘。

## 10. 建议下一步

建议后续按以下顺序推进：

1. 修正 `NEWS_WEB_SEARCH_ENABLED=false` 时联动研究仍默认尝试外部搜索的问题。
2. 为 OpenAI web search 和普通 LLM 调用拆分 base URL，避免本地代理不支持 `/responses` 时影响搜索。
3. 给“补全同行行情”增加前端进度和失败明细展示。
4. 建立更稳定的行业关系库，沉淀上游、下游、互补、替代行业。
5. 增加行业/概念板块成分股的多源 fallback。
6. 为联动研究增加“补全后自动重算摘要”选项，但默认仍不自动调用 LLM。
7. 后续如需预测，优先评估 Qlib 管线；不要直接沿用原版美股预测模型。
