/** 展示层格式化与中文标签（纯函数）。 */

import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import timezone from 'dayjs/plugin/timezone';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';
import type {
  Bucket,
  CondStatus,
  Condition,
  ConditionOrigin,
  ConflictKind,
  DatasetStatus,
  JobStatus,
  MetricCategory,
  MetricDef,
  MetricStatus,
  RankScope,
  SentenceKind,
  SourceProvider,
  UniverseKind,
} from '../types';

dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

export const TZ = 'Asia/Shanghai';
export const DASH = '--';

// ───────────────────────────── 数字 ─────────────────────────────

export function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** 固定小数位 + 千分位，如 1,234.50 */
export function fmtNumber(v: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(v)) return DASH;
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** 去掉多余尾零的数值，如 0.5、25、1,234.56 */
export function fmtPlain(v: number | null | undefined, maxDigits = 2): string {
  if (!isFiniteNumber(v)) return DASH;
  return v.toLocaleString('zh-CN', { maximumFractionDigits: maxDigits });
}

export function fmtInt(v: number | null | undefined): string {
  if (!isFiniteNumber(v)) return DASH;
  return Math.round(v).toLocaleString('zh-CN');
}

/** 数值 + 单位：12.3%、15.2倍、365天、1.5个百分点；布尔 → 是/否 */
export function fmtWithUnit(
  v: number | boolean | string | null | undefined,
  unit = '',
  maxDigits = 2,
): string {
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'string') return v;
  if (!isFiniteNumber(v)) return DASH;
  const s = fmtPlain(v, unit === '天' ? 0 : maxDigits);
  if (unit === '百分点') return `${s}个百分点`;
  return `${s}${unit}`;
}

/** 带符号百分数，如 +12.30% / -3.10% */
export function fmtSignedPct(v: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(v)) return DASH;
  const s = v.toFixed(digits);
  return `${v > 0 ? '+' : ''}${s}%`;
}

export function fmtSignedInt(v: number | null | undefined): string {
  if (!isFiniteNumber(v)) return DASH;
  return `${v > 0 ? '+' : ''}${Math.round(v)}`;
}

/** A 股习惯：红涨绿跌（仅用于价格/收益类数值） */
export function trendClass(v: number | null | undefined): string {
  if (!isFiniteNumber(v) || v === 0) return '';
  return v > 0 ? 'trend-up' : 'trend-down';
}

/** 0~1 的进度；兼容后端误给 0~100 */
export function progressPercent(p: number | null | undefined): number {
  if (!isFiniteNumber(p)) return 0;
  const x = p > 1 ? p / 100 : p;
  return Math.max(0, Math.min(100, Math.round(x * 100)));
}

// ───────────────────────────── 时间 ─────────────────────────────

export function toDayjs(v: string | number | null | undefined): dayjs.Dayjs | null {
  if (v === null || v === undefined || v === '') return null;
  const d = dayjs(v);
  return d.isValid() ? d.tz(TZ) : null;
}

/** ISO 时间或毫秒时间戳 → 2026-10-16 15:00（Asia/Shanghai） */
export function fmtDateTime(v: string | number | null | undefined, withSeconds = false): string {
  const d = toDayjs(v);
  return d ? d.format(withSeconds ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD HH:mm') : DASH;
}

export function fmtDate(v: string | number | null | undefined): string {
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
  const d = toDayjs(v);
  return d ? d.format('YYYY-MM-DD') : DASH;
}

export function fromNow(v: string | number | null | undefined): string {
  const d = toDayjs(v);
  return d ? d.fromNow() : '';
}

export function todayCN(): dayjs.Dayjs {
  return dayjs().tz(TZ);
}

// ───────────────────────────── 报告期 ─────────────────────────────

const REPORT_NAME: Record<string, string> = { '1': '一季报', '2': '中报', '3': '三季报', '4': '年报' };

export function isValidReport(s: string | null | undefined): boolean {
  return /^\d{4}-[1-4]$/.test((s ?? '').trim());
}

/** "2026-2" → "2026年中报" */
export function reportLabel(report: string | null | undefined): string {
  const m = /^(\d{4})-([1-4])$/.exec((report ?? '').trim());
  if (!m) return report || DASH;
  return `${m[1]}年${REPORT_NAME[m[2]]}`;
}

export const AUTO_REPORT_RULE =
  '自动：按法定披露截止日（一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30）选取“全部公司都应已披露”的最近报告期；' +
  '年报与一季报截止日相同，5/1 起取当年一季报。手动指定较新的报告期时，未披露的公司会显示为数据缺失。';

/** 前端按同一规则推算的报告期（仅作提示，以执行结果为准） */
export function uniformReportPeriod(today: dayjs.Dayjs = todayCN()): string {
  const y = today.year();
  const ymd = today.format('YYYY-MM-DD');
  const candidates: [string, string][] = [
    [`${y}-3`, `${y}-10-31`],
    [`${y}-2`, `${y}-08-31`],
    [`${y}-1`, `${y}-04-30`],
    [`${y - 1}-4`, `${y}-04-30`],
    [`${y - 1}-3`, `${y - 1}-10-31`],
  ];
  for (const [rep, deadline] of candidates) {
    if (ymd > deadline) return rep;
  }
  return `${y - 1}-3`;
}

// ───────────────────────────── 标签字典 ─────────────────────────────

export const CATEGORY_LABEL: Record<MetricCategory, string> = {
  valuation: '估值',
  growth: '成长',
  profitability: '盈利能力',
  quality: '盈利质量',
  solvency: '偿债能力',
  price: '价格与风险',
  liquidity: '流动性',
  size: '规模',
  dividend: '分红',
  basic: '基础信息',
};
export const CATEGORY_ORDER: MetricCategory[] = [
  'valuation',
  'growth',
  'profitability',
  'quality',
  'solvency',
  'price',
  'liquidity',
  'size',
  'dividend',
  'basic',
];

export const RANK_SCOPE_LABEL: Record<RankScope, string> = {
  universe: '股票池内',
  industry: '所属行业内',
  market: '全A股内',
};

export const ORIGIN_META: Record<ConditionOrigin, { label: string; color: string; desc: string }> = {
  ai: { label: 'AI', color: 'geekblue', desc: '由 AI 根据原句解析生成（草案，需你确认）' },
  rule: { label: '规则', color: 'cyan', desc: '由规则词典解析生成（AI 不可用时的降级解析）' },
  user: { label: '用户', color: 'purple', desc: '由你添加或修改' },
  system: { label: '系统', color: 'default', desc: '系统默认风控条件（可删除）' },
};

export const SENTENCE_KIND_META: Record<SentenceKind, { label: string; color: string }> = {
  fact: { label: '事实', color: 'blue' },
  inference: { label: '推断', color: 'purple' },
  uncertain: { label: '不确定', color: 'orange' },
  caveat: { label: '提示', color: 'default' },
};

export const COND_STATUS_META: Record<CondStatus, { label: string; color: string }> = {
  pass: { label: '通过', color: '#2f9e44' },
  fail: { label: '未通过', color: '#e03131' },
  missing: { label: '缺失', color: '#e8590c' },
  not_applicable: { label: '不适用', color: '#868e96' },
  skipped: { label: '跳过', color: '#adb5bd' },
};

export const METRIC_STATUS_LABEL: Record<MetricStatus, string> = {
  ok: '正常',
  missing: '缺失',
  not_applicable: '不适用',
  error: '获取失败',
};

export const DATASET_STATUS_META: Record<DatasetStatus['status'], { label: string; color: string }> = {
  ok: { label: '正常', color: 'success' },
  partial: { label: '部分失败', color: 'warning' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '未请求', color: 'default' },
};

export const JOB_STATUS_META: Record<JobStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  done: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
};

