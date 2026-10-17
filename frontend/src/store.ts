/** 全局状态（zustand）：目录、健康状态、工作台（解析 → 编辑 → 校验 → 执行 → 结果）。 */

import { create } from 'zustand';
import { ApiError, api, errorMessage, sleep, type ScreenLite } from './api';
import type {
  CatalogResponse,
  Condition,
  ConditionOrigin,
  HealthStatus,
  JobStatus,
  MetricDef,
  MissingPolicy,
  ParseResult,
  ScreenResult,
  ServiceStatus,
  Strategy,
  Universe,
  ValidateResult,
} from './types';
import {
  DEFAULT_UNIVERSE,
  applyClarificationOption,
  condId,
  ensureIds,
  freshId,
  initialClarSelections,
  initialClarTracked,
  makeCondition,
  normalizeUniverse,
  parserOrigin,
  strategySignature,
} from './utils/strategy';

export interface RunState {
  runId: string | null;
  status: JobStatus;
  progress: number;
  progressText: string | null;
  error: string | null;
  startedAt: number;
  simulated: string[];
}

export interface LoadedFrom {
  kind: 'library' | 'monitor' | 'manual';
  name?: string | null;
}

export type RunStartOutcome = 'started' | 'blocked' | 'noop';

interface State {
  // 目录
  catalog: CatalogResponse | null;
  catalogLoading: boolean;
  catalogError: string | null;
  loadCatalog: () => Promise<void>;

  // 健康
  health: HealthStatus | null;
  healthError: string | null;
  healthLoading: boolean;
  refreshHealth: () => Promise<void>;

  // 解析
  query: string;
  setQuery: (q: string) => void;
  parsing: boolean;
  parseError: string | null;
  parseResult: ParseResult | null;
  parsedQuery: string;
  loadedFrom: LoadedFrom | null;

  // 当前策略
  strategy: Strategy | null;
  strategyVersion: number;
  clarSelections: Record<string, string>;
  clarTracked: Record<string, string[]>;
  /** 被用户修改过的条件 → 修改前的来源 */
  editedFrom: Record<string, ConditionOrigin>;
  appliedChanges: string[];

  // 校验
  validation: ValidateResult | null;
  validationVersion: number;
  validating: boolean;
  validationError: string | null;

  // 执行选项
  simulateFailures: string[];
  forceRefresh: boolean;

  // 执行与结果
  run: RunState | null;
  result: ScreenResult | null;
  resultSignature: string | null;

  parseQuery: (q?: string) => Promise<void>;
  startManual: () => void;
  loadStrategy: (s: Strategy, from: LoadedFrom) => void;
  setUniverse: (u: Universe) => void;
  setReportPeriod: (p: string | null) => void;
  setMissingPolicy: (p: MissingPolicy) => void;
  updateCondition: (id: string, patch: Partial<Condition>) => void;
  addCondition: (metricId: string) => void;
  removeCondition: (id: string) => void;
  selectClarification: (clarId: string, optionId: string) => void;
  applyModification: (s: Strategy, changes: string[]) => void;
  validateNow: () => Promise<ValidateResult | null>;
  setSimulateFailures: (ds: string[]) => void;
  setForceRefresh: (v: boolean) => void;
  startRun: () => Promise<RunStartOutcome>;
  stopTracking: () => void;
  adoptWhatIf: (s: Strategy, r: ScreenResult) => void;
}

let validateTimer: number | undefined;
let validateSeq = 0;
let parseSeq = 0;
let runSeq = 0;

