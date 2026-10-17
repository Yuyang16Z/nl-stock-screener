# 设计规格：自然语言智能选股与策略解释器（ScreenLens）

> 本文是实现契约。所有模块以 `backend/app/schemas.py`（数据结构）与 `backend/app/catalog.py`（指标目录 + 词典）为唯一真源；
> 本文定义模块职责、函数签名与语义。实现者不得改动 schemas/catalog 的既有字段语义；如需新增字段，只能“追加可选字段”。

## 0. 产品一句话

把“经营改善、估值合理、走势相对稳定”这类模糊意图，变成 **用户可检查、可修改、可执行** 的数据条件；
由 **确定性引擎** 在真实数据上执行；对每只股票给出 **入选 / 排除 / 数据缺失** 的逐条依据（数值、阈值、来源接口、字段、时点、口径）；
并量化 **条件变化的影响**（去掉某条件会多几只、阈值放宽到多少会怎样、谁差一点入选）。结果可继续 **比较 / 保存 / 回测 / 转监控**。

## 1. 职责边界（评审重点）

| 角色 | 做什么 | 绝不做什么 |
|---|---|---|
| **AI（LLM，DeepSeek，OpenAI 兼容）** | ① 把自然语言映射为“指标目录内”的候选条件草案，并标注来源短语与理由；② 为模糊词生成澄清问题与备选解释；③ 提示语义冲突（如“高成长+低估值”过严）；④ 用引擎给出的结构化证据写解释文案；⑤ 用自然语言修改既有条件（输出补丁，用户确认后生效） | 不产生任何个股数值；不决定入选；不直接调数据接口；不预测涨跌、不给买卖建议、不承诺收益 |
| **确定性筛选引擎（Python）** | 校验条件（指标存在、算子合法、阈值合理、区间冲突）；按公开公式计算派生指标；逐股逐条件判定 pass / fail / missing / not_applicable；漏斗、单独卡点、去条件影响、阈值扫描、差一点入选、结果 diff；回测；同输入同输出 | 不“猜”缺失值；缺失/失败/过期不判为通过 |
| **金融数据工具** | 扶摇 REST：行情快照、历史 K 线、估值快照、财务指标、利润表、指数/行业成分、代码表、交易日历、分红；iFinD MCP：候选股资讯背景 | 数据层只透传与记录来源（endpoint、参数、request_id、data.timestamp、fetched_at），错误码映射为显式状态，绝不编造 |

数据流：`用户NL → AI解析(草案+澄清+冲突) → 用户检查/修改 → 引擎校验(确定性冲突) → 数据层按条件依赖取数 → 特征计算(带证据) → 引擎判定 → 结果+影响分析 → AI 解释(数字校验) → 比较/保存/回测/监控`。

AI 不可用（无 Key / 超时 / 输出不合法）时：自动降级为 **规则词典解析**（`ai/fallback.py`），界面明确标注“AI 不可用，已用规则解析”，主链路仍可跑通。

## 2. 仓库结构

```
nl-stock-screener/
  backend/
    pyproject.toml
    app/
      main.py              # FastAPI 应用、静态文件、启动预热、监控调度
      config.py            # 环境变量（pydantic-settings）
      schemas.py           # 【契约】全部 pydantic 模型
      catalog.py           # 【契约】指标目录、股票池预设、词典模板、算子描述
      data/
        cache.py           # SQLite 响应缓存
        fuyao.py           # 扶摇 REST 客户端：重试/限流/缓存/来源记录/故障注入
        ifind.py           # iFinD MCP JSON-RPC 客户端
        periods.py         # 报告期规则、日期工具
        repository.py      # 领域数据访问：股票池、行业映射、批量取数、数据集状态
        features.py        # 指标计算（含证据链），纯函数
      engine/
        conditions.py      # 条件描述文本、规范化、区间工具
        conflicts.py       # 确定性冲突检测
        evaluate.py        # 判定、分桶、漏斗、统计
        sensitivity.py     # 去条件影响、阈值扫描、差一点入选
        diff.py            # 两次结果 diff
        backtest.py        # 回测（时点一致）
      ai/
        llm.py             # OpenAI 兼容客户端（JSON 输出、超时、重试）
        prompts.py         # 提示词
        parser.py          # NL → 策略（LLM + 校验 + 降级）
        fallback.py        # 规则词典解析
        explain.py         # 解释生成 + 数字校验 + 模板解释
        compliance.py      # 输入/输出合规守卫
      services/
        store.py           # SQLite：保存的策略、监控、运行摘要
        runs.py            # 筛选任务编排、快照复用、what-if
        monitor.py         # 监控调度与 diff
        backtests.py       # 回测任务编排
      api/
        routes.py          # 全部 REST 路由
    tests/                 # pytest；只用合成数据（不提交任何真实供应商数据）
  frontend/                # Vite + React 18 + TypeScript + Ant Design 5 + ECharts
  docs/                    # DESIGN / DATA_SOURCES / TESTING / AI_USAGE
  scripts/                 # 数据口径探测脚本（需本地 .env）
  Dockerfile, render.yaml, .github/workflows/
```

