/** 策略 JSON 导出 / 导入（纯函数 + 浏览器下载） */

import type { Condition, Op, SavedStrategy, Strategy } from '../types';
import { normalizeUniverse, wireStrategy } from './strategy';

export const EXPORT_FORMAT = 'screenlens.strategy';

const OPS: Op[] = ['gt', 'gte', 'lt', 'lte', 'between', 'pct_rank_lte', 'pct_rank_gte', 'is_true', 'is_false'];

export interface StrategyExport {
  format: typeof EXPORT_FORMAT;
  version: 1;
  exported_at: string;
  name: string;
  note: string | null;
  strategy: Strategy;
}

export function buildExport(s: SavedStrategy, now: Date = new Date()): StrategyExport {
  return {
    format: EXPORT_FORMAT,
    version: 1,
    exported_at: now.toISOString(),
    name: s.name,
    note: s.note ?? null,
    strategy: wireStrategy(s.strategy),
  };
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function validateStrategy(v: unknown): { strategy: Strategy } | { error: string } {
  if (!isObj(v)) return { error: '缺少 strategy 对象' };
  const conds = v.conditions ?? [];
  if (!Array.isArray(conds)) return { error: 'conditions 必须是数组' };
  for (let i = 0; i < conds.length; i += 1) {
    const c = conds[i] as unknown;
    if (!isObj(c)) return { error: `第 ${i + 1} 个条件格式不正确` };
    if (typeof c.metric !== 'string' || !c.metric) return { error: `第 ${i + 1} 个条件缺少 metric` };
    if (typeof c.op !== 'string' || !OPS.includes(c.op as Op)) return { error: `第 ${i + 1} 个条件的算子 ${String(c.op)} 不合法` };
    for (const k of ['value', 'value2', 'weight'] as const) {
      if (c[k] !== undefined && c[k] !== null && typeof c[k] !== 'number') return { error: `第 ${i + 1} 个条件的 ${k} 必须是数字` };
    }
  }
  if (v.universe !== undefined && v.universe !== null && !isObj(v.universe)) return { error: 'universe 格式不正确' };
  const strategy: Strategy = {
    ...(v as Strategy),
    universe: normalizeUniverse((v.universe as Strategy['universe']) ?? undefined),
    conditions: conds as Condition[],
  };
  return { strategy };
}

/** 接受：本产品导出格式 / {name, strategy} / 裸 Strategy */
export function parseImported(data: unknown, fileName: string): { saved: SavedStrategy } | { error: string } {
  if (!isObj(data)) return { error: '文件内容不是 JSON 对象' };
  const baseName = fileName.replace(/\.json$/i, '') || '导入的策略';
  if (isObj(data.strategy)) {
    const r = validateStrategy(data.strategy);
    if ('error' in r) return r;
    const name = typeof data.name === 'string' && data.name.trim() ? data.name.trim() : baseName;
    const note = typeof data.note === 'string' ? data.note : null;
    return { saved: { name, note, strategy: r.strategy } };
  }
  if (Array.isArray(data.conditions)) {
    const r = validateStrategy(data);
    if ('error' in r) return r;
    const name = typeof data.name === 'string' && data.name.trim() ? data.name.trim() : baseName;
    return { saved: { name, note: null, strategy: r.strategy } };
  }
  return { error: '没有找到 strategy 或 conditions 字段' };
}

export function safeFileName(name: string): string {
  return name.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60) || 'strategy';
}

/** 在浏览器中把对象保存为 JSON 文件 */
export function downloadJson(fileName: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