/** 实质性修改：会改变判定语义的字段 */
const SUBSTANTIVE_FIELDS: (keyof Condition)[] = ['metric', 'op', 'value', 'value2', 'rank_scope', 'hard', 'weight'];

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export const useStore = create<State>()((set, get) => {
  const scheduleValidate = (delay = 300) => {
    window.clearTimeout(validateTimer);
    validateTimer = window.setTimeout(() => {
      void get().validateNow();
    }, delay);
  };

  /** 提交新的策略版本并触发防抖校验 */
  const commit = (next: Strategy, extra: Partial<State> = {}) => {
    set({ strategy: next, strategyVersion: get().strategyVersion + 1, ...extra });
    scheduleValidate();
  };

  const resetWorkspace = (): Partial<State> => {
    runSeq += 1;
    return {
      clarSelections: {},
      clarTracked: {},
      editedFrom: {},
      appliedChanges: [],
      validation: null,
      validationError: null,
      run: null,
      result: null,
      resultSignature: null,
    };
  };

  return {
    catalog: null,
    catalogLoading: false,
    catalogError: null,
    loadCatalog: async () => {
      set({ catalogLoading: true, catalogError: null });
      try {
        const c = await api.catalog();
        set({ catalog: c, catalogLoading: false });
      } catch (e) {
        set({ catalogLoading: false, catalogError: errorMessage(e) });
      }
    },

    health: null,
    healthError: null,
    healthLoading: false,
    refreshHealth: async () => {
      if (get().healthLoading) return;
      set({ healthLoading: true });
      try {
        const h = await api.health();
        set({ health: h, healthError: null, healthLoading: false });
      } catch (e) {
        set({ healthError: errorMessage(e), healthLoading: false });
      }
    },

    query: '',
    setQuery: (q) => set({ query: q }),
    parsing: false,
    parseError: null,
    parseResult: null,
    parsedQuery: '',
    loadedFrom: null,

    strategy: null,
    strategyVersion: 0,
    clarSelections: {},
    clarTracked: {},
    editedFrom: {},
    appliedChanges: [],

    validation: null,
    validationVersion: -1,
    validating: false,
    validationError: null,

    simulateFailures: [],
    forceRefresh: false,

    run: null,
    result: null,
    resultSignature: null,

    parseQuery: async (q) => {
      const query = (q ?? get().query).trim();
      if (!query) return;
      const seq = ++parseSeq;
      set({ parsing: true, parseError: null, query });
      try {
        const r = await api.parse({ query });
        if (seq !== parseSeq) return;
        const strategy = r.strategy ? ensureIds({ ...r.strategy, query: r.strategy.query ?? query }) : null;
        set({
          ...resetWorkspace(),
          parsing: false,
          parseResult: r,
          parsedQuery: query,
          loadedFrom: null,
          strategy,
          strategyVersion: get().strategyVersion + 1,
          clarSelections: initialClarSelections(r),
          clarTracked: initialClarTracked(r, strategy),
        });
        if (strategy) scheduleValidate(0);
      } catch (e) {
        if (seq !== parseSeq) return;
        set({ parsing: false, parseError: errorMessage(e) });
      }
    },

    startManual: () => {
      const defaults = (get().catalog?.default_conditions ?? []).map((c) => ({ ...c }));
      const strategy = ensureIds({
        name: null,
        query: null,
        universe: { ...DEFAULT_UNIVERSE },
        conditions: defaults,
        missing_policy: 'exclude',
        report_period: null,
        sort_metric: null,
        sort_desc: true,
      });
      parseSeq += 1;
      set({
        ...resetWorkspace(),
        parsing: false,
        parseError: null,
        parseResult: null,
        parsedQuery: '',
        loadedFrom: { kind: 'manual' },
        strategy,
        strategyVersion: get().strategyVersion + 1,
      });
      scheduleValidate(0);
    },

    loadStrategy: (s, from) => {
      const strategy = ensureIds({ ...s, universe: normalizeUniverse(s.universe) });
      parseSeq += 1;
      set({
        ...resetWorkspace(),
        query: s.query ?? '',
        parsing: false,
        parseError: null,
        parseResult: null,
        parsedQuery: '',
        loadedFrom: from,
        strategy,
        strategyVersion: get().strategyVersion + 1,
      });
      scheduleValidate(0);
    },

    setUniverse: (u) => {
      const s = get().strategy;
      if (s) commit({ ...s, universe: normalizeUniverse(u) });
    },
    setReportPeriod: (p) => {
      const s = get().strategy;
      if (s && (s.report_period ?? null) !== p) commit({ ...s, report_period: p });
    },
    setMissingPolicy: (p) => {
      const s = get().strategy;
      if (s) commit({ ...s, missing_policy: p });
    },

    updateCondition: (id, patch) => {
      const s = get().strategy;
      if (!s) return;
      const editedFrom = { ...get().editedFrom };
      const conditions = (s.conditions ?? []).map((c) => {
        if (condId(c) !== id) return c;
        const substantive = SUBSTANTIVE_FIELDS.some((k) => k in patch && !same(patch[k], c[k]));
        const next: Condition = { ...c, ...patch };
        if (substantive) {
          next.label = null; // 由后端按新口径重新生成
          const prev = c.origin ?? 'user';
          if (prev !== 'user') {
            if (!editedFrom[id]) editedFrom[id] = prev;
            next.origin = 'user';
          }
        }
        return next;
      });
      commit({ ...s, conditions }, { editedFrom });
    },

    addCondition: (metricId) => {
      const s = get().strategy;
      const m: MetricDef | undefined = get().catalog?.metrics.find((x) => x.id === metricId);
      if (!s || !m) return;
      const used = new Set((s.conditions ?? []).map(condId));
      const c = makeCondition(m, freshId(used, 'u'));
      commit({ ...s, conditions: [...(s.conditions ?? []), c] });
    },

    removeCondition: (id) => {
      const s = get().strategy;
      if (!s) return;
      const clarTracked: Record<string, string[]> = {};
      for (const [k, ids] of Object.entries(get().clarTracked)) clarTracked[k] = ids.filter((x) => x !== id);
      const editedFrom = { ...get().editedFrom };
      delete editedFrom[id];
      commit({ ...s, conditions: (s.conditions ?? []).filter((c) => condId(c) !== id) }, { clarTracked, editedFrom });
    },

    selectClarification: (clarId, optionId) => {
      const { parseResult, strategy, clarSelections, clarTracked } = get();
      const clar = parseResult?.clarifications?.find((c) => c.id === clarId);
      const option = clar?.options.find((o) => o.id === optionId);
      if (!clar || !option || !strategy || clarSelections[clarId] === optionId) return;
      const prev = clar.options.find((o) => o.id === clarSelections[clarId]);
      const { strategy: next, insertedIds } = applyClarificationOption(
        strategy,
        clar,
        option,
        prev,
        clarTracked[clarId] ?? [],
        parserOrigin(parseResult),
      );
      commit(next, {
        clarSelections: { ...clarSelections, [clarId]: optionId },
        clarTracked: { ...clarTracked, [clarId]: insertedIds },
      });
    },

    applyModification: (s, changes) => {
      const next = ensureIds({ ...s, universe: normalizeUniverse(s.universe) });
      const ids = new Set((next.conditions ?? []).map(condId));
      const editedFrom: Record<string, ConditionOrigin> = {};
      for (const [k, v] of Object.entries(get().editedFrom)) if (ids.has(k)) editedFrom[k] = v;
      commit(next, { editedFrom, appliedChanges: [...get().appliedChanges, ...changes] });
    },

    validateNow: async () => {
      window.clearTimeout(validateTimer);
      const { strategy, strategyVersion } = get();
      if (!strategy) return null;
      const seq = ++validateSeq;
      set({ validating: true });
      try {
        const r = await api.validate(strategy);
        if (seq !== validateSeq) return null;
        const cur = get();
        let merged = cur.strategy;
        if (cur.strategy && cur.strategyVersion === strategyVersion) {
          const labels = new Map((r.strategy.conditions ?? []).map((c) => [condId(c), c.label ?? null]));
          let changed = false;
          const conditions = (cur.strategy.conditions ?? []).map((c) => {
            const l = labels.get(condId(c));
            if (!c.label && l) {
              changed = true;
              return { ...c, label: l };
            }
            return c;
          });
          if (changed) merged = { ...cur.strategy, conditions };
        }
        set({ validation: r, validationVersion: strategyVersion, validating: false, validationError: null, strategy: merged });
        return r;
      } catch (e) {
        if (seq === validateSeq) set({ validating: false, validationError: errorMessage(e) });
        return null;
      }
    },

    setSimulateFailures: (ds) => set({ simulateFailures: ds }),
    setForceRefresh: (v) => set({ forceRefresh: v }),

    startRun: async () => {
      if (!get().strategy) return 'noop';
      if (!get().validation || get().validationVersion !== get().strategyVersion) {
        await get().validateNow();
      }
      const v = get().validation;
      if (v && get().validationVersion === get().strategyVersion && !v.can_run) return 'blocked';
      const strategy = get().strategy;
      if (!strategy) return 'noop';

      const seq = ++runSeq;
      const signature = strategySignature(strategy);
      const demo = get().health?.demo_faults_enabled ?? false;
      const simulated = demo ? [...get().simulateFailures] : [];
      set({
        run: {
          runId: null,
          status: 'pending',
          progress: 0,
          progressText: '正在提交筛选任务…',
          error: null,
          startedAt: Date.now(),
          simulated,
        },
        result: null,
        resultSignature: null,
      });
      const upd = (patch: Partial<RunState>) => {
        const r = get().run;
        if (r && seq === runSeq) set({ run: { ...r, ...patch } });
      };

      void (async () => {
        try {
          const { run_id } = await api.startScreen({
            strategy,
            simulate_failures: simulated,
            force_refresh: get().forceRefresh,
          });
          if (seq !== runSeq) return;
          upd({ runId: run_id, status: 'running', progressText: '任务已提交，等待执行…' });
          let failures = 0;
          for (;;) {
            await sleep(1000);
            if (seq !== runSeq) return;
            let lite: ScreenLite;
            try {
              lite = await api.getScreenLite(run_id);
              failures = 0;
            } catch (e) {
              if (e instanceof ApiError && e.status === 404) throw e;
              failures += 1;
              if (failures >= 5) throw e;
              continue;
            }
            if (seq !== runSeq) return;
            upd({
              status: lite.status,
              progress: lite.progress ?? 0,
              progressText: lite.progress_text ?? null,
              error: lite.error ?? null,
            });
            if (lite.status === 'done' || lite.status === 'failed') break;
          }
          upd({ progressText: '正在载入完整结果…' });
          const full = await api.getScreen(run_id);
          if (seq !== runSeq) return;
          const r = get().run;
          set({
            result: full,
            resultSignature: signature,
            run: r
              ? { ...r, status: full.status, progress: 1, progressText: full.progress_text ?? null, error: full.error ?? null }
              : null,
          });
        } catch (e) {
          if (seq !== runSeq) return;
          upd({ status: 'failed', error: errorMessage(e) });
        }
      })();
      return 'started';
    },

    stopTracking: () => {
      runSeq += 1;
      set({ run: null });
    },

    adoptWhatIf: (s, r) => {
      runSeq += 1;
      const labels = new Map((r.strategy.conditions ?? []).map((c) => [condId(c), c.label ?? null]));
      const base = ensureIds({ ...s, universe: normalizeUniverse(s.universe) });
      const strategy: Strategy = {
        ...base,
        conditions: (base.conditions ?? []).map((c) => ({ ...c, label: c.label || labels.get(condId(c)) || null })),
      };
      set({
        strategy,
        strategyVersion: get().strategyVersion + 1,
        result: r,
        resultSignature: strategySignature(strategy),
        run: {
          runId: r.run_id,
          status: r.status,
          progress: 1,
          progressText: r.progress_text ?? null,
          error: r.error ?? null,
          startedAt: Date.now(),
          simulated: r.simulated_failures ?? [],
        },
      });
      scheduleValidate(0);
    },
  };
});