## 3. 配置（`app/config.py`）

`Settings`（pydantic-settings，读 `.env` 与环境变量）：
`fuyao_api_key, fuyao_base_url, ifind_mcp_token, ifind_mcp_url, llm_api_key, llm_base_url, llm_model,
data_dir="./data", fuyao_max_concurrency=6, fuyao_rps=8.0, fuyao_timeout_s=20, llm_timeout_s=40,
demo_faults_enabled=True, warm_universes="000300.SH", max_deep_eval=800, app_env="dev"`。
`get_settings()` 带 lru_cache。所有模块从这里取配置，禁止散落 `os.environ`。

## 4. 数据层

### 4.1 `data/cache.py`
```python
class ApiCache:
    def __init__(self, db_path: str): ...
    def get(self, key: str) -> CachedResponse | None      # 过期返回 None
    def put(self, key: str, resp: CachedResponse, ttl_s: int) -> None
    def clear(self) -> None
```
`CachedResponse = {payload: dict, request_id, data_timestamp, fetched_at}`。key = `path + 排序后的 params`。

### 4.2 `data/fuyao.py`
```python
class FuyaoError(Exception):
    code: int | None; message: str; request_id: str | None; kind: FuyaoErrorKind; endpoint: str
class FuyaoResponse(BaseModel):
    endpoint: str; params: dict; data: Any; request_id: str | None
    data_timestamp: int | None; fetched_at: datetime; cache_hit: bool
    def source(self, field: str | None = None) -> SourceRef
class FuyaoClient:
    def __init__(self, settings, cache: ApiCache | None, fault_injector: FaultInjector | None = None)
    async def get(self, path: str, params: dict, ttl_s: int, dataset: str) -> FuyaoResponse
    async def aclose(self)
```
- 头：`X-api-key`。信封 `{code,message,request_id,data}`；`code!=0` → `FuyaoError`。
- 错误码映射（`FuyaoErrorKind`）：2001 auth、2003 permission、1001/1002/1003/1004 bad_request、3001 not_found、3002 not_ready、3004 unsupported、4001 或 HTTP429 rate_limited、5001 internal、5002 upstream_timeout、5003 upstream_unavailable；网络异常 network；非 JSON/缺字段 invalid_response；无 Key → auth（message="未配置 FUYAO_API_KEY"）。
- 重试：rate_limited 退避 1s/2s/4s（含抖动）最多 3 次；upstream_timeout/upstream_unavailable/network/internal 最多 2 次；其他不重试。
- 并发：全局 `asyncio.Semaphore(fuyao_max_concurrency)` + 令牌桶 `fuyao_rps`。
- TTL（秒）：snapshot 60；valuations 600；indicators/income 86400；history 21600；constituents/industry/tickers/calendar 86400；dividends 86400。
- **故障注入** `FaultInjector(datasets: set[str], kind="upstream_unavailable")`：命中 dataset 时直接抛对应 `FuyaoError`（code 5003, message="【演示故障注入】..."），且不写缓存。仅当 `demo_faults_enabled` 时由请求参数 `simulate_failures` 启用，作用域为单次运行。

