/** 策略/条件的纯函数工具：规范化、默认值、澄清切换、差异比较。 */

import type {
  Clarification,
  ClarificationOption,
  Condition,
  ConditionOrigin,
  Interpretation,
  MetricDef,
  Op,
  ParseResult,
  RankScope,
  Strategy,
  Universe,
  UniverseOption,
} from '../types';

export const isRankOp = (op: Op): boolean => op === 'pct_rank_lte' || op === 'pct_rank_gte';
export const isBoolOp = (op: Op): boolean => op === 'is_true' || op === 'is_false';
export const isNumericOp = (op: Op): boolean => !isRankOp(op) && !isBoolOp(op);

/** 与 pydantic 默认值一致：缺省股票池 = 沪深300 */
export const DEFAULT_UNIVERSE: Universe = { kind: 'index', code: '000300.SH', name: '沪深300', codes: [], query: null };

/**
 * 补全股票池字段。缺省字段按后端默认值补齐，显式 null 保持 null；
 * 这样 all_a/custom 发出去一定是 code: null，不会被后端默认值改成沪深300。
 */
export function normalizeUniverse(u: Universe | null | undefined): Universe {
  if (!u) return { ...DEFAULT_UNIVERSE };
  return {
    kind: u.kind ?? 'index',
    code: u.code === undefined ? DEFAULT_UNIVERSE.code : u.code,
    name: u.name === undefined ? DEFAULT_UNIVERSE.name : u.name,
    codes: u.codes ?? [],
    query: u.query ?? null,
  };
}

export function universeFromOption(o: UniverseOption): Universe {
  return { kind: o.kind, code: o.code ?? null, name: o.name, codes: [], query: null };
}

export function universeKey(u: Pick<Universe, 'kind' | 'code' | 'name'>): string {
  return `${u.kind ?? 'index'}:${u.code ?? u.name ?? ''}`;
}

export function universeName(u: Universe | null | undefined): string {
  const n = normalizeUniverse(u);
  if (n.kind === 'custom') return n.name || `自定义（${n.codes?.length ?? 0} 只）`;
  return n.name || n.code || n.query || '未命名股票池';
}

function wireCondition(c: Condition): Condition {
  return {
    id: c.id ?? '',
    metric: c.metric,
    op: c.op,
    value: c.value ?? null,
    value2: c.value2 ?? null,
    rank_scope: c.rank_scope ?? null,
    hard: c.hard ?? true,
    weight: c.weight ?? 1,
    enabled: c.enabled ?? true,
    origin: c.origin ?? 'user',
    source_phrase: c.source_phrase ?? null,
    rationale: c.rationale ?? null,
    label: c.label ?? null,
  };
}

/** 发往后端前的规范形态：所有字段显式给出 */
export function wireStrategy(s: Strategy): Strategy {
  return {
    name: s.name ?? null,
    query: s.query ?? null,
    universe: normalizeUniverse(s.universe),
    conditions: (s.conditions ?? []).map(wireCondition),
    missing_policy: s.missing_policy ?? 'exclude',
    report_period: s.report_period ?? null,
    sort_metric: s.sort_metric ?? null,
    sort_desc: s.sort_desc ?? true,
  };
}

/** 判定语义签名（忽略 label/来源/理由等展示字段），用于判断结果是否对应当前条件 */
export function strategySignature(s: Strategy | null | undefined): string {
  if (!s) return '';
  const w = wireStrategy(s);
  const u = w.universe ?? DEFAULT_UNIVERSE;
  return JSON.stringify({
    u: [u.kind, u.code, u.codes, u.query],
    c: (w.conditions ?? []).map((c) => [c.metric, c.op, c.value, c.value2, c.rank_scope, c.hard, c.weight, c.enabled]),
    m: w.missing_policy,
    r: w.report_period,
    s: [w.sort_metric, w.sort_desc],
  });
}

export function freshId(used: Set<string>, prefix: string): string {
  for (let i = 1; ; i += 1) {
    const id = `${prefix}${i}`;
    if (!used.has(id)) return id;
  }
}

/** 给缺 id 或重复 id 的条件补唯一 id（后端规则：c1, c2…） */
export function ensureIds(s: Strategy): Strategy {
  const conds = s.conditions ?? [];
  const reserved = new Set(conds.map((c) => c.id).filter((x): x is string => !!x));
  const used = new Set<string>();
  const out = conds.map((c) => {
    let id = c.id ?? '';
    if (!id || used.has(id)) {
      id = freshId(new Set([...reserved, ...used]), 'c');
    }
    used.add(id);
    return { ...c, id };
  });
  return { ...s, conditions: out };
}

export function condId(c: Condition): string {
  return c.id ?? '';
}