// ───────────────────────────── 派生工具 ─────────────────────────────

let cachedCatalog: CatalogResponse | null = null;
let cachedMetrics: Record<string, MetricDef> = {};

export function metricsMap(c: CatalogResponse | null): Record<string, MetricDef> {
  if (c !== cachedCatalog) {
    cachedCatalog = c;
    cachedMetrics = Object.fromEntries((c?.metrics ?? []).map((m) => [m.id, m]));
  }
  return cachedMetrics;
}

export function useMetrics(): Record<string, MetricDef> {
  return metricsMap(useStore((s) => s.catalog));
}

export type ServiceKey = 'fuyao' | 'ifind' | 'llm';

const SERVICE_MATCHERS: { key: ServiceKey; re: RegExp }[] = [
  { key: 'fuyao', re: /fuyao|扶摇/i },
  { key: 'ifind', re: /ifind/i },
  { key: 'llm', re: /llm|deepseek|openai|^ai$|ai[\s_-]|模型/i },
];

/** 把 health.services 归到 扶摇 / iFinD / AI 三个槽位 */
export function groupServices(h: HealthStatus | null): {
  slots: Record<ServiceKey, ServiceStatus | undefined>;
  others: ServiceStatus[];
} {
  const slots: Record<ServiceKey, ServiceStatus | undefined> = { fuyao: undefined, ifind: undefined, llm: undefined };
  const others: ServiceStatus[] = [];
  for (const s of h?.services ?? []) {
    const hit = SERVICE_MATCHERS.find((m) => !slots[m.key] && m.re.test(s.name));
    if (hit) slots[hit.key] = s;
    else others.push(s);
  }
  return { slots, others };
}

/** 扶摇未配置或异常 → 无法执行筛选 */
export function fuyaoUnavailable(h: HealthStatus | null): { unavailable: boolean; reason: string | null } {
  if (!h) return { unavailable: false, reason: null };
  const s = groupServices(h).slots.fuyao;
  if (!s) return { unavailable: false, reason: null };
  if (!s.configured) return { unavailable: true, reason: s.detail || '未配置 FUYAO_API_KEY' };
  if (s.ok === false) return { unavailable: true, reason: s.detail || '扶摇接口检查失败' };
  return { unavailable: false, reason: null };
}
