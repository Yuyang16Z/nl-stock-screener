/** 类型化 API 客户端：覆盖 DESIGN §7.3 全部路由；错误统一转换为带中文提示的 ApiError。 */

import type {
  BacktestCreated,
  BacktestRequest,
  BacktestResult,
  CatalogResponse,
  CompareRequest,
  CompareResult,
  Explanation,
  HealthStatus,
  Monitor,
  NewsResult,
  ParseRequest,
  ParseResult,
  RunCreated,
  SavedStrategy,
  ScreenRequest,
  ScreenResult,
  StockDetail,
  Strategy,
  UniverseOption,
  ValidateResult,
  WhatIfResult,
} from './types';
import { wireStrategy } from './utils/strategy';

const BASE = '/api';

export class ApiError extends Error {
  /** HTTP 状态码；0 = 网络不可达/超时，-1 = 主动取消 */
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

const STATUS_TEXT: Record<number, string> = {
  400: '请求参数有误',
  401: '未授权访问',
  403: '无权访问该资源',
  404: '请求的资源不存在（可能已过期，或后端已重启）',
  409: '请求与当前状态冲突',
  413: '请求内容过大',
  422: '请求参数校验失败',
  429: '请求过于频繁，请稍后再试',
  500: '服务器内部错误（若为开发模式，请确认后端已在 8000 端口启动）',
  502: '后端服务不可用（网关错误），请确认后端已启动',
  503: '服务暂不可用，请稍后再试',
  504: '后端响应超时，请稍后再试',
};

function formatValidationItem(it: unknown): string {
  if (it && typeof it === 'object') {
    const rec = it as { loc?: unknown; msg?: unknown };
    const loc = Array.isArray(rec.loc) ? rec.loc.filter((x) => x !== 'body').join('.') : '';
    const msg = typeof rec.msg === 'string' ? rec.msg : JSON.stringify(it);
    return loc ? `${loc}：${msg}` : msg;
  }
  return String(it);
}

/** 从 {detail} 中取中文提示；兼容 FastAPI 422 的数组形式 */
export function extractDetail(data: unknown, status: number): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    const d = (data as { detail: unknown }).detail;
    if (typeof d === 'string' && d.trim()) return d;
    if (Array.isArray(d) && d.length) {
      const parts = d.slice(0, 3).map(formatValidationItem);
      return `请求参数校验失败：${parts.join('；')}${d.length > 3 ? `（共 ${d.length} 处）` : ''}`;
    }
    if (d && typeof d === 'object') return JSON.stringify(d);
  }
  return STATUS_TEXT[status] ?? `请求失败（HTTP ${status}）`;
}

