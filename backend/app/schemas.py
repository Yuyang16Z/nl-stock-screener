"""数据契约：前后端与各模块之间唯一的结构定义。

约定：
- 只允许追加可选字段，不改既有字段语义。
- 时间：`*_ms` 为 Asia/Shanghai 毫秒时间戳；datetime 一律带时区。
- 百分比类数值统一用“百分数值”（12.3 表示 12.3%），单位写在 Evidence.unit / MetricDef.unit。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ───────────────────────────── 基础枚举 ─────────────────────────────

Op = Literal[
    "gt",  # >
    "gte",  # ≥
    "lt",  # <
    "lte",  # ≤
    "between",  # value ≤ x ≤ value2
    "pct_rank_lte",  # 参考组内分位 ≤ value（0=最小，100=最大）
    "pct_rank_gte",  # 参考组内分位 ≥ value
    "is_true",
    "is_false",
]
RankScope = Literal["universe", "industry", "market"]
ConditionOrigin = Literal["ai", "rule", "user", "system"]
MetricStatus = Literal["ok", "missing", "not_applicable", "error"]
CondStatus = Literal["pass", "fail", "missing", "not_applicable", "skipped"]
Bucket = Literal["included", "excluded", "missing"]
UniverseKind = Literal["index", "industry", "concept", "all_a", "custom"]
JobStatus = Literal["pending", "running", "done", "failed"]
SentenceKind = Literal["fact", "inference", "uncertain", "caveat"]


# ───────────────────────────── 来源与证据 ─────────────────────────────


class SourceRef(BaseModel):
    """一条数据的出处：可回到原始接口与字段。"""

    provider: Literal["fuyao", "ifind", "derived", "user", "system"]
    endpoint: str | None = None  # 如 /api/a-share/valuations/snapshot
    params: dict[str, Any] = Field(default_factory=dict)
    field: str | None = None  # 原始字段名，如 pe_ttm
    request_id: str | None = None
    data_timestamp: int | None = None  # 信封 data.timestamp 或记录自身时间（ms）
    fetched_at: datetime | None = None
    cache_hit: bool = False


class Evidence(BaseModel):
    metric: str
    value: float | bool | str | None
    unit: str  # "%", "倍", "元", "亿元", "天", "百分点", ""
    display: str  # 已格式化的展示值，如 "12.30%"
    period: str | None = None  # "2026年中报（2026-2）" / "近250个交易日（截至2026-10-16）" / "实时快照"
    as_of: datetime | None = None  # 数据有效时点
    sources: list[SourceRef] = Field(default_factory=list)
    derived: bool = False
    formula: str | None = None
    inputs: list[Evidence] = Field(default_factory=list)
    note: str | None = None  # 口径提示，如“年化估算”“回测自算口径”


class MetricValue(BaseModel):
    metric: str
    status: MetricStatus
    value: float | bool | None = None
    evidence: Evidence | None = None
    reason: str | None = None  # missing / not_applicable / error 的原因（含错误码、request_id）
    stale: bool = False  # 数据时点早于预期（如快照非最近交易日）


# ───────────────────────────── 股票池与条件 ─────────────────────────────


class Universe(BaseModel):
    kind: UniverseKind = "index"
    code: str | None = "000300.SH"  # index/industry/concept 的 thscode
    name: str | None = "沪深300"
    codes: list[str] = Field(default_factory=list)  # custom 时的成分
    query: str | None = None  # 未解析的自然语言股票池描述（如“白酒”），由后端解析

    @model_validator(mode="after")
    def _drop_index_defaults(self) -> "Universe":
        """全A/自定义股票池没有指数代码；不让 code/name 的“沪深300”默认值冒充股票池名称。"""
        if self.kind in ("all_a", "custom"):
            self.code = None
            if self.name in (None, "", "沪深300") and ("name" not in self.model_fields_set or self.name == "沪深300"):
                self.name = None
        return self


class UniverseOption(BaseModel):
    kind: UniverseKind
    code: str | None
    name: str
    group: str  # "常用指数" / "同花顺行业" / "同花顺概念" / "全市场"
    size_hint: int | None = None


class ResolvedUniverse(BaseModel):
    kind: UniverseKind
    code: str | None
    name: str
    size: int
    members: list[str]
    source: SourceRef | None = None
    as_of: datetime | None = None
    note: str | None = None  # 如“成分股为当前时点”


class Condition(BaseModel):
    id: str = ""  # 后端规范化时补 c1, c2...
    metric: str
    op: Op
    value: float | None = None
    value2: float | None = None  # between 的上界
    rank_scope: RankScope | None = None  # 分位算子必填
    hard: bool = True  # 硬条件=过滤；软条件=只计分
    weight: float = 1.0
    enabled: bool = True
    origin: ConditionOrigin = "user"
    source_phrase: str | None = None  # 来自原句的哪个短语
    rationale: str | None = None  # 为什么这样解释
    label: str | None = None  # 后端生成的可读描述


class ReportPeriod(BaseModel):
    report: str  # "2026-2"
    label: str  # "2026年中报"
    yoy_report: str  # "2025-2"
    quarters: int  # 1..4
    rule: str  # 如何确定的（法定截止日规则 / 用户指定）


class Strategy(BaseModel):
    name: str | None = None
    query: str | None = None
    universe: Universe = Field(default_factory=Universe)
    conditions: list[Condition] = Field(default_factory=list)
    missing_policy: Literal["exclude", "keep_flagged"] = "exclude"
    report_period: str | None = None  # None=自动（法定截止日规则）
    sort_metric: str | None = None
    sort_desc: bool = True


# ───────────────────────────── 解析（AI / 规则） ─────────────────────────────


class Interpretation(BaseModel):
    phrase: str
    meaning: str
    condition_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    origin: Literal["ai", "rule"] = "ai"


class ClarificationOption(BaseModel):
    id: str
    label: str
    description: str | None = None
    conditions: list[Condition] = Field(default_factory=list)  # 选中后替换该短语对应的条件
    universe: Universe | None = None  # 股票池类澄清


class Clarification(BaseModel):
    id: str
    phrase: str
    question: str
    options: list[ClarificationOption]
    default_option_id: str
    selected_option_id: str | None = None


class Conflict(BaseModel):
    id: str
    severity: Literal["error", "warning", "info"]
    kind: Literal[
        "contradiction",
        "redundant",
        "tension",
        "universe_mismatch",
        "implausible",
        "unsupported_metric",
        "data_coverage",
        "empty_hard",
    ]
    condition_ids: list[str] = Field(default_factory=list)
    message: str
    suggestion: str | None = None
    source: Literal["rule", "ai"] = "rule"


class Unsupported(BaseModel):
    phrase: str
    reason: str
    alternative: str | None = None


class ComplianceNotice(BaseModel):
    level: Literal["block", "warn"]
    category: Literal["prediction", "advice", "return_promise", "other"]
    message: str
    matched: list[str] = Field(default_factory=list)


class ParseRequest(BaseModel):
    query: str
    base_strategy: Strategy | None = None  # 非空 = 自然语言修改既有条件


class ParseResult(BaseModel):
    intent: Literal["screen", "modify", "advice_request", "prediction_request", "out_of_scope"]
    strategy: Strategy | None = None
    interpretations: list[Interpretation] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    unsupported: list[Unsupported] = Field(default_factory=list)
    compliance: ComplianceNotice | None = None
    changes: list[str] = Field(default_factory=list)  # 修改模式下的变更说明
    parser: Literal["llm", "rule_fallback"] = "rule_fallback"
    llm_model: str | None = None
    llm_error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ValidateResult(BaseModel):
    strategy: Strategy  # 规范化后（补 id/label）
    conflicts: list[Conflict]
    can_run: bool  # 无 error 级冲突
    warnings: list[str] = Field(default_factory=list)


# ───────────────────────────── 数据集与快照 ─────────────────────────────


class DatasetStatus(BaseModel):
    dataset: str  # tickers / constituents / industry_map / snapshot / valuations / indicators / indicators_yoy / income / history / benchmark / dividends
    label: str
    endpoint: str
    status: Literal["ok", "partial", "failed", "skipped"]
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    as_of: datetime | None = None  # 成功响应中最大的 data_timestamp
    fetched_at: datetime | None = None
    errors: list[str] = Field(default_factory=list)  # 错误样例（含 code/request_id），最多 5 条
    note: str | None = None


class TickerRow(BaseModel):
    thscode: str
    ticker: str
    name: str | None = None
    exchange: str | None = None
    list_date: str | None = None


class StockInfo(BaseModel):
    thscode: str
    ticker: str
    name: str | None = None
    industry: str | None = None
    industry_code: str | None = None
    list_date: str | None = None


class StockFeatures(BaseModel):
    info: StockInfo
    metrics: dict[str, MetricValue] = Field(default_factory=dict)


class FeatureSnapshot(BaseModel):
    snapshot_id: str
    universe: ResolvedUniverse
    stocks: dict[str, StockFeatures]
    # 行业/全市场分位的参考组：metric -> {thscode: value}（只含全市场可廉价获取的指标）
    reference: dict[str, dict[str, float]] = Field(default_factory=dict)
    reference_industry: dict[str, str] = Field(default_factory=dict)  # thscode -> industry_code
    industry_names: dict[str, str] = Field(default_factory=dict)  # industry_code -> name
    datasets: list[DatasetStatus] = Field(default_factory=list)
    report_period: ReportPeriod
    trade_date: str | None = None  # 价格窗口截止交易日 yyyy-MM-dd
    metrics_available: list[str] = Field(default_factory=list)
    prefiltered: bool = False  # all_a 预筛模式
    created_at: datetime


# ───────────────────────────── 判定结果 ─────────────────────────────


class CondEval(BaseModel):
    condition_id: str
    status: CondStatus
    value: float | bool | None = None
    display: str = "--"
    threshold: str = ""  # "> 0%"、"行业分位 ≤ 50%"
    rank_pct: float | None = None
    rank_group_size: int | None = None
    margin: float | None = None  # 通过为正、未通过为负的相对距离
    reason: str | None = None
    evidence: Evidence | None = None


class StockResult(BaseModel):
    thscode: str
    name: str | None = None
    industry: str | None = None
    bucket: Bucket
    failed_conditions: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    soft_score: float | None = None
    evaluations: list[CondEval] = Field(default_factory=list)
    near_miss: bool = False
    flagged: bool = False  # keep_flagged 策略下带缺失入选


class FunnelStep(BaseModel):
    condition_id: str | None  # None = 股票池
    label: str
    remaining: int
    removed_fail: int = 0
    removed_missing: int = 0


class SweepPoint(BaseModel):
    value: float
    included: int
    is_current: bool = False


class ConditionStat(BaseModel):
    condition_id: str
    label: str
    hard: bool
    passed: int = 0
    failed: int = 0
    missing: int = 0
    not_applicable: int = 0
    skipped: int = 0
    sole_blocker: int = 0  # 只因此条件被排除的股票数
    drop_effect: int | None = None  # 去掉该条件后的入选数（精确重算；无法精确时为 None）
    sweep: list[SweepPoint] = Field(default_factory=list)
    note: str | None = None


class EvaluationOutput(BaseModel):
    included: list[StockResult]
    excluded: list[StockResult]
    missing: list[StockResult]
    funnel: list[FunnelStep]
    condition_stats: list[ConditionStat]
    conflicts: list[Conflict] = Field(default_factory=list)  # data_coverage 等运行期冲突
    strategy_hash: str


class ExplanationSentence(BaseModel):
    text: str
    kind: SentenceKind
    refs: list[str] = Field(default_factory=list)  # 条件 id / 指标 id


class Explanation(BaseModel):
    sentences: list[ExplanationSentence]
    generator: Literal["llm", "template"]
    verified: bool = True  # 数字已与证据核对
    verification_notes: list[str] = Field(default_factory=list)


class ScreenRequest(BaseModel):
    strategy: Strategy
    simulate_failures: list[str] = Field(default_factory=list)  # 演示：让指定数据集失败
    force_refresh: bool = False


class ScreenResult(BaseModel):
    run_id: str
    status: JobStatus
    progress: float = 0.0
    progress_text: str | None = None
    strategy: Strategy
    strategy_hash: str | None = None
    universe: ResolvedUniverse | None = None
    report_period: ReportPeriod | None = None
    trade_date: str | None = None
    datasets: list[DatasetStatus] = Field(default_factory=list)
    funnel: list[FunnelStep] = Field(default_factory=list)
    included: list[StockResult] = Field(default_factory=list)
    excluded: list[StockResult] = Field(default_factory=list)
    missing: list[StockResult] = Field(default_factory=list)
    condition_stats: list[ConditionStat] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    summary: Explanation | None = None
    warnings: list[str] = Field(default_factory=list)
    simulated_failures: list[str] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class DiffItem(BaseModel):
    thscode: str
    name: str | None = None
    reason: str


class ResultDiff(BaseModel):
    base_run_id: str | None = None
    new_run_id: str | None = None
    added: list[DiffItem] = Field(default_factory=list)
    removed: list[DiffItem] = Field(default_factory=list)
    unchanged: int = 0
    changes_text: list[str] = Field(default_factory=list)


class WhatIfResult(BaseModel):
    result: ScreenResult
    diff: ResultDiff


# ───────────────────────────── 个股 / 比较 / 资讯 ─────────────────────────────


class PricePoint(BaseModel):
    date: str  # yyyy-MM-dd
    close: float


class StockDetail(BaseModel):
    info: StockInfo
    bucket: Bucket | None = None
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    evaluations: list[CondEval] = Field(default_factory=list)
    prices: list[PricePoint] = Field(default_factory=list)
    price_source: SourceRef | None = None
    price_note: str | None = None  # 如“后复权收盘价”


class NewsItem(BaseModel):
    title: str
    time: str | None = None
    source: str | None = None
    url: str | None = None
    summary: str | None = None


class NewsResult(BaseModel):
    thscode: str
    status: Literal["ok", "disabled", "failed", "empty"]
    items: list[NewsItem] = Field(default_factory=list)
    source: SourceRef | None = None
    note: str = "资讯仅作背景参考，不参与筛选与打分。"
    error: str | None = None


class CompareRequest(BaseModel):
    run_id: str
    thscodes: list[str]


class CompareRow(BaseModel):
    metric: str
    label: str
    unit: str
    period: str | None = None
    values: list[str | None]  # 与 CompareResult.stocks 同序的展示值
    raw: list[float | None]


class CompareResult(BaseModel):
    stocks: list[StockInfo]
    rows: list[CompareRow]
    prices: dict[str, list[PricePoint]] = Field(default_factory=dict)  # 归一化到 100
    notes: list[str] = Field(default_factory=list)


# ───────────────────────────── 回测 ─────────────────────────────


class BacktestRequest(BaseModel):
    strategy: Strategy
    start: str  # yyyy-MM-dd
    end: str | None = None
    rebalance: Literal["monthly", "quarterly"] = "monthly"
    top_n: int | None = 20
    cost_bps: float = 10.0  # 每边
    benchmark: str = "000300.SH"


class CurvePoint(BaseModel):
    date: str
    strategy: float
    benchmark: float | None = None


class PerfStats(BaseModel):
    total_return: float | None = None  # %
    annual_return: float | None = None  # %
    annual_vol: float | None = None  # %
    max_drawdown: float | None = None  # %
    sharpe: float | None = None
    excess_total_return: float | None = None  # %
    avg_holdings: float | None = None
    avg_turnover: float | None = None  # 每次调仓单边换手 %


class RebalanceRecord(BaseModel):
    date: str
    holdings: list[str]
    names: list[str | None] = Field(default_factory=list)
    candidates: int = 0
    note: str | None = None


class IgnoredCondition(BaseModel):
    condition_id: str
    label: str
    reason: str


class BacktestResult(BaseModel):
    id: str
    status: JobStatus
    progress: float = 0.0
    progress_text: str | None = None
    request: BacktestRequest
    curve: list[CurvePoint] = Field(default_factory=list)
    stats: PerfStats | None = None
    benchmark_stats: PerfStats | None = None
    rebalances: list[RebalanceRecord] = Field(default_factory=list)
    ignored_conditions: list[IgnoredCondition] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    datasets: list[DatasetStatus] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


# ───────────────────────────── 保存与监控 ─────────────────────────────


class SavedStrategy(BaseModel):
    id: str = ""
    name: str
    strategy: Strategy
    note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run_id: str | None = None
    last_included: int | None = None
    last_trade_date: str | None = None


class MonitorRun(BaseModel):
    run_at: datetime
    run_id: str | None = None
    status: JobStatus
    included_count: int | None = None
    trade_date: str | None = None
    diff: ResultDiff | None = None
    data_alerts: list[str] = Field(default_factory=list)
    error: str | None = None


class Monitor(BaseModel):
    id: str = ""
    name: str
    strategy: Strategy
    frequency: Literal["daily", "weekly"] = "daily"
    enabled: bool = True
    created_at: datetime | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    runs: list[MonitorRun] = Field(default_factory=list)


# ───────────────────────────── 健康 / 目录 ─────────────────────────────


class ServiceStatus(BaseModel):
    name: str
    configured: bool
    ok: bool | None = None  # None = 未检查
    detail: str | None = None
    checked_at: datetime | None = None


class HealthStatus(BaseModel):
    ok: bool
    version: str
    services: list[ServiceStatus]
    warm: dict[str, str] = Field(default_factory=dict)  # 预热状态：universe -> "ready" / "warming 35%" / "failed: ..."
    demo_faults_enabled: bool = False
    now: datetime


class OpDef(BaseModel):
    op: Op
    label: str  # "大于"、"行业/池内分位不高于"
    symbol: str  # ">"、"≤"、"分位≤"
    needs_value2: bool = False
    needs_scope: bool = False


class MetricDef(BaseModel):
    id: str
    name: str
    category: Literal["valuation", "growth", "profitability", "quality", "solvency", "price", "liquidity", "basic", "dividend", "size"]
    unit: str
    description: str  # 口径说明（给用户看）
    formula: str | None = None
    source: str  # 给用户看的出处，如“扶摇 /api/a-share/valuations/snapshot · pe_ttm”
    datasets: list[str]  # 依赖的数据集
    period_type: Literal["realtime", "report", "window", "static"]
    value_type: Literal["number", "bool"] = "number"
    higher_is_better: bool | None = None
    allowed_ops: list[Op]
    rank_scopes: list[RankScope] = Field(default_factory=list)
    positive_only_for_rank: bool = False
    plausible_range: tuple[float, float] | None = None
    sweep_values: list[float] = Field(default_factory=list)
    derived: bool = False
    estimate: bool = False  # 估算类指标（界面标“估算/推断”）
    backtestable: bool = False
    caveats: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    metrics: list[MetricDef]
    ops: list[OpDef]
    universes: list[UniverseOption]
    default_conditions: list[Condition]
    disclaimer: str
    examples: list[str]