### 4.3 `data/ifind.py`
iFinD MCP：`POST {IFIND_MCP_URL}`，头 `Authorization: <token>`、`Content-Type: application/json`、`Accept: application/json, text/event-stream`；JSON-RPC 2.0：先 `initialize`（若服务端要求），`tools/list`，`tools/call {name, arguments}`。响应可能是 JSON 或 SSE（`data: {...}` 行），两种都要解析。
```python
class IfindClient:
    enabled: bool
    async def list_tools(self) -> list[dict]
    async def call(self, name: str, arguments: dict) -> dict      # 返回 result（content 列表等）
async def get_stock_news(client, thscode: str, name: str | None, limit: int = 8) -> NewsResult
```
资讯只作背景，`NewsResult.note` 固定说明“不参与筛选与打分”。工具名以 `tools/list` 实际结果为准（探测后写入 `docs/DATA_SOURCES.md`）。

### 4.4 `data/periods.py`
- `uniform_report_period(today: date) -> ReportPeriod`：按法定披露截止日确定“全部公司都应已披露”的最近报告期：
  一季报截止 4/30、中报 8/31、三季报 10/31、年报次年 4/30。例：2026-10-17 → `2026-2`（中报）；2026-11-05 → `2026-3`；2027-03-01 → `2026-3`；2027-05-01 → `2026-4`… 注意年报与一季报截止同为 4/30：5/1 起最近期为当年 `yyyy-1`（一季报，晚于上年年报）。
- `report_label("2026-2") -> "2026年中报"`；`yoy_report("2026-2") -> "2025-2"`；`quarters_in_report("2026-2") -> 2`。
- 报告期可由用户在 `Strategy.report_period` 指定（如 `2026-3`），此时覆盖率不足会产生大量 missing——这是预期的、要如实展示。
- 时间统一 `Asia/Shanghai`；工具函数：`ms_to_date`, `date_to_ms`, `now_cn()`。

### 4.5 `data/repository.py`（领域数据访问）
```python
class Repository:
    def __init__(self, client: FuyaoClient, settings): ...
    async def ticker_table(self) -> dict[str, TickerRow]                 # /api/meta/tickers/list asset_type=a-share（分页取尽）
    async def resolve_universe(self, u: Universe) -> ResolvedUniverse     # index/industry/concept → 成分股；all_a → 代码表；custom → 校验
    async def search_universes(self, q: str) -> list[UniverseOption]      # 预设指数 + THS 行业/概念目录模糊匹配
    async def industry_map(self) -> dict[str, tuple[str, str]]            # thscode → (industry_code, industry_name)；THS 行业指数成分反查
    async def valuations(self, codes, *, faults) -> BatchResult           # 每批 100
    async def snapshots(self, codes, *, faults) -> BatchResult            # 每批 100
    async def indicators(self, code, report, *, faults) -> FuyaoResponse  # /api/a-share/financials/indicators
    async def income_quarterly(self, code, limit=12, *, faults) -> FuyaoResponse
    async def history(self, code, start, end, adjust="backward", *, faults) -> FuyaoResponse
    async def index_history(self, code, start, end) -> FuyaoResponse
    async def dividends(self, code, from_date, *, faults) -> FuyaoResponse
    async def build_snapshot(self, strategy: Strategy, *, faults: set[str], progress: Callable[[float, str], None]) -> FeatureSnapshot
```
- **按依赖取数**：由策略启用条件涉及的指标 + 展示用核心指标（`catalog.DISPLAY_METRICS`）求并集，经 `catalog.METRICS[m].datasets` 得到需要的数据集，只取这些。
- 每个数据集汇总一个 `DatasetStatus`（请求数/成功数/失败数/最大 data_timestamp/错误样例）。部分失败 → `partial`，全部失败 → `failed`。
- `all_a` 股票池：先只取“廉价数据集”（snapshot、valuations、tickers），由引擎对廉价条件预筛；若剩余 > `max_deep_eval`，运行以 `failed` 结束并给出明确提示，不截断。
- 行业映射：拉 `ths-index-list?tag=industry`，逐个取成分；同一股票命中多级行业时选择 **成员数 ≥ 8 的最细一级**（探测后可调整，见 DATA_SOURCES）。参考组（行业/全市场分位）只对“全市场可廉价获得”的指标开放（估值、行情）。

