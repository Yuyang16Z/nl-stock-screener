import { Select, Spin } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorMessage } from '../api';
import { useStore } from '../store';
import type { Universe, UniverseOption } from '../types';
import { UNIVERSE_KIND_LABEL } from '../utils/format';
import { normalizeUniverse, universeFromOption, universeKey, universeName } from '../utils/strategy';

const GROUP_ORDER = ['当前股票池', '常用指数', '同花顺行业', '同花顺概念', '全市场'];

/** 股票池选择：预设 + 远程搜索行业/概念，分组显示 */
export function UniverseSelect({ value, onChange }: { value: Universe | undefined; onChange: (u: Universe) => void }) {
  const presets = useStore((s) => s.catalog?.universes);
  const [search, setSearch] = useState('');
  const [remote, setRemote] = useState<UniverseOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const seqRef = useRef(0);
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  const current = normalizeUniverse(value);
  const currentKey = universeKey(current);

  const onSearch = (q: string) => {
    setSearch(q);
    window.clearTimeout(timerRef.current);
    const term = q.trim();
    const seq = ++seqRef.current;
    if (!term) {
      setRemote([]);
      setLoading(false);
      setErr(null);
      return;
    }
    setLoading(true);
    timerRef.current = window.setTimeout(async () => {
      try {
        const r = await api.searchUniverses(term);
        if (seq === seqRef.current) {
          setRemote(r);
          setErr(null);
        }
      } catch (e) {
        if (seq === seqRef.current) {
          setRemote([]);
          setErr(errorMessage(e));
        }
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    }, 300);
  };

  const { options, lookup } = useMemo(() => {
    const term = search.trim().toLowerCase();
    const base = presets ?? [];
    const local = term
      ? base.filter((p) => p.name.toLowerCase().includes(term) || (p.code ?? '').toLowerCase().includes(term))
      : base;
    const all: UniverseOption[] = [];
    const seen = new Set<string>();
    const push = (o: UniverseOption) => {
      const k = universeKey(o);
      if (seen.has(k)) return;
      seen.add(k);
      all.push(o);
    };
    if (!seen.has(currentKey) && !term) {
      const inPresets = base.some((p) => universeKey(p) === currentKey);
      if (!inPresets) {
        push({
          kind: current.kind ?? 'index',
          code: current.code ?? null,
          name: universeName(current),
          group: '当前股票池',
          size_hint: current.kind === 'custom' ? current.codes?.length ?? null : null,
        });
      }
    }
    local.forEach(push);
    remote.forEach(push);
    const groups = new Map<string, UniverseOption[]>();
    for (const o of all) {
      const g = o.group || '其他';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g)!.push(o);
    }
    const names = [...groups.keys()].sort((a, b) => {
      const ia = GROUP_ORDER.indexOf(a);
      const ib = GROUP_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const lk = new Map<string, UniverseOption>();
    const opts = names.map((g) => ({
      label: g,
      title: g,
      options: (groups.get(g) ?? []).map((o) => {
        const k = universeKey(o);
        lk.set(k, o);
        return {
          value: k,
          label: (
            <div className="uni-opt">
              <span>{o.name}</span>
              <span className="uni-opt-meta">
                {UNIVERSE_KIND_LABEL[o.kind] ?? o.kind}
                {o.code ? ` · ${o.code}` : ''}
                {o.size_hint ? ` · 约 ${o.size_hint} 只` : ''}
              </span>
            </div>
          ),
        };
      }),
    }));
    return { options: opts, lookup: lk };
  }, [presets, remote, search, currentKey, current]);

  return (
    <Select
      showSearch
      value={currentKey}
      filterOption={false}
      onSearch={onSearch}
      onChange={(k: string) => {
        setSearch('');
        if (k === currentKey) return;
        const o = lookup.get(k);
        if (o) onChange(universeFromOption(o));
      }}
      onOpenChange={(open) => {
        if (!open) setSearch('');
      }}
      options={options}
      labelRender={() => (
        <span>
          {universeName(current)}
          <span className="uni-kind">
            {UNIVERSE_KIND_LABEL[current.kind ?? 'index']}
            {current.code ? ` · ${current.code}` : ''}
          </span>
        </span>
      )}
      notFoundContent={
        loading ? (
          <Spin size="small" />
        ) : err ? (
          <span className="text-error">搜索失败：{err}</span>
        ) : (
          '没有匹配的指数、行业或概念'
        )
      }
      placeholder="选择或输入关键词搜索（如 白酒、半导体）"
      style={{ width: '100%' }}
      popupMatchSelectWidth={380}
      aria-label="股票池"
    />
  );
}
