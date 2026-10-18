# 数据来源与口径（含真实接口核验）

本产品的所有个股数值都来自 **扶摇（同花顺金融数据 API）** 的 REST 接口；iFinD MCP 只用于个股资讯背景，不参与筛选与打分。
每个数值在界面上都能展开到：接口路径、原始字段、request_id、数据时点、获取时间；派生指标另附公式与输入。

核验方式：`scripts/probe_fuyao.py`（需本地 `.env`），2026-10-17（美中时间，对应北京时间 10-18 凌晨，周日）用真实 Key 运行。
下表“实测”列是当次运行的结构性结论；本仓库不保存任何原始供应商数据。

## 1. 使用的接口

| 数据集 | 接口 | 用途 | 实测 |
|---|---|---|---|
| 代码表 | `GET /api/meta/tickers/list?asset_type=a-share` | 名称、上市日期、ST 判断、全 A 股票池 | 5580 只 A 股（沪 2320 / 深 2907 / 北 353），名称含 ST 的 201 只 |
| 交易日历 | `GET /api/a-share/calendar/trading-days` | 最近交易日 | 近一年 241 个交易日，最后一个交易日 2026-10-16 |
| 指数成分 | `GET /api/a-share-index/constituents/ths-stock-list` | 股票池 | 沪深300=300、中证500=500、中证1000=1000、中证800=800、上证50=50、创业板指=100、科创50=50 均可用 |
| 同花顺行业/概念目录 | `GET /api/a-share-index/catalog/ths-index-list?tag=industry\|cn_concept` | 行业归属、行业/概念股票池 | 行业 320 个（二级 881xxx 共 90 个、三级 884xxx 共 230 个），概念 390 个 |
| 行情快照 | `GET /api/a-share/prices/snapshot` | 最新价、涨跌幅 | 一次传 150 个代码也能返回；产品按 100 个一批 |
| 估值快照 | `GET /api/a-share/valuations/snapshot` | PE/PB/PS/PCF | 一次最多 100 个，101 个返回 `code=1003 thscodes count must not exceed 100` |
| 财务指标 | `GET /api/a-share/financials/indicators?report=yyyy-q` | 成长、盈利、偿债、现金流指标 | 24 个指标；**成长类字段名与文档不同**（见 §3） |
| 利润表 | `GET /api/a-share/financials/income-statements?period=quarterly&limit=20` | 营收/利润金额、TTM、回测自算成长 | 季报为**年初至今累计值**；Q4 即年报 |
| 历史日K | `GET /api/a-share/prices/historical?adjust=backward` | 波动率、回撤、区间涨跌、成交额 | 2022-01-04 起 1157 根日K，最后一根 2026-10-16 |
| 指数日K | `GET /api/a-share-index/prices/historical` | Beta 基准、回测基准 | 沪深300 近 5 年 1202 根 |
| 分红事件 | `GET /api/a-share/corporate-actions/adjustment-factors` | 近 12 个月股息率 | 返回除权除息日与每股现金分红 |

限流：20 并发 × 3 轮共 60 次财务指标请求全部成功，p50 延迟 0.33 秒、最大 1.03 秒（从美国访问）。产品默认并发 6、每秒 8 次，并对 429 / `code=4001` 退避重试。

## 2. 口径约定

- **报告期**：默认取“法定披露截止日已过的最近一期”。2026-10-17 时为 **2026 年中报（2026-2）**；三季报截止日 10-31 未到，且实测抽样 20 只股票的 2026-3 指标全部返回 `code=5003`、利润表最新一期全部是 2026Q2。用户可手动指定报告期，覆盖不足的股票会进入“数据缺失”并写明原因，不会被当作通过。
- **同比/变动**：同口径累计值比较（本期累计 vs 上年同期累计）。ROE、毛利率的“同比变动”用百分点差，避免累计值年化误差。
- **ROE 绝对阈值**：接口 ROE 为期内累计值（茅台 2026 中报 16.75%，未年化），与“ROE≥15%”这类年度口径不可比，所以词典里“盈利能力强”默认映射到“年化估算 ROE”（累计值 × 4 ÷ 季度数），并标注“估算”。
- **价格窗口**：后复权日收盘价；波动率 = 日对数收益率标准差 × √252；最大回撤为正数；窗口截至最近交易日。
- **行业同业组**：同花顺二级行业（881xxx.TI）。三级行业常不足 10 家，分位统计不稳。
- **估值时点**：估值与行情快照的 `data.timestamp` 是服务端就绪时间（周日凌晨运行时显示 2026-10-18 03:36），不是成交时间。界面上的行情时点取交易日历/日K的最后交易日（2026-10-16 收盘），并单独展示接口返回的 timestamp。
- **金融企业**：银行等无毛利率、流动比率（接口返回 null，招商银行、平安银行实测均如此），这些条件对它们记为“不适用”，不记为通过。

## 3. 与官方文档不一致、已按实测修正的地方

