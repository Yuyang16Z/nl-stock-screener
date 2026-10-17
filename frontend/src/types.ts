/**
 * 数据契约：逐一镜像 backend/app/schemas.py（字段名、可选性、字面量联合类型保持一致）。
 *
 * 映射规则：
 * - pydantic 无默认值的字段 → 必填；有默认值（含 `= None`）的字段 → 可选 `?:`
 * - `X | None` → `X | null`；datetime → ISO 8601 字符串（带时区）
 * - 百分比类数值为“百分数值”（12.3 表示 12.3%），单位见 Evidence.unit / MetricDef.unit
 */

// ───────────────────────────── 基础枚举 ─────────────────────────────

export type Op =
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'between'
  | 'pct_rank_lte'
  | 'pct_rank_gte'
  | 'is_true'
  | 'is_false';
export type RankScope = 'universe' | 'industry' | 'market';
export type ConditionOrigin = 'ai' | 'rule' | 'user' | 'system';
export type MetricStatus = 'ok' | 'missing' | 'not_applicable' | 'error';
export type CondStatus = 'pass' | 'fail' | 'missing' | 'not_applicable' | 'skipped';
export type Bucket = 'included' | 'excluded' | 'missing';
export type UniverseKind = 'index' | 'industry' | 'concept' | 'all_a' | 'custom';
export type JobStatus = 'pending' | 'running' | 'done' | 'failed';
export type SentenceKind = 'fact' | 'inference' | 'uncertain' | 'caveat';

/** 带时区的 ISO 8601 时间字符串（pydantic datetime 序列化结果） */
export type DateTimeString = string;

// ───────────────────────────── 来源与证据 ─────────────────────────────

export type SourceProvider = 'fuyao' | 'ifind' | 'derived' | 'user' | 'system';

export interface SourceRef {
  provider: SourceProvider;
  endpoint?: string | null;
  params?: Record<string, unknown>;
  field?: string | null;
  request_id?: string | null;
  /** 信封 data.timestamp 或记录自身时间（ms） */
  data_timestamp?: number | null;
  fetched_at?: DateTimeString | null;
  cache_hit?: boolean;
}

export interface Evidence {
  metric: string;
  value: number | boolean | string | null;
  unit: string;
  display: string;
  period?: string | null;
  as_of?: DateTimeString | null;
  sources?: SourceRef[];
  derived?: boolean;
  formula?: string | null;
  inputs?: Evidence[];
  note?: string | null;
}

export interface MetricValue {
  metric: string;
  status: MetricStatus;
  value?: number | boolean | null;
  evidence?: Evidence | null;
  reason?: string | null;
  stale?: boolean;
}

// ───────────────────────────── 股票池与条件 ─────────────────────────────

/** 注意：后端默认值 code="000300.SH"、name="沪深300"，发送 all_a 等股票池时必须显式给 code: null */
export interface Universe {
  kind?: UniverseKind;
  code?: string | null;
  name?: string | null;
  codes?: string[];
  query?: string | null;
}

export interface UniverseOption {
  kind: UniverseKind;
  code: string | null;
  name: string;
  group: string;
  size_hint?: number | null;
}

export interface ResolvedUniverse {
  kind: UniverseKind;
  code: string | null;
  name: string;
  size: number;
  members: string[];
  source?: SourceRef | null;
  as_of?: DateTimeString | null;
  note?: string | null;
}

export interface Condition {
  id?: string;
  metric: string;
  op: Op;
  value?: number | null;
  value2?: number | null;
  rank_scope?: RankScope | null;
  hard?: boolean;
  weight?: number;
  enabled?: boolean;
  origin?: ConditionOrigin;
  source_phrase?: string | null;
  rationale?: string | null;
  label?: string | null;
}

export interface ReportPeriod {
  report: string;
  label: string;
  yoy_report: string;
  quarters: number;
  rule: string;
}

export type MissingPolicy = 'exclude' | 'keep_flagged';

export interface Strategy {
  name?: string | null;
  query?: string | null;
  universe?: Universe;
  conditions?: Condition[];
  missing_policy?: MissingPolicy;
  /** null = 自动（法定截止日规则） */
  report_period?: string | null;
  sort_metric?: string | null;
  sort_desc?: boolean;
}