// ───────────────────────────── 默认条件 ─────────────────────────────

export function defaultOp(m: MetricDef): Op {
  const ops = m.allowed_ops;
  if (m.value_type === 'bool') return ops.includes('is_false') ? 'is_false' : ops[0];
  const pref: Op = m.higher_is_better === false ? 'lte' : 'gte';
  if (ops.includes(pref)) return pref;
  return ops.find((o) => isNumericOp(o)) ?? ops[0];
}

export function defaultValue(m: MetricDef): number {
  const sv = m.sweep_values ?? [];
  if (sv.length) return sv[Math.floor((sv.length - 1) / 2)];
  return 0;
}

function defaultScope(m: MetricDef | undefined, current?: RankScope | null): RankScope {
  const scopes = m?.rank_scopes ?? [];
  if (current && scopes.includes(current)) return current;
  return scopes[0] ?? 'universe';
}

export function makeCondition(m: MetricDef, id: string): Condition {
  const op = defaultOp(m);
  const v = defaultValue(m);
  return {
    id,
    metric: m.id,
    op,
    value: isBoolOp(op) ? null : isRankOp(op) ? 50 : v,
    value2: op === 'between' ? v * 2 || 1 : null,
    rank_scope: isRankOp(op) ? defaultScope(m) : null,
    hard: true,
    weight: 1,
    enabled: true,
    origin: 'user',
    source_phrase: null,
    rationale: null,
    label: null,
  };
}

/** 切换算子时需要联动修改的字段 */
export function patchForOp(c: Condition, op: Op, m?: MetricDef): Partial<Condition> {
  const wasRank = isRankOp(c.op);
  const patch: Partial<Condition> = { op };
  if (isRankOp(op)) {
    if (!wasRank) patch.value = 50;
    patch.value2 = null;
    patch.rank_scope = defaultScope(m, c.rank_scope);
  } else if (isBoolOp(op)) {
    patch.value = null;
    patch.value2 = null;
    patch.rank_scope = null;
  } else {
    patch.rank_scope = null;
    const base = wasRank || isBoolOp(c.op) || c.value == null ? (m ? defaultValue(m) : 0) : c.value;
    patch.value = base;
    if (op === 'between') {
      patch.value2 = c.value2 ?? (base > 0 ? base * 2 : base + 10);
    } else {
      patch.value2 = null;
    }
  }
  return patch;
}

/** 切换指标：保留 id/启用/硬软/权重，其余按新指标默认值重置 */
export function patchForMetric(c: Condition, m: MetricDef): Partial<Condition> {
  const fresh = makeCondition(m, condId(c));
  return {
    metric: m.id,
    op: fresh.op,
    value: fresh.value,
    value2: fresh.value2,
    rank_scope: fresh.rank_scope,
  };
}

export function condSignature(c: Condition): string {
  return JSON.stringify([c.metric, c.op, c.value ?? null, c.value2 ?? null, c.rank_scope ?? null]);
}

// ───────────────────────────── 澄清切换 ─────────────────────────────

export function parserOrigin(r: ParseResult | null | undefined): ConditionOrigin {
  return r?.parser === 'llm' ? 'ai' : 'rule';
}

export function initialClarSelections(r: ParseResult): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of r.clarifications ?? []) {
    out[c.id] = c.selected_option_id || c.default_option_id;
  }
  return out;
}

/** 每个澄清问题当前“占用”的条件 id（初始 = 该短语映射的条件） */
export function initialClarTracked(r: ParseResult, s: Strategy | null): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  const conds = s?.conditions ?? [];
  for (const clar of r.clarifications ?? []) {
    const ids = new Set<string>();
    for (const it of r.interpretations ?? []) {
      if (it.phrase === clar.phrase) (it.condition_ids ?? []).forEach((id) => ids.add(id));
    }
    for (const c of conds) {
      if (c.source_phrase && c.source_phrase === clar.phrase && c.id) ids.add(c.id);
    }
    out[clar.id] = [...ids].filter((id) => conds.some((c) => c.id === id));
  }
  return out;
}

function isPureUniverseClar(clar: Clarification): boolean {
  return clar.options.some((o) => !!o.universe) && clar.options.every((o) => !(o.conditions ?? []).length);
}

/**
 * 切换澄清选项：删除该短语对应的条件 → 在原位置插入新选项的条件；股票池类选项替换 universe。
 * 返回新策略与新插入的条件 id。
 */