### 4.6 `data/features.py`（纯函数，带证据）
入口：`compute_features(raw: RawBundle, metrics: set[str], report: ReportPeriod, as_of: date) -> dict[str, StockFeatures]`。
每个指标产出 `MetricValue(status, value, evidence, reason)`。规则：
- 原始字段为 null → `missing`，reason=“接口返回空值（该期未披露或不适用）”；数据集请求失败 → `error`，reason 含错误码与 request_id；
- 指标对该股不适用（如银行无毛利率、PE<0 时的 PE 分位）→ `not_applicable` + 原因；
- 派生指标的 `Evidence.derived=True`，`formula` 写清公式，`inputs` 挂上原始证据；
- 价格类窗口：使用后复权日线（`adjust=backward`），窗口 = 截至最近一个交易日的 N 个交易日；样本不足（<N×0.8）→ `missing`。
公式（单位见 catalog）：
- `volatility_250d = std(ln(P_t/P_{t-1})) × √252 × 100`
- `max_drawdown_250d = max_t (1 − P_t / max_{s≤t} P_s) × 100`（正数，越小越稳）
- `return_Nd = (P_last / P_{last−N} − 1) × 100`
- `beta_250d = cov(r_i, r_m) / var(r_m)`，`r_m` 为沪深300（000300.SH）日收益，按日期对齐
- `avg_amount_20d = mean(turnover 近20日) / 1e8`（亿元）
- `roe_change = roe(本期) − roe(上年同期)`（百分点）；`gross_margin_change` 同理
- `roe_annualized = roe(本期) × 4 / 报告期季度数`（估算，标注“年化估算”）
- `revenue_ttm = 本期累计 + 上年年报 − 上年同期累计`（利润表季报为累计值，需探测确认）；`market_cap_est = ps_ttm × revenue_ttm / 1e8`（亿元，估算）
- `dividend_yield_ttm = 近365天每股现金分红合计(税前) / 最新价 × 100`
- `is_st`：名称含 “ST”；`listing_days = 今日 − list_date`

## 5. 引擎

### 5.1 `engine/conditions.py`
`describe(cond, metric_def) -> str`（如“市盈率TTM 处于所属行业后 50%”“营业收入同比增长率 > 0%”）；`interval(cond) -> (lo, lo_incl, hi, hi_incl)`；`normalize(strategy) -> Strategy`（补 id、补 label、去重）；`DEFAULT_SYSTEM_CONDITIONS`（`is_st is_false`、`listing_days gte 365`，origin="system"，source_phrase="默认风控"，可删除）。

### 5.2 `engine/conflicts.py`
`detect_conflicts(strategy) -> list[Conflict]`（纯确定性）：
1. 同指标同口径区间求交为空 → `error/contradiction`（阻止执行）；2. 区间包含 → `info/redundant`；
3. 阈值超出 `plausible_range` → `warning/implausible`；4. 算子不被指标允许、分位值不在 [0,100]、未知指标 → `error/unsupported_metric`；
5. 张力规则表（高成长+低估值、低波动+强动量、小市值+大盘指数池、负债率条件+金融行业池、毛利率条件+银行池）→ `warning/tension|universe_mismatch`；
6. 无任何启用的硬条件 → `warning`。
运行后由 `evaluate` 追加 `data_coverage`（某条件缺失率 > 20%）。

### 5.3 `engine/evaluate.py`
```python
def evaluate(strategy: Strategy, snap: FeatureSnapshot, *, compute_stats: bool = True) -> EvaluationOutput
```
- 逐股逐条件：指标 status≠ok → 条件 `missing`（missing/error）或 `not_applicable`；ok → 按算子判定；记录 `margin`（相对距离：`(v−t)/|t|`，方向使“通过为正”；t=0 时用绝对差）。
- 分位算子：参考组 = universe（快照内同指标 ok 的股票）/ industry（`snap.reference` 中同行业）/ market（`snap.reference` 全部）；分位 = 平均秩法，0=最小，100=最大；`positive_only_for_rank` 的指标只用正值排名，非正值 → `not_applicable`；参考组 < 5 → `not_applicable`。
- 分桶：任一硬条件 fail → `excluded`；否则任一硬条件 missing/na → `missing_policy=exclude` 时进 `missing` 桶，`keep_flagged` 时进 `included` 且带标记；否则 `included`。软条件只计分：`soft_score = Σw·pass / Σw`。
- 排序：included 按 soft_score 降序 → `sort_metric` → thscode；excluded/missing 按 thscode。
- 漏斗：按条件顺序依次过滤，记录每步剩余与（fail/缺失）移除数；注明“顺序不影响最终结果”。
- `ConditionStat`：各状态计数、`sole_blocker`、`drop_effect`（精确重算）、`sweep`（数值算子用 catalog.sweep_values ∪ 当前值附近 ×0.5/×0.75/×1.25/×1.5；分位算子用 20..80）。
- 差一点入选：只差 1 个硬条件且 |margin| ≤ 0.2（分位差 ≤ 10 个点）→ `near_miss=True`。
- 确定性：输入相同 → 输出相同；`strategy_hash = sha256(规范化 JSON)`。