async function request<T>(
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = opts.timeoutMs ?? 60_000;
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onAbort = () => controller.abort();
  opts.signal?.addEventListener('abort', onAbort);

  let resp: Response;
  let text: string;
  try {
    resp = await fetch(BASE + path, {
      method,
      headers:
        body !== undefined
          ? { 'Content-Type': 'application/json', Accept: 'application/json' }
          : { Accept: 'application/json' },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    text = await resp.text();
  } catch {
    if (opts.signal?.aborted) throw new ApiError(-1, '请求已取消');
    if (timedOut) throw new ApiError(0, `请求超时（${Math.round(timeoutMs / 1000)} 秒），请稍后重试`);
    throw new ApiError(0, '无法连接后端服务，请确认后端已启动（开发模式下 /api 代理到 http://localhost:8000）');
  } finally {
    window.clearTimeout(timer);
    opts.signal?.removeEventListener('abort', onAbort);
  }

  let data: unknown = undefined;
  let parsed = false;
  if (text) {
    try {
      data = JSON.parse(text);
      parsed = true;
    } catch {
      parsed = false;
    }
  }
  if (!resp.ok) throw new ApiError(resp.status, extractDetail(data, resp.status));
  if (text && !parsed) throw new ApiError(resp.status, '服务器返回了无法解析的响应（非 JSON）');
  return data as T;
}

/** 列表接口兼容 `[...]` 与 `{items: [...]}` 两种返回 */
function asList<T>(data: unknown): T[] {
  if (Array.isArray(data)) return data as T[];
  if (data && typeof data === 'object' && Array.isArray((data as { items?: unknown }).items)) {
    return (data as { items: T[] }).items;
  }
  return [];
}

const seg = (s: string) => encodeURIComponent(s);

/** 轮询用的精简结果（?lite=1）：只保证状态与进度字段 */
export type ScreenLite = Pick<ScreenResult, 'run_id' | 'status'> &
  Partial<Pick<ScreenResult, 'progress' | 'progress_text' | 'error'>>;

export const api = {
  // ── 系统 ──
  health: (opts?: RequestOptions) => request<HealthStatus>('GET', '/health', undefined, { timeoutMs: 15_000, ...opts }),
  catalog: (opts?: RequestOptions) => request<CatalogResponse>('GET', '/catalog', undefined, opts),
  searchUniverses: async (q: string, opts?: RequestOptions) =>
    asList<UniverseOption>(await request<unknown>('GET', `/universes/search?q=${seg(q)}`, undefined, opts)),

  // ── 解析与校验 ──
  parse: (req: ParseRequest, opts?: RequestOptions) =>
    request<ParseResult>(
      'POST',
      '/parse',
      { query: req.query, base_strategy: req.base_strategy ? wireStrategy(req.base_strategy) : null },
      { timeoutMs: 120_000, ...opts },
    ),
  validate: (strategy: Strategy, opts?: RequestOptions) =>
    request<ValidateResult>('POST', '/strategy/validate', wireStrategy(strategy), { timeoutMs: 20_000, ...opts }),

  // ── 筛选任务 ──
  startScreen: (req: ScreenRequest) =>
    request<RunCreated>('POST', '/screen', {
      strategy: wireStrategy(req.strategy),
      simulate_failures: req.simulate_failures ?? [],
      force_refresh: req.force_refresh ?? false,
    }),
  getScreenLite: (runId: string, opts?: RequestOptions) =>
    request<ScreenLite>('GET', `/screen/${seg(runId)}?lite=1`, undefined, { timeoutMs: 15_000, ...opts }),
  getScreen: (runId: string, opts?: RequestOptions) =>
    request<ScreenResult>('GET', `/screen/${seg(runId)}`, undefined, { timeoutMs: 120_000, ...opts }),
  whatIf: (runId: string, strategy: Strategy) =>
    request<WhatIfResult>('POST', `/screen/${seg(runId)}/whatif`, wireStrategy(strategy), { timeoutMs: 180_000 }),
  stockDetail: (runId: string, thscode: string, opts?: RequestOptions) =>
    request<StockDetail>('GET', `/screen/${seg(runId)}/stocks/${seg(thscode)}`, undefined, opts),
  explainStock: (runId: string, thscode: string) =>
    request<Explanation>('POST', `/screen/${seg(runId)}/explain/${seg(thscode)}`, undefined, { timeoutMs: 120_000 }),

  // ── 个股资讯 / 比较 ──
  stockNews: (thscode: string, opts?: RequestOptions) =>
    request<NewsResult>('GET', `/stocks/${seg(thscode)}/news`, undefined, { timeoutMs: 45_000, ...opts }),
  compare: (req: CompareRequest, opts?: RequestOptions) =>
    request<CompareResult>('POST', '/compare', req, { timeoutMs: 90_000, ...opts }),

  // ── 回测 ──
  startBacktest: (req: BacktestRequest) =>
    request<BacktestCreated>('POST', '/backtest', { ...req, strategy: wireStrategy(req.strategy) }),
  getBacktest: (id: string, opts?: RequestOptions) =>
    request<BacktestResult>('GET', `/backtest/${seg(id)}`, undefined, { timeoutMs: 30_000, ...opts }),

  // ── 保存的策略 ──
  listStrategies: async () => asList<SavedStrategy>(await request<unknown>('GET', '/strategies')),
  getStrategy: (id: string) => request<SavedStrategy>('GET', `/strategies/${seg(id)}`),
  createStrategy: (s: SavedStrategy) =>
    request<SavedStrategy>('POST', '/strategies', { ...s, strategy: wireStrategy(s.strategy) }),
  deleteStrategy: (id: string) => request<unknown>('DELETE', `/strategies/${seg(id)}`),

  // ── 监控 ──
  listMonitors: async () => asList<Monitor>(await request<unknown>('GET', '/monitors')),
  getMonitor: (id: string, opts?: RequestOptions) => request<Monitor>('GET', `/monitors/${seg(id)}`, undefined, opts),
  createMonitor: (m: Monitor) => request<Monitor>('POST', '/monitors', { ...m, strategy: wireStrategy(m.strategy) }),
  deleteMonitor: (id: string) => request<unknown>('DELETE', `/monitors/${seg(id)}`),
  runMonitor: (id: string) => request<RunCreated>('POST', `/monitors/${seg(id)}/run`),
};

/** 任意异常 → 中文提示文本 */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message || '未知错误';
  return String(e);
}

export function isAbort(e: unknown): boolean {
  return e instanceof ApiError && e.status === -1;
}

export const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