export function applyClarificationOption(
  strategy: Strategy,
  clar: Clarification,
  option: ClarificationOption,
  prevOption: ClarificationOption | undefined,
  trackedIds: string[],
  origin: ConditionOrigin,
): { strategy: Strategy; insertedIds: string[] } {
  let next: Strategy = { ...strategy };
  if (option.universe) next.universe = normalizeUniverse(option.universe);
  if (isPureUniverseClar(clar)) return { strategy: next, insertedIds: [] };

  const conds = strategy.conditions ?? [];
  const remove = new Set(trackedIds);
  for (const c of conds) {
    if (c.id && c.source_phrase && c.source_phrase === clar.phrase) remove.add(c.id);
  }
  if (!conds.some((c) => remove.has(condId(c))) && prevOption?.conditions?.length) {
    const sigs = new Set(prevOption.conditions.map(condSignature));
    for (const c of conds) if (sigs.has(condSignature(c)) && c.id) remove.add(c.id);
  }

  const firstIdx = conds.findIndex((c) => remove.has(condId(c)));
  const kept = conds.filter((c) => !remove.has(condId(c)));
  let insertAt: number;
  if (firstIdx >= 0) {
    insertAt = conds.slice(0, firstIdx).filter((c) => !remove.has(condId(c))).length;
  } else {
    const sysIdx = kept.findIndex((c) => c.origin === 'system');
    insertAt = sysIdx >= 0 ? sysIdx : kept.length;
  }

  const used = new Set(kept.map(condId));
  const inserted: Condition[] = (option.conditions ?? []).map((c) => {
    const id = freshId(used, `${clar.id}_${option.id}_`.replace(/[^\w-]/g, '_'));
    used.add(id);
    return {
      ...c,
      id,
      origin: c.origin && c.origin !== 'user' ? c.origin : origin,
      source_phrase: clar.phrase,
      enabled: c.enabled ?? true,
      hard: c.hard ?? true,
      label: c.label ?? null,
    };
  });
  next = { ...next, conditions: [...kept.slice(0, insertAt), ...inserted, ...kept.slice(insertAt)] };
  return { strategy: next, insertedIds: inserted.map(condId) };
}

/** 某个短语当前映射到的条件（按来源短语或解析给出的 id） */
export function conditionsForInterpretation(it: Interpretation, s: Strategy | null): Condition[] {
  const ids = new Set(it.condition_ids ?? []);
  return (s?.conditions ?? []).filter((c) => (c.source_phrase && c.source_phrase === it.phrase) || ids.has(condId(c)));
}

// ───────────────────────────── 差异 ─────────────────────────────

const CHANGE_FIELDS = ['metric', 'op', 'value', 'value2', 'rank_scope', 'hard', 'weight', 'enabled'] as const;
type ChangeField = (typeof CHANGE_FIELDS)[number];

export interface CondChange {
  kind: 'added' | 'removed' | 'changed';
  before?: Condition;
  after?: Condition;
  fields: ChangeField[];
}

function norm(v: unknown): unknown {
  return v === undefined ? null : v;
}

function normalizedField(c: Condition, f: ChangeField): unknown {
  if (f === 'hard' || f === 'enabled') return c[f] ?? true;
  if (f === 'weight') return c.weight ?? 1;
  return norm(c[f]);
}

/** 条件级差异：先按 id 匹配，未匹配的再按判定签名匹配 */
export function diffConditions(base: Condition[], next: Condition[]): CondChange[] {
  const out: CondChange[] = [];
  const baseById = new Map(base.filter((c) => c.id).map((c) => [condId(c), c]));
  const matchedBase = new Set<Condition>();
  const unmatchedNext: Condition[] = [];
  for (const c of next) {
    const b = c.id ? baseById.get(c.id) : undefined;
    if (b) {
      matchedBase.add(b);
      const fields = CHANGE_FIELDS.filter((f) => JSON.stringify(normalizedField(b, f)) !== JSON.stringify(normalizedField(c, f)));
      if (fields.length) out.push({ kind: 'changed', before: b, after: c, fields });
    } else {
      unmatchedNext.push(c);
    }
  }
  const restBase = base.filter((b) => !matchedBase.has(b));
  for (const c of unmatchedNext) {
    const idx = restBase.findIndex((b) => condSignature(b) === condSignature(c));
    if (idx >= 0) {
      restBase.splice(idx, 1);
    } else {
      out.push({ kind: 'added', after: c, fields: [] });
    }
  }
  for (const b of restBase) out.push({ kind: 'removed', before: b, fields: [] });
  return out;
}

export function enabledHard(s: Strategy | null | undefined): Condition[] {
  return (s?.conditions ?? []).filter((c) => (c.enabled ?? true) && (c.hard ?? true));
}

export function enabledSoft(s: Strategy | null | undefined): Condition[] {
  return (s?.conditions ?? []).filter((c) => (c.enabled ?? true) && !(c.hard ?? true));
}