### 5.4 `engine/sensitivity.py` / `engine/diff.py`
`threshold_sweep(strategy, snap, cond_id, values) -> list[SweepPoint]`；`drop_effect(...)`；
`diff_results(base: EvaluationOutput, new: EvaluationOutput, base_strategy, new_strategy) -> ResultDiff`：新增/移除股票，每只给出“哪个条件状态变了、数值 vs 新旧阈值”的原因文本；`changes_text` 列出条件层面的变化。

### 5.5 `engine/backtest.py`（时点一致）
```python
def run_backtest(req: BacktestRequest, data: BacktestData) -> BacktestResult
```
- 调仓日：每月/每季首个交易日（交易日序列取自基准指数历史 K 线日期）；在调仓日用 **前一交易日收盘及以前** 的数据判定条件；财务数据只用 `report_date_ms ≤ 调仓日` 的报告（防前视）。
- 支持指标：价格类（波动率、回撤、区间收益、成交额、beta）、成长类（由利润表季报自算同比，标注“回测自算口径”）、`listing_days`、`is_st`（按当前名称，注明偏差）；
  估值类：P1 可选，用 `不复权收盘价 / 滚动四季基本每股收益` 近似重建 PE，行业分位改为股票池内分位，并在 `assumptions` 中写明；做不到的条件进 `ignored_conditions`（带原因），绝不静默丢弃。
- 持仓：等权；超过 `top_n` 时按 `sort_metric`/soft_score 取前 N；无入选则空仓并记录。
- 成本：每边 `cost_bps`；统计：总收益、年化、年化波动、最大回撤、夏普（无风险利率 0，注明）、超额、平均持仓数、换手率；基准同口径。
- `caveats` 固定包含：幸存者偏差（成分股为当前时点）、历史不代表未来、未模拟涨跌停/停牌无法成交、成本假设。

## 6. AI 层

### 6.1 `ai/llm.py`
`class LLMClient: enabled; async def chat_json(system: str, user: str, *, temperature=0.0, max_tokens=2000) -> dict`
OpenAI 兼容 `/chat/completions`，`response_format={"type":"json_object"}`；超时 `llm_timeout_s`；JSON 解析失败重试 1 次（附错误提示）；抛 `LLMError(kind)`。

### 6.2 `ai/parser.py`
```python
async def parse_query(req: ParseRequest, llm: LLMClient, repo: Repository | None) -> ParseResult
```
1. `compliance.check_input(query)`：预测/荐股/收益承诺 → 返回 `intent=advice_request|prediction_request` 与 `ComplianceNotice`；能转化为可观察条件的（如“会涨的股票”→“近 60 日趋势向上（仅描述历史）”）给出转化后的草案并标注。
2. LLM（system 含：规则、指标目录精简表、股票池预设、算子说明、输出 JSON schema、少样本）→ JSON：
   `{intent, universe:{kind,code?,name?,query?}, phrases:[{phrase, meaning, confidence, conditions:[...], alternatives:[{label, description, conditions:[...]}]}], unsupported:[{phrase, reason, alternative}], conflicts:[{condition_refs, message}], patch?:{...}}`
3. 后端校验：未知指标/非法算子 → 丢弃并记 warning；阈值越界 → 保留并由冲突检测提示；条件补 id、origin="ai"、source_phrase；
   有 alternatives 或 confidence≠high 的短语 → 生成 `Clarification`（默认项=AI 首选）；
   universe.query（如“白酒”）→ `repo.search_universes` 解析为具体代码，多个候选 → 生成股票池澄清问题。