| # | 文档写法 | 实测 | 处理 |
|---|---|---|---|
| 1 | 成长类 `index_id` 为 `operating_income_yoy_growth_ratio` / `net_profit_yoy_growth_ratio` / `operating_profit_yoy_growth_ratio` | 实际为 `calculate_operating_income_yoy_growth_ratio` / `calculate_parent_holder_net_profit_yoy_growth_ratio` / `calculate_operating_profit_yoy_growth_ratio`；另有 `fixed_asset_invest_expansion_ratio` | `catalog.INDICATOR_FIELDS` 以实测为准；“净利润同比”更名为“**归母**净利润同比增长率” |
| 2 | 利润表 `report_date_ms` 为披露日 | 历史期的披露日会被后续报告覆盖：茅台 2025Q2 显示 2026-08-15、2024Q4 显示 2026-04-17（即包含该期比较数据的最新报告日期） | 回测判断“当时是否可得”改用法定披露截止日（一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30），不用 `report_date_ms` |
| 3 | 未就绪数据返回 `3002` | 尚未披露的报告期（2026-3）返回 `5003 Data source unavailable` | 财务指标的 5003 只重试 1 次；报告期尚未到法定披露截止日时，原因追加“该报告期尚未到法定披露截止日，可能尚未披露” |
| 4 | — | 平安银行 2026Q1：加权 ROE 2.83%，扣非加权 ROE 9.15%（二者本应接近） | 扣非 ROE 不进默认解释；作为“数据冲突”样例写进测试说明 |

## 4. 派生指标复核（用实测原始数值手算）

| 指标 | 公式 | 复核 |
|---|---|---|
| 营收同比 | (本期累计 ÷ 上年同期累计 − 1) × 100 | 茅台 2026H1：自算 1.4699%，接口 1.469869% ✓ |
| 归母净利润同比 | 同上 | 茅台 2026H1：自算 −1.9516%，接口 −1.951595% ✓ |
| 估算总市值 | 优先 PE × 归母净利润TTM；PE≤0 用 PS × 营收TTM | 茅台：PE 法 16613.6 亿、PS 法 16317.6 亿（低 1.78%，PS 可能按营业总收入计算）；平安银行两法均为 2231.7 亿。故以 PE 法为主 |
| 股息率（近 12 个月） | Σ 近 365 天每股现金分红 ÷ 最新价 | 茅台：(28.02 + 23.96) ÷ 1329 = 3.91% |

## 5. iFinD MCP

- 端点：`https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-mcp`（综合服务；另有 stock / news / edb 等分服务），JSON-RPC 2.0 over HTTP（streamable HTTP），请求头 `Authorization: <密钥>`；`initialize` 后返回 `Mcp-Session-Id`，后续请求需带上。
- 密钥获取：iFinD MCP 官网 → 个人中心 → 密钥管理。产品后端直接调用 MCP，**不需要**安装官网提供的 Skill。
- 实测 `tools/list` 共 9 个工具：`get_stock_summary`、`fetch_edb`、`search_edb`、`search_notice`、`search_news`、`search_trending_news`、`security_highfreq_quotes`、`get_security_indicators`、`search_securities`。
- 产品使用 `search_news`（参数 `query`、`size`≤20、`time_start`、`time_end`）展示候选股近 30 天资讯；返回为 `result.content[0].text` 里的 JSON 字符串，其 `data.data` 又是一层 JSON 字符串，条目字段为 `资讯标题 / 资讯内容 / 日期 / URL`。实测第一条可能是工商登记信息（URL 指向企业信息站），产品会过滤掉。
- 资讯只作背景展示，统一标注“不参与筛选与打分”；资讯原文中的观点不代表本产品观点。

### 跨数据源核验：估算市值 vs iFinD 总市值

扶摇暂未提供总市值字段，产品用 `PE(TTM) × 归母净利润TTM` 估算。用 iFinD `get_security_indicators`（query“贵州茅台 平安银行 最新总市值”）独立取数比对：

| 股票 | 产品估算（PE 法） | 旧公式（PS 法） | iFinD 总市值 |
|---|---|---|---|
| 贵州茅台 | 16613.6 亿 | 16317.6 亿 | 1.6614 万亿 |
| 平安银行 | 2231.7 亿 | 2231.7 亿 | 2231.68 亿 |

PE 法与 iFinD 一致（相对误差 < 0.01%），PS 法对茅台偏低约 1.8%，因此以 PE 法为主公式。界面仍标“估算”。

第二轮核验（`scripts/verify_traceability.py`，沪深300 入选股前 5 只）：海康威视 0.00%、亿联网络 0.00%、美的集团 +0.42%；申万宏源 +5.77%、广发证券 +6.66%。后两者均为 A+H 两地上市，推测扶摇 PE 以 A 股价格对应全部股本，而 iFinD 总市值按 A、H 股各自价格计算，H 股折价带来差异——这是口径差异的推断，未逐家核实。

## 6. 大模型

- DeepSeek OpenAI 兼容接口，`/models` 实测可用模型为 `deepseek-flash`、`deepseek-v4-pro`。
- 同一解析任务（意图→条件 JSON）：`deepseek-chat`（实际由 deepseek-flash 非思考模式响应）3.2 秒；`deepseek-flash` 13.2 秒；`deepseek-v4-pro` 30.6 秒；三者输出的指标 id 均在目录内。交互场景默认 `deepseek-chat`，可用 `LLM_MODEL` 切换。
