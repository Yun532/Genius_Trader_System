# PokieTicker 原版功能保真记录

本文件用于记录当前 A 股版与 `reference-original` 原版之间的功能关系。`reference-original` 仅作为只读对照，不参与运行。

## 已保留

- K 线主视图：继续使用 `CandlestickChart` 展示日 K 和事件点。
- 事件点交互：支持 hover、click lock、按日期打开右侧事件面板。
- 区间选择：支持在 K 线上选择日期区间。
- 区间事件列表：继续通过 `/api/news/{symbol}/range` 返回区间事件。
- 区间归因：保留 `/api/analysis/range`，并新增 `/api/stocks/{symbol}/analyze`。
- 事件筛选：保留分类筛选 UI，分类改为 A 股语境。
- 原版兼容接口：`/api/news/{symbol}`、`/api/news/{symbol}/particles`、`/api/news/{symbol}/categories`、`/api/news/{symbol}/timeline` 继续可用。
- 每日归因：新增 `/api/stocks/{symbol}/daily-reason`，点击 K 线单日或事件点后可查看当日涨跌的证据驱动可能原因，并保留相似日期入口。
- 事件深挖：`/api/analysis/deep` 已改为 A 股中文事件分析，支持新闻、公告、财报和外部新闻；无 LLM key 时返回本地规则分析。
- 相似事件：`/api/analysis/similar` 已恢复入口，前端事件卡片可直接打开相似事件；TF-IDF 不足时按事件类型、情绪和标题重合降级。
- 右侧研究台：原右侧预测区域已变成研究面板，可切换每日归因、事件深挖、相似事件、相似日期、区间归因和信号参考。

## A 股替代

- 行情源：Polygon OHLC 替换为 AKShare A 股日 K。
- 代码体系：支持 `000001`、`600519`、`sz000001`、`sh600519` 等格式，并统一为内部 `sz000001` / `sh600519`。
- 事件源：原新闻事件扩展为 `news`、`announcement`、`financial_report`。
- 财报：独立写入 `financial_reports`，同时映射为 K 线事件点。
- 公告：优先使用 AKShare `stock_individual_notice_report`，按股票代码和日期区间抓取。
- LLM：Claude 专用调用替换为可插拔 provider，默认 DeepSeek，可选 OpenAI fallback。
- 同步体验：`POST /api/stocks/{symbol}/sync` 返回日 K、新闻、公告、财报分源状态；日 K 失败时不会阻塞其他源，若有缓存则继续展示缓存。

## 暂时降级

- 预测面板：原版 XGBoost 预测不作为第一阶段展示重点，避免未重训 A 股模型时误导用户。
- 信号参考：新增 `/api/stocks/{symbol}/signal-reference`，用相似历史统计、近期事件、市场/行业/资金、机构预期和 LLM 情景推演替代强预测，不给买卖建议。
- Batch API：原版 Anthropic Batch API 在 A 股版中暂不启用，批量事件处理先走通用 LLM 调用。
- Story/deep analysis：接口保留，但输出语境改为中文 A 股，并依赖配置好的 LLM key。

## 待恢复或增强

- A 股专用预测模型：需要重新设计特征和训练，不能直接沿用原美股模型。
- 更稳定的多源新闻：当前先用 AKShare/EastMoney，后续可加公告原文、巨潮、交易所公告。
- 相似事件语义检索：当前已有 TF-IDF/规则降级；后续可加中文 embedding 或向量库提升语义相似质量。
- 财报公告日期精确化：当前财报事件以报告期或可得披露日期映射，后续应尽量使用真实披露日。

## 验收目标

第一阶段的功能保真目标是：用户输入一只 A 股股票后，可以看到日 K、事件点、事件详情、事件分类、事件深挖、相似事件、区间事件、区间归因、每日归因、相似日期和信号参考；即使没有 LLM key，也不能影响基础行情和事件研究闭环。