// ───────────────────────────── 解析（AI / 规则） ─────────────────────────────

export type Confidence = 'high' | 'medium' | 'low';

export interface Interpretation {
  phrase: string;
  meaning: string;
  condition_ids?: string[];
  confidence?: Confidence;
  origin?: 'ai' | 'rule';
}

export interface ClarificationOption {
  id: string;
  label: string;
  description?: string | null;
  conditions?: Condition[];
  universe?: Universe | null;
}

export interface Clarification {
  id: string;
  phrase: string;
  question: string;
  options: ClarificationOption[];
  default_option_id: string;
  selected_option_id?: string | null;
}

export type ConflictSeverity = 'error' | 'warning' | 'info';
export type ConflictKind =
  | 'contradiction'
  | 'redundant'
  | 'tension'
  | 'universe_mismatch'
  | 'implausible'
  | 'unsupported_metric'
  | 'data_coverage'
  | 'empty_hard';

export interface Conflict {
  id: string;
  severity: ConflictSeverity;
  kind: ConflictKind;
  condition_ids?: string[];
  message: string;
  suggestion?: string | null;
  source?: 'rule' | 'ai';
}

export interface Unsupported {
  phrase: string;
  reason: string;
  alternative?: string | null;
}

export interface ComplianceNotice {
  level: 'block' | 'warn';
  category: 'prediction' | 'advice' | 'return_promise' | 'other';
  message: string;
  matched?: string[];
}

export interface ParseRequest {
  query: string;
  /** 非空 = 自然语言修改既有条件 */
  base_strategy?: Strategy | null;
}

export type ParseIntent = 'screen' | 'modify' | 'advice_request' | 'prediction_request' | 'out_of_scope';

export interface ParseResult {
  intent: ParseIntent;
  strategy?: Strategy | null;
  interpretations?: Interpretation[];
  clarifications?: Clarification[];
  conflicts?: Conflict[];
  unsupported?: Unsupported[];
  compliance?: ComplianceNotice | null;
  changes?: string[];
  parser?: 'llm' | 'rule_fallback';
  llm_model?: string | null;
  llm_error?: string | null;
  warnings?: string[];
}

export interface ValidateResult {
  strategy: Strategy;
  conflicts: Conflict[];
  can_run: boolean;
  warnings?: string[];
}

// ───────────────────────────── 数据集与快照 ─────────────────────────────

export interface DatasetStatus {
  dataset: string;
  label: string;
  endpoint: string;
  status: 'ok' | 'partial' | 'failed' | 'skipped';
  requested?: number;
  succeeded?: number;
  failed?: number;
  as_of?: DateTimeString | null;
  fetched_at?: DateTimeString | null;
  errors?: string[];
  note?: string | null;
}

export interface TickerRow {
  thscode: string;
  ticker: string;
  name?: string | null;
  exchange?: string | null;
  list_date?: string | null;
}

export interface StockInfo {
  thscode: string;
  ticker: string;
  name?: string | null;
  industry?: string | null;
  industry_code?: string | null;
  list_date?: string | null;
}

export interface StockFeatures {
  info: StockInfo;
  metrics?: Record<string, MetricValue>;
}

export interface FeatureSnapshot {
  snapshot_id: string;
  universe: ResolvedUniverse;
  stocks: Record<string, StockFeatures>;
  reference?: Record<string, Record<string, number>>;
  reference_industry?: Record<string, string>;
  industry_names?: Record<string, string>;
  datasets?: DatasetStatus[];
  report_period: ReportPeriod;
  trade_date?: string | null;
  metrics_available?: string[];
  prefiltered?: boolean;
  created_at: DateTimeString;
}

// ───────────────────────────── 判定结果 ─────────────────────────────

export interface CondEval {
  condition_id: string;
  status: CondStatus;
  value?: number | boolean | null;
  display?: string;
  threshold?: string;
  rank_pct?: number | null;
  rank_group_size?: number | null;
  /** 通过为正、未通过为负的相对距离 */
  margin?: number | null;
  reason?: string | null;
  evidence?: Evidence | null;
}

export interface StockResult {
  thscode: string;
  name?: string | null;
  industry?: string | null;
  bucket: Bucket;
  failed_conditions?: string[];
  missing_conditions?: string[];
  soft_score?: number | null;
  evaluations?: CondEval[];
  near_miss?: boolean;
  /** keep_flagged 策略下带缺失入选 */
  flagged?: boolean;
}