4. 追加系统默认条件（ST/次新）；`detect_conflicts`；AI 冲突（必须引用存在的条件）合并为 `source="ai"`。
5. 修改模式（`base_strategy` 非空）：LLM 输出补丁 `{add:[...], remove:[cond_id], update:[{id, value?, value2?, op?, enabled?, hard?}], universe?:...}`，后端应用并生成 `changes` 文本；用户在前端确认后才执行。
6. LLM 不可用/失败 → `fallback.parse(query, base_strategy)`，`parser="rule_fallback"`，`llm_error` 写明原因。

### 6.3 `ai/fallback.py`
基于 `catalog.LEXICON`（正则 → 解释模板，含备选项）逐短语匹配；股票池关键词（沪深300/中证500/创业板/科创50/“白酒”等）匹配预设或交给 `search_universes`；
修改语句（“放宽/收紧/去掉/只看”）做有限支持。必须能处理题面示例与 TESTING 中列出的用例。

### 6.4 `ai/explain.py`
- `template_stock_explanation(stock: StockResult, strategy, snap) -> Explanation`：逐条件生成“事实”句（数值+阈值+报告期/窗口+来源字段）；缺失/不适用 → “不确定”句；分位类 → “推断”句（如“估值在行业中偏低”）；末尾 caveat 句。
- `template_run_summary(result) -> Explanation`：入选数/股票池规模/主要卡点（drop_effect 最大的条件）/缺失情况/数据时点。
- `async llm_run_summary(...)`、`async llm_stock_explanation(...)`：只把结构化证据交给 LLM，要求输出 `{sentences:[{text, kind, refs}]}`；
  **数字校验**：抽取文本中的数字，必须能在证据数值/阈值/计数中找到（容差：绝对 0.05 或相对 0.5%）；任一不符或命中合规禁语 → 丢弃 LLM 结果，用模板，`verification_notes` 记录原因。

### 6.5 `ai/compliance.py`
- `check_input(text) -> ComplianceNotice | None`：类别 prediction（明天/下周会涨跌、能涨到、预测股价、目标价）、advice（推荐买、该不该买/卖、满仓、抄底建议）、return_promise（稳赚、保本、必涨、翻倍、无风险）。
- `sanitize_output(text) -> (clean_text, violations)`：禁语（建议买入/卖出、必涨、稳赚、目标价、保证收益、预计上涨xx%、强烈推荐…）→ 替换为中性表述或删除句子；返回违规列表用于日志与测试。
- `DISCLAIMER` 常量：所有结果页、回测页展示。

## 7. 服务与 API

### 7.1 `services/runs.py`
`RunManager`：`start(req: ScreenRequest) -> run_id`（后台任务）、`get(run_id) -> ScreenResult`、`whatif(run_id, strategy) -> WhatIfResult`（复用同一 `FeatureSnapshot`，缺指标则增量取数）、`explain_stock(run_id, thscode) -> Explanation`。
快照复用：key=(股票池, 数据集集合, 报告期, 交易日)，10 分钟内复用；`force_refresh` 跳过。运行状态与进度（0~1 + 文本）供前端轮询。

### 7.2 `services/store.py` / `monitor.py` / `backtests.py`
SQLite（`{data_dir}/app.sqlite`）：`strategies`、`monitors`、`monitor_runs`、`run_summaries`。
监控：`frequency=daily|weekly`；后台循环每 10 分钟检查到期任务（启动时补跑过期任务）；每次运行与上次结果做 diff；数据集 partial/failed 时生成 `data_alerts`，绝不报“无变化”。

### 7.3 REST 路由（`api/routes.py`，前缀 `/api`）
| 方法 | 路径 | 入参 → 出参 |
|---|---|---|
| GET | `/health` | → `HealthStatus` |
| GET | `/catalog` | → `CatalogResponse`（指标、算子、股票池预设、免责声明） |
| GET | `/universes/search?q=` | → `list[UniverseOption]` |
| POST | `/parse` | `ParseRequest` → `ParseResult` |
| POST | `/strategy/validate` | `Strategy` → `ValidateResult` |
| POST | `/screen` | `ScreenRequest` → `{run_id}` |
| GET | `/screen/{run_id}` | → `ScreenResult`（含 status/progress） |
| POST | `/screen/{run_id}/whatif` | `Strategy` → `WhatIfResult` |
| GET | `/screen/{run_id}/stocks/{thscode}` | → `StockDetail` |
| POST | `/screen/{run_id}/explain/{thscode}` | → `Explanation` |
| GET | `/stocks/{thscode}/news` | → `NewsResult` |
| POST | `/compare` | `CompareRequest` → `CompareResult` |
| POST | `/backtest` | `BacktestRequest` → `{backtest_id}`；GET `/backtest/{id}` → `BacktestResult` |
| GET/POST/DELETE | `/strategies`、`/strategies/{id}` | `SavedStrategy` |
| GET/POST/DELETE | `/monitors`、`/monitors/{id}`；POST `/monitors/{id}/run` | `Monitor` |

