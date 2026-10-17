/** 结果数据的纯函数工具 */

import type { CondEval, Condition, MetricDef, StockResult, Strategy } from '../types';
import { conditionLabel, fmtPlain, isFiniteNumber } from './format';
import { condId, isRankOp } from './strategy';

const evalCache = new WeakMap<StockResult, Map<string, CondEval>>();

/** 股票的 condition_id → CondEval（按对象缓存） */
export function evalsOf(s: StockResult): Map<string, CondEval> {
  let m = evalCache.get(s);
  if (!m) {
    m = new Map((s.evaluations ?? []).map((e) => [e.condition_id, e]));
    evalCache.set(s, m);
  }
  return m;
}

export function condMap(s: Strategy | null | undefined): Map<string, Condition> {
  return new Map((s?.conditions ?? []).map((c) => [condId(c), c]));
}

export function labelResolver(s: Strategy | null | undefined, metrics: Record<string, MetricDef>) {
  const map = condMap(s);
  return (id: string): string => {
    const c = map.get(id);
    return c ? conditionLabel(c, metrics[c.metric]) : id;
  };
}

/** 数值比较：null/非数值排在最后 */
export function cmpNullableNum(a: unknown, b: unknown): number {
  const x = isFiniteNumber(a) ? a : null;
  const y = isFiniteNumber(b) ? b : null;
  if (x === null && y === null) return 0;
  if (x === null) return 1;
  if (y === null) return -1;
  return x - y;
}

/** 差一点入选的差距描述：分位算子为“分位点”，阈值为 0 时为绝对差，否则为相对差 */
export function gapText(ev: CondEval | undefined, cond: Condition | undefined): string {
  if (!ev || !isFiniteNumber(ev.margin)) return '--';
  const m = Math.abs(ev.margin);
  if (cond && isRankOp(cond.op)) return `差 ${fmtPlain(m * 100, 1)} 个分位点`;
  if (cond && (cond.value === 0 || (cond.op === 'between' && (cond.value === 0 || cond.value2 === 0)))) {
    return `绝对差 ${fmtPlain(m, 2)}`;
  }
  return `相对阈值差 ${fmtPlain(m * 100, 1)}%`;
}

/** 失败条件的简短描述：值 vs 阈值（分位类同时给出分位） */
export function evalVersus(ev: CondEval | undefined): string {
  if (!ev) return '--';
  const val = ev.rank_pct != null ? `分位 ${fmtPlain(ev.rank_pct, 1)}（${ev.display || '--'}）` : ev.display || '--';
  return ev.threshold ? `${val} vs ${ev.threshold}` : val;
}