export interface FunnelStep {
  /** null = 股票池 */
  condition_id: string | null;
  label: string;
  remaining: number;
  removed_fail?: number;
  removed_missing?: number;
}

export interface SweepPoint {
  value: number;
  included: number;
  is_current?: boolean;
}

export interface ConditionStat {
  condition_id: string;
  label: string;
  hard: boolean;
  passed?: number;
  failed?: number;
  missing?: number;
  not_applicable?: number;
  skipped?: number;
  sole_blocker?: number;
  /** 去掉该条件后的入选数（无法精确时为 null） */
  drop_effect?: number | null;
  sweep?: SweepPoint[];
  note?: string | null;
}

export interface EvaluationOutput {
  included: StockResult[];
  excluded: StockResult[];
  missing: StockResult[];
  funnel: FunnelStep[];
  condition_stats: ConditionStat[];
  conflicts?: Conflict[];
  strategy_hash: string;
}

export interface ExplanationSentence {
  text: string;
  kind: SentenceKind;
  refs?: string[];
}

export interface Explanation {
  sentences: ExplanationSentence[];
  generator: 'llm' | 'template';
  verified?: boolean;
  verification_notes?: string[];
}

export interface ScreenRequest {
  strategy: Strategy;
  simulate_failures?: string[];
  force_refresh?: boolean;
}

export interface ScreenResult {
  run_id: string;
  status: JobStatus;
  progress?: number;
  progress_text?: string | null;
  strategy: Strategy;
  strategy_hash?: string | null;
  universe?: ResolvedUniverse | null;
  report_period?: ReportPeriod | null;
  trade_date?: string | null;
  datasets?: DatasetStatus[];
  funnel?: FunnelStep[];
  included?: StockResult[];
  excluded?: StockResult[];
  missing?: StockResult[];
  condition_stats?: ConditionStat[];
  conflicts?: Conflict[];
  summary?: Explanation | null;
  warnings?: string[];
  simulated_failures?: string[];
  created_at: DateTimeString;
  finished_at?: DateTimeString | null;
  error?: string | null;
}

export interface DiffItem {
  thscode: string;
  name?: string | null;
  reason: string;
}

export interface ResultDiff {
  base_run_id?: string | null;
  new_run_id?: string | null;
  added?: DiffItem[];
  removed?: DiffItem[];
  unchanged?: number;
  changes_text?: string[];
}

export interface WhatIfResult {
  result: ScreenResult;
  diff: ResultDiff;
}

// ───────────────────────────── 个股 / 比较 / 资讯 ─────────────────────────────

export interface PricePoint {
  date: string;
  close: number;
}

export interface StockDetail {
  info: StockInfo;
  bucket?: Bucket | null;
  metrics?: Record<string, MetricValue>;
  evaluations?: CondEval[];
  prices?: PricePoint[];
  price_source?: SourceRef | null;
  price_note?: string | null;
}

export interface NewsItem {
  title: string;
  time?: string | null;
  source?: string | null;
  url?: string | null;
  summary?: string | null;
}

export interface NewsResult {
  thscode: string;
  status: 'ok' | 'disabled' | 'failed' | 'empty';
  items?: NewsItem[];
  source?: SourceRef | null;
  note?: string;
  error?: string | null;
}

export interface CompareRequest {
  run_id: string;
  thscodes: string[];
}

export interface CompareRow {
  metric: string;
  label: string;
  unit: string;
  period?: string | null;
  /** 与 CompareResult.stocks 同序的展示值 */
  values: (string | null)[];
  raw: (number | null)[];
}

export interface CompareResult {
  stocks: StockInfo[];
  rows: CompareRow[];
  /** 归一化到 100 */
  prices?: Record<string, PricePoint[]>;
  notes?: string[];
}

// ───────────────────────────── 回测 ─────────────────────────────

export type Rebalance = 'monthly' | 'quarterly';

export interface BacktestRequest {
  strategy: Strategy;
  start: string;
  end?: string | null;
  rebalance?: Rebalance;
  top_n?: number | null;
  cost_bps?: number;
  benchmark?: string;
}

