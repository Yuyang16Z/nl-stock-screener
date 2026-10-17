import { ExperimentOutlined, FolderOpenOutlined, LoadingOutlined, SyncOutlined } from '@ant-design/icons';
import { Layout, Tooltip } from 'antd';
import { Link, NavLink } from 'react-router-dom';
import { groupServices, useStore, type ServiceKey } from '../store';
import type { CatalogResponse, ServiceStatus } from '../types';
import { fmtDateTime, fromNow } from '../utils/format';

export function BrandLogo({ size = 32 }: { size?: number }) {
  return (
    <svg className="brand-logo" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#1f4fd8" />
      <circle cx="14" cy="14" r="7" fill="none" stroke="#fff" strokeWidth="2.6" />
      <path d="M19.2 19.2 25 25" stroke="#fff" strokeWidth="2.8" strokeLinecap="round" />
      <path
        d="M10.5 15.5l2.2-2.4 2 1.6 2.8-3.3"
        fill="none"
        stroke="#fff"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const SLOTS: { key: ServiceKey; label: string; hint: string }[] = [
  { key: 'fuyao', label: '扶摇', hint: '行情、估值、财务、K线等结构化数据（筛选必需）' },
  { key: 'ifind', label: 'iFinD', hint: '个股资讯背景（仅参考，不参与筛选与打分）' },
  { key: 'llm', label: 'AI', hint: '自然语言解析与解读文案（不可用时降级为规则解析）' },
];

type PillState = 'ok' | 'bad' | 'off' | 'pending' | 'unknown';
const PILL_TEXT: Record<PillState, string> = {
  ok: '正常',
  bad: '异常',
  off: '未配置',
  pending: '未检查',
  unknown: '未知',
};

function pillState(s: ServiceStatus | undefined): PillState {
  if (!s) return 'unknown';
  if (!s.configured) return 'off';
  if (s.ok === true) return 'ok';
  if (s.ok === false) return 'bad';
  return 'pending';
}

function ServicePill({ label, hint, s }: { label: string; hint?: string; s: ServiceStatus | undefined }) {
  const state = pillState(s);
  const tip = (
    <div style={{ fontSize: 12, lineHeight: 1.7 }}>
      <div>
        <b>{label}</b>：{PILL_TEXT[state]}
        {s && s.name !== label ? `（${s.name}）` : ''}
      </div>
      {hint && <div>{hint}</div>}
      {s?.detail && <div>详情：{s.detail}</div>}
      <div>
        检查时间：{s?.checked_at ? `${fmtDateTime(s.checked_at, true)}（${fromNow(s.checked_at)}）` : '尚未检查'}
      </div>
      {state === 'off' && <div>在 .env 中配置对应密钥并重启后端后启用。</div>}
      {!s && <div>后端未返回该服务的状态。</div>}
    </div>
  );
  return (
    <Tooltip title={tip} placement="bottomRight">
      <span className={`pill pill-${state}`} aria-label={`${label}：${PILL_TEXT[state]}`}>
        <span className="pill-dot" />
        {label}
        <span className="pill-state">{PILL_TEXT[state]}</span>
      </span>
    </Tooltip>
  );
}

function universeNameByCode(catalog: CatalogResponse | null, key: string): string {
  const hit = catalog?.universes.find((u) => u.code === key || u.name === key);
  return hit?.name ?? key;
}

function WarmPill({ warm }: { warm: Record<string, string> | undefined }) {
  const catalog = useStore((s) => s.catalog);
  const entries = Object.entries(warm ?? {});
  if (!entries.length) return null;
  const parsed = entries.map(([k, v]) => {
    const raw = String(v);
    const pct = /(\d+(?:\.\d+)?)\s*%/.exec(raw);
    return {
      key: k,
      name: universeNameByCode(catalog, k),
      raw,
      state: raw === 'ready' ? 'ready' : raw.startsWith('warming') ? 'warming' : raw.startsWith('failed') ? 'failed' : 'other',
      pct: pct ? Number(pct[1]) : null,
    };
  });
  const tip = (
    <div style={{ fontSize: 12, lineHeight: 1.7 }}>
      <div>
        <b>数据预热</b>：启动后在后台预取常用股票池数据，加快首次筛选。
      </div>
      {parsed.map((p) => (
        <div key={p.key}>
          {p.name}：{p.state === 'ready' ? '已就绪' : p.raw}
        </div>
      ))}
    </div>
  );
  const warming = parsed.filter((p) => p.state === 'warming');
  const failed = parsed.filter((p) => p.state === 'failed');
  if (warming.length) {
    const w = warming[0];
    return (
      <Tooltip title={tip} placement="bottomRight">
        <span className="pill pill-pending">
          <SyncOutlined spin />
          预热 {w.name}
          {w.pct !== null ? ` ${Math.round(w.pct)}%` : ''}
          {warming.length > 1 ? ` 等 ${warming.length} 项` : ''}
        </span>
      </Tooltip>
    );
  }
  if (failed.length) {
    return (
      <Tooltip title={tip} placement="bottomRight">
        <span className="pill pill-warn">预热失败 {failed.length} 项</span>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={tip} placement="bottomRight">
      <span className="pill">已预热</span>
    </Tooltip>
  );
}

export function StatusPills() {
  const health = useStore((s) => s.health);
  const healthError = useStore((s) => s.healthError);
  const refresh = useStore((s) => s.refreshHealth);

  if (!health && !healthError) {
    return (
      <span className="pill">
        <LoadingOutlined /> 正在检查数据源…
      </span>
    );
  }
  if (!health) {
    return (
      <Tooltip title={`${healthError ?? ''}（点击重试）`} placement="bottomRight">
        <span className="pill pill-bad pill-clickable" onClick={() => void refresh()}>
          <span className="pill-dot" />
          后端未连接
        </span>
      </Tooltip>
    );
  }
  const { slots, others } = groupServices(health);
  return (
    <div className="pill-group">
      {healthError && (
        <Tooltip title={`最近一次健康检查失败：${healthError}（点击重试）`} placement="bottomRight">
          <span className="pill pill-bad pill-clickable" onClick={() => void refresh()}>
            <span className="pill-dot" />
            连接中断
          </span>
        </Tooltip>
      )}
      {SLOTS.map((slot) => (
        <ServicePill key={slot.key} label={slot.label} hint={slot.hint} s={slots[slot.key]} />
      ))}
      {others.map((s) => (
        <ServicePill key={s.name} label={s.name} s={s} />
      ))}
      <WarmPill warm={health.warm} />
    </div>
  );
}

export function AppHeader() {
  return (
    <Layout.Header className="app-header">
      <div className="app-header-inner">
        <Link to="/" className="brand" aria-label="ScreenLens 选股解释器 首页">
          <BrandLogo />
          <div>
            <div className="brand-name">ScreenLens 选股解释器</div>
            <div className="brand-sub">自然语言 → 可检查、可修改、可执行的数据条件</div>
          </div>
        </Link>
        <nav className="main-nav" aria-label="主导航">
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}>
            <ExperimentOutlined /> 选股工作台
          </NavLink>
          <NavLink to="/library" className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}>
            <FolderOpenOutlined /> 我的策略
          </NavLink>
        </nav>
        <div className="header-right">
          <StatusPills />
        </div>
      </div>
    </Layout.Header>
  );
}