export const BUCKET_META: Record<Bucket, { label: string; color: string }> = {
  included: { label: '入选', color: 'blue' },
  excluded: { label: '排除', color: 'default' },
  missing: { label: '数据缺失', color: 'orange' },
};

export const UNIVERSE_KIND_LABEL: Record<UniverseKind, string> = {
  index: '指数',
  industry: '行业',
  concept: '概念',
  all_a: '全A',
  custom: '自定义',
};

export const CONFLICT_KIND_LABEL: Record<ConflictKind, string> = {
  contradiction: '条件矛盾',
  redundant: '条件冗余',
  tension: '语义张力',
  universe_mismatch: '股票池不匹配',
  implausible: '阈值异常',
  unsupported_metric: '不支持',
  data_coverage: '数据覆盖不足',
  empty_hard: '无硬条件',
};

export const PROVIDER_LABEL: Record<SourceProvider, string> = {
  fuyao: '扶摇',
  ifind: 'iFinD',
  derived: '派生计算',
  user: '用户',
  system: '系统',
};

/** 演示故障注入可选的数据集 */
export const FAULT_DATASETS: { id: string; label: string; short: string }[] = [
  { id: 'valuations', label: '模拟估值接口失败(valuations)', short: '估值快照' },
  { id: 'indicators', label: '模拟财务指标接口失败(indicators)', short: '财务指标' },
  { id: 'history', label: '模拟K线接口失败(history)', short: '历史日K' },
];

export function faultLabel(id: string): string {
  return FAULT_DATASETS.find((f) => f.id === id)?.short ?? id;
}

// ───────────────────────────── 条件描述 ─────────────────────────────

const CMP_SYMBOL: Partial<Record<Condition['op'], string>> = { gt: '>', gte: '≥', lt: '<', lte: '≤' };

/** 阈值部分的短描述，如 "> 0%"、"0 ~ 25倍"、"行业内分位 ≤ 50" */
export function fmtThreshold(c: Condition, m?: MetricDef): string {
  const unit = m?.unit ?? '';
  switch (c.op) {
    case 'gt':
    case 'gte':
    case 'lt':
    case 'lte':
      return `${CMP_SYMBOL[c.op]} ${fmtWithUnit(c.value, unit)}`;
    case 'between':
      return `${fmtPlain(c.value)} ~ ${fmtWithUnit(c.value2, unit)}`;
    case 'pct_rank_lte':
    case 'pct_rank_gte': {
      const scope = c.rank_scope ? RANK_SCOPE_LABEL[c.rank_scope] : '参考组内';
      return `${scope}分位 ${c.op === 'pct_rank_lte' ? '≤' : '≥'} ${fmtPlain(c.value)}`;
    }
    case 'is_true':
      return '是';
    case 'is_false':
      return '否';
    default:
      return DASH;
  }
}

/** 前端兜底的条件描述（后端 label 缺失或条件刚被编辑时使用） */
export function describeCondition(c: Condition, m?: MetricDef): string {
  const name = m?.name ?? c.metric;
  if (c.op === 'is_true' || c.op === 'is_false') return `${name}：${c.op === 'is_true' ? '是' : '否'}`;
  if (c.op === 'between') return `${name} 介于 ${fmtThreshold(c, m)}`;
  return `${name} ${fmtThreshold(c, m)}`;
}

export function conditionLabel(c: Condition, m?: MetricDef): string {
  return c.label?.trim() ? c.label : describeCondition(c, m);
}

/** 截断长文本 */
export function truncate(s: string | null | undefined, n: number): string {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