错误统一：HTTP 4xx/5xx + `{detail: str}`；业务性的数据缺失不是 HTTP 错误，而是结果里的显式状态。

## 8. 前端（`frontend/`）

Vite + React 18 + TS + Ant Design 5（zh_CN）+ ECharts（echarts-for-react）+ zustand。开发代理 `/api → http://localhost:8000`。
页面：
1. **选股工作台**（`/`）
   - 顶栏：产品名、数据源状态灯（扶摇 / iFinD / AI，鼠标悬停看时点与错误）、免责声明。
   - 输入区：自然语言输入 + 示例（含题面示例）+「解析」。
   - **我的理解**：原句中各短语 → 映射条件（chip）；澄清卡片（单选，默认项标“AI 默认”）；不支持的短语与替代建议；合规提示。
   - **条件编辑器**：表格行=条件（指标下拉[按类别]、算子、阈值+单位、分位口径、硬/软、启用、来源标签 ai/rule/user/system、理由）；股票池选择（预设 + 搜索行业/概念）；缺失策略；报告期；任何修改实时调用 `/strategy/validate` 刷新冲突；error 冲突禁用“执行”。
   - **一句话修改**：输入“估值放宽到行业 70% 分位、去掉回撤条件”→ `/parse`（带 base_strategy）→ 展示变更 diff，用户「应用」后再执行。
   - **结果**（Tabs）：入选（表格：代码/名称/行业/每个条件的值+✓✗?/软分；勾选→比较）、排除（失败条件筛选、差一点入选标记）、数据缺失（原因）、**条件影响**（漏斗图、每条件 pass/fail/missing 柱、单独卡点、去掉该条件后的入选数、阈值扫描折线、差一点入选列表、阈值滑块 → what-if → “新增 N / 移除 M”diff 横幅）、**数据口径与时点**（数据集表：接口、状态、覆盖、data_timestamp、获取时间、错误样例；指标口径表）。
   - 摘要卡片：AI/模板摘要，逐句带“事实/推断/不确定/提示”标签；“已通过数字校验”徽标。
   - **个股抽屉**：逐条件证据（数值、阈值、报告期/窗口、接口+字段+request_id+时点、公式与输入）、后复权价格走势、iFinD 资讯（标注“仅背景，不参与打分”）、解释文本。
   - 操作：比较（2–5 只：指标并排 + 归一化走势）、保存策略、回测（参数 → 净值 vs 基准、统计、忽略条件、假设与警示）、转为监控。
   - 演示开关（仅 demo_faults_enabled）：“模拟估值接口故障”等，用于展示异常处理。
2. **我的策略**（`/library`）：已保存策略（加载/运行/删除/导出 JSON）、监控任务（立即运行、历史运行 diff、数据告警）。

## 9. 合规与可追溯（贯穿）
- 任何结论回到 `Evidence`：数值、单位、期间/窗口、来源接口与字段、request_id、data_timestamp、fetched_at、派生公式。
- 缺失、冲突、过期、调用失败：显式状态 + 原因，绝不显示为“通过/正常”。
- 不输出确定性涨跌预测、收益承诺、买卖建议；解释区分 事实 / 推断 / 不确定；回测标注历史局限。
- 不提交 API Key 与任何真实供应商数据；测试只用合成数据；用户画像/持仓不涉及。

## 10. 实现优先级
P0：主链路（解析→澄清→条件编辑→冲突→执行→入选/排除/缺失依据→口径时点→条件影响/what-if）、异常处理、合规守卫、测试。
P1：比较、保存、监控、回测（价格+成长）、iFinD 资讯、LLM 摘要。
P2：回测估值重建、策略导出导入、本地镜像。