export interface CurvePoint {
  date: string;
  strategy: number;
  benchmark?: number | null;
}

export interface PerfStats {
  total_return?: number | null;
  annual_return?: number | null;
  annual_vol?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  excess_total_return?: number | null;
  avg_holdings?: number | null;
  avg_turnover?: number | null;
}

export interface RebalanceRecord {
  date: string;
  holdings: string[];
  names?: (string | null)[];
  candidates?: number;
  note?: string | null;
}

export interface IgnoredCondition {
  condition_id: string;
  label: string;
  reason: string;
}

export interface BacktestResult {
  id: string;
  status: JobStatus;
  progress?: number;
  progress_text?: string | null;
  request: BacktestRequest;
  curve?: CurvePoint[];
  stats?: PerfStats | null;
  benchmark_stats?: PerfStats | null;
  rebalances?: RebalanceRecord[];
  ignored_conditions?: IgnoredCondition[];
  assumptions?: string[];
  caveats?: string[];
  datasets?: DatasetStatus[];
  error?: string | null;
  created_at: DateTimeString;
  finished_at?: DateTimeString | null;
}

// ───────────────────────────── 保存与监控 ─────────────────────────────

export interface SavedStrategy {
  id?: string;
  name: string;
  strategy: Strategy;
  note?: string | null;
  created_at?: DateTimeString | null;
  updated_at?: DateTimeString | null;
  last_run_id?: string | null;
  last_included?: number | null;
  last_trade_date?: string | null;
}

export interface MonitorRun {
  run_at: DateTimeString;
  run_id?: string | null;
  status: JobStatus;
  included_count?: number | null;
  trade_date?: string | null;
  diff?: ResultDiff | null;
  data_alerts?: string[];
  error?: string | null;
}

export type MonitorFrequency = 'daily' | 'weekly';

export interface Monitor {
  id?: string;
  name: string;
  strategy: Strategy;
  frequency?: MonitorFrequency;
  enabled?: boolean;
  created_at?: DateTimeString | null;
  last_run_at?: DateTimeString | null;
  next_run_at?: DateTimeString | null;
  runs?: MonitorRun[];
}

// ───────────────────────────── 健康 / 目录 ─────────────────────────────

export interface ServiceStatus {
  name: string;
  configured: boolean;
  /** null = 未检查 */
  ok?: boolean | null;
  detail?: string | null;
  checked_at?: DateTimeString | null;
}

export interface HealthStatus {
  ok: boolean;
  version: string;
  services: ServiceStatus[];
  /** 预热状态：universe -> "ready" / "warming 35%" / "failed: ..." */
  warm?: Record<string, string>;
  demo_faults_enabled?: boolean;
  now: DateTimeString;
}

export interface OpDef {
  op: Op;
  label: string;
  symbol: string;
  needs_value2?: boolean;
  needs_scope?: boolean;
}

export type MetricCategory =
  | 'valuation'
  | 'growth'
  | 'profitability'
  | 'quality'
  | 'solvency'
  | 'price'
  | 'liquidity'
  | 'basic'
  | 'dividend'
  | 'size';

export interface MetricDef {
  id: string;
  name: string;
  category: MetricCategory;
  unit: string;
  description: string;
  formula?: string | null;
  source: string;
  datasets: string[];
  period_type: 'realtime' | 'report' | 'window' | 'static';
  value_type?: 'number' | 'bool';
  higher_is_better?: boolean | null;
  allowed_ops: Op[];
  rank_scopes?: RankScope[];
  positive_only_for_rank?: boolean;
  /** pydantic tuple[float, float] 序列化为二元数组 */
  plausible_range?: [number, number] | null;
  sweep_values?: number[];
  derived?: boolean;
  estimate?: boolean;
  backtestable?: boolean;
  caveats?: string[];
  aliases?: string[];
}

export interface CatalogResponse {
  metrics: MetricDef[];
  ops: OpDef[];
  universes: UniverseOption[];
  default_conditions: Condition[];
  disclaimer: string;
  examples: string[];
}

// ───────────────── 路由级响应（schemas.py 之外的简单字典返回） ─────────────────

/** POST /api/screen、POST /api/monitors/{id}/run */
export interface RunCreated {
  run_id: string;
}

/** POST /api/backtest */
export interface BacktestCreated {
  backtest_id: string;
}
