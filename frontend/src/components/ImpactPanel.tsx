import { ExperimentOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Alert, App, Button, Card, Col, Empty, InputNumber, Row, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api, errorMessage } from '../api';
import { useMetrics, useStore } from '../store';
import type { ConditionStat, DiffItem, FunnelStep, ScreenResult, StockResult, Strategy, WhatIfResult } from '../types';
import { fmtPlain, fmtSignedInt, fmtThreshold, isFiniteNumber } from '../utils/format';
import { condMap, evalVersus, evalsOf, gapText, labelResolver } from '../utils/result';
import { condId, isBoolOp, isRankOp, strategySignature } from '../utils/strategy';
import { CHART_COLORS, Chart, esc, type ChartOption } from './Chart';
import { CondStatusIcon } from './CondStatusIcon';

export type OpenStock = (code: string, ctx?: { runId: string; strategy: Strategy }) => void;

// ───────────────────────────── 漏斗 ─────────────────────────────

function funnelColor(i: number, n: number): string {
  const t = n <= 1 ? 0 : i / (n - 1);
  const from = [31, 79, 216];
  const to = [143, 176, 255];
  const c = from.map((f, k) => Math.round(f + (to[k] - f) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function FunnelView({ funnel }: { funnel: FunnelStep[] }) {
  const option = useMemo<ChartOption>(() => {
    const max = Math.max(1, ...funnel.map((f) => f.remaining));
    const data = funnel.map((f, i) => ({
      name: f.label,
      value: f.remaining,
      removedFail: f.removed_fail ?? 0,
      removedMissing: f.removed_missing ?? 0,
      first: i === 0,
      itemStyle: { color: funnelColor(i, funnel.length) },
    }));
    return {
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (p: unknown) => {
          const d = (p as { data: (typeof data)[number] }).data;
          const lines = [`<b>${esc(d.name)}</b>`, `剩余 ${d.value} 只`];
          if (!d.first) {
            lines.push(`本步未通过移除 ${d.removedFail} 只`);
            lines.push(`本步数据缺失移除 ${d.removedMissing} 只`);
          }
          return lines.join('<br/>');
        },
      },
      series: [
        {
          type: 'funnel',
          sort: 'none',
          left: 8,
          top: 6,
          bottom: 6,
          width: '40%',
          min: 0,
          max,
          minSize: '6%',
          maxSize: '100%',
          gap: 3,
          label: {
            show: true,
            position: 'right',
            formatter: (p: unknown) => {
              const d = (p as { data: (typeof data)[number] }).data;
              const name = d.name.length > 34 ? `${d.name.slice(0, 34)}…` : d.name;
              const extra = d.first
                ? '起点'
                : d.removedFail || d.removedMissing
                  ? `−${d.removedFail} 未通过 · −${d.removedMissing} 缺失`
                  : '本步无移除';
              return `{a|${name}}\n{b|剩 ${d.value} 只 · ${extra}}`;
            },
            rich: {
              a: { fontSize: 12.5, color: '#1c2333', lineHeight: 18 },
              b: { fontSize: 11.5, color: '#7a8499', lineHeight: 16 },
            },
          },
          labelLine: { show: true, length: 14, lineStyle: { color: '#c5ccd9' } },
          itemStyle: { borderColor: '#fff', borderWidth: 1 },
          data,
        },
      ],
    } as ChartOption;
  }, [funnel]);
  if (!funnel.length) return <Empty description="后端未返回漏斗数据" />;
  return <Chart option={option} height={Math.max(220, 40 + funnel.length * 46)} />;
}

// ───────────────────────────── 阈值扫描小图 ─────────────────────────────

function SweepChart({
  stat,
  unit,
  axisName,
  onPick,
}: {
  stat: ConditionStat;
  unit: string;
  /** 横轴含义：普通阈值为“阈值”，between 条件后端扫描的是“上界” */
  axisName: string;
  onPick: (value: number) => void;
}) {
  const pts = useMemo(() => [...(stat.sweep ?? [])].sort((a, b) => a.value - b.value), [stat.sweep]);
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 40, right: 18, top: 26, bottom: 26 },
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (p: unknown) => {
          const v = (p as { value: [number, number] }).value;
          const cur = pts.find((x) => x.value === v[0])?.is_current;
          return `${esc(axisName)} ${esc(fmtPlain(v[0]))}${esc(unit)}<br/>入选 ${v[1]} 只${cur ? '（当前）' : ''}<br/><span style="color:#999">点击带入试算</span>`;
        },
      },
      xAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 11, color: '#7a8499' },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        axisLabel: { fontSize: 11, color: '#7a8499' },
        splitLine: { lineStyle: { color: '#eef1f6' } },
      },
      series: [
        {
          type: 'line',
          data: pts.map((p) => ({
            value: [p.value, p.included],
            symbolSize: p.is_current ? 12 : 7,
            itemStyle: { color: p.is_current ? '#f08c00' : CHART_COLORS[0] },
            label: p.is_current
              ? { show: true, formatter: '当前', position: 'top', color: '#f08c00', fontSize: 11 }
              : undefined,
          })),
          lineStyle: { color: CHART_COLORS[0], width: 2 },
          showSymbol: true,
          animation: false,
        },
      ],
    }),
    [pts, unit, axisName],
  );
  return (
    <Chart
      option={option}
      height={170}
      onEvents={{
        click: (p: unknown) => {
          const v = (p as { value?: unknown }).value;
          if (Array.isArray(v) && isFiniteNumber(v[0])) onPick(v[0]);
        },
      }}
    />
  );
}

// ───────────────────────────── what-if 结果 ─────────────────────────────

function DiffList({
  title,
  items,
  color,
  onOpen,
}: {
  title: string;
  items: DiffItem[];
  color: string;
  onOpen: (code: string) => void;
}) {
  return (
    <div>
      <div className="diff-col-title">
        <Tag color={color}>{title}</Tag> {items.length} 只
      </div>
      <div style={{ maxHeight: 260, overflow: 'auto' }}>
        {items.length ? (
          items.map((it) => (
            <div className="diff-item" key={it.thscode}>
              <Typography.Link onClick={() => onOpen(it.thscode)}>{it.name ?? it.thscode}</Typography.Link>{' '}
              <span className="mono muted">{it.thscode}</span>
              <div className="muted">{it.reason}</div>
            </div>
          ))
        ) : (
          <span className="muted">无</span>
        )}
      </div>
    </div>
  );
}

interface WhatIfState {
  condId: string | null;
  value: number | null;
  value2: number | null;
  loading: boolean;
  error: string | null;
  response: WhatIfResult | null;
  sent: Strategy | null;
  desc: string | null;
}

const WI_INIT: WhatIfState = {
  condId: null,
  value: null,
  value2: null,
  loading: false,
  error: null,
  response: null,
  sent: null,
  desc: null,
};

function toNum(v: string | number | null): number | null {
  if (v === null || v === '') return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 条件影响：漏斗、逐条件统计、阈值扫描、what-if、差一点入选 */
export function ImpactPanel({ result, onOpen }: { result: ScreenResult; onOpen: OpenStock }) {
  const metrics = useMetrics();
  const { modal, message } = App.useApp();
  const adoptWhatIf = useStore((s) => s.adoptWhatIf);
  const workStrategy = useStore((s) => s.strategy);
  const resultSignature = useStore((s) => s.resultSignature);
  const [wi, setWi] = useState<WhatIfState>(WI_INIT);

  useEffect(() => {
    setWi(WI_INIT);
  }, [result.run_id]);

  const label = labelResolver(result.strategy, metrics);
  const cmap = condMap(result.strategy);
  const stats = result.condition_stats ?? [];
  const funnel = result.funnel ?? [];
  const includedCount = (result.included ?? []).length;
  const nearMiss = (result.excluded ?? []).filter((r) => r.near_miss);
  const whatIfConds = (result.strategy.conditions ?? []).filter((c) => (c.enabled ?? true) && !isBoolOp(c.op));
  const selCond = wi.condId ? cmap.get(wi.condId) : undefined;
  const selMetric = selCond ? metrics[selCond.metric] : undefined;

  /** picked = 扫描图上点中的值。between 条件后端扫描的是上界：写入 value2，下界保持不变 */
  const pickCond = (id: string, picked?: number) => {
    const c = cmap.get(id);
    const isBetween = c?.op === 'between';
    setWi({
      ...WI_INIT,
      condId: id,
      value: picked != null && !isBetween ? picked : c?.value ?? null,
      value2: picked != null && isBetween ? picked : c?.value2 ?? null,
    });
  };

  const runWhatIf = async (strategy: Strategy, desc: string) => {
    setWi((w) => ({ ...w, loading: true, error: null, response: null, sent: null, desc: null }));
    try {
      const r = await api.whatIf(result.run_id, strategy);
      setWi((w) => ({ ...w, loading: false, response: r, sent: strategy, desc }));
    } catch (e) {
      setWi((w) => ({ ...w, loading: false, error: errorMessage(e) }));
    }
  };

  const tryThreshold = () => {
    const c = selCond;
    if (!c || wi.value == null || (c.op === 'between' && wi.value2 == null)) return;
    const changed = wi.value !== (c.value ?? null) || (c.op === 'between' && wi.value2 !== (c.value2 ?? null));
    const next: Strategy = {
      ...result.strategy,
      conditions: (result.strategy.conditions ?? []).map((x) =>
        condId(x) === condId(c)
          ? {
              ...x,
              value: wi.value,
              value2: x.op === 'between' ? wi.value2 : x.value2,
              label: changed ? null : x.label,
              origin: changed ? 'user' : x.origin,
            }
          : x,
      ),
    };
    const before = fmtThreshold(c, selMetric);
    const after = fmtThreshold({ ...c, value: wi.value, value2: c.op === 'between' ? wi.value2 : c.value2 }, selMetric);
    void runWhatIf(next, `${selMetric?.name ?? c.metric}：${before} → ${after}`);
  };

  const tryDrop = (id: string) => {
    const next: Strategy = {
      ...result.strategy,
      conditions: (result.strategy.conditions ?? []).map((x) => (condId(x) === id ? { ...x, enabled: false } : x)),
    };
    setWi({ ...WI_INIT });
    void runWhatIf(next, `去掉条件：${label(id)}`);
  };

  const adopt = () => {
    const resp = wi.response;
    const sent = wi.sent;
    if (!resp || !sent) return;
    const doAdopt = () => {
      adoptWhatIf(sent, resp.result);
      setWi(WI_INIT);
      message.success('已采用试算条件，当前结果已更新');
    };
    if (resultSignature !== strategySignature(workStrategy)) {
      modal.confirm({
        title: '用试算条件替换当前编辑中的条件？',
        content: '执行后你又编辑过条件。采用试算会以“本次结果的条件 + 试算修改”替换当前编辑中的条件。',
        okText: '替换并采用',
        cancelText: '取消',
        onOk: doAdopt,
      });
    } else {
      doAdopt();
    }
  };

  const statColumns: TableColumnsType<ConditionStat> = [
    {
      title: '条件',
      key: 'label',
      render: (_, s) => (
        <Space size={4} wrap>
          <span>{s.label || label(s.condition_id)}</span>
          {!s.hard && <Tag>软条件</Tag>}
          {s.note && (
            <Tooltip title={s.note}>
              <InfoCircleOutlined className="help-icon" />
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: (
        <span>
          <CondStatusIcon status="pass" /> 通过
        </span>
      ),
      key: 'passed',
      width: 76,
      align: 'right',
      render: (_, s) => <span className="num">{s.passed ?? 0}</span>,
    },
    {
      title: (
        <span>
          <CondStatusIcon status="fail" /> 未通过
        </span>
      ),
      key: 'failed',
      width: 86,
      align: 'right',
      render: (_, s) => <span className="num">{s.failed ?? 0}</span>,
    },
    {
      title: (
        <span>
          <CondStatusIcon status="missing" /> 缺失
        </span>
      ),
      key: 'missing',
      width: 76,
      align: 'right',
      render: (_, s) => <span className="num">{s.missing ?? 0}</span>,
    },
    {
      title: (
        <span>
          <CondStatusIcon status="not_applicable" /> 不适用
        </span>
      ),
      key: 'na',
      width: 86,
      align: 'right',
      render: (_, s) => <span className="num">{s.not_applicable ?? 0}</span>,
    },
    {
      title: '跳过',
      key: 'skipped',
      width: 62,
      align: 'right',
      render: (_, s) => <span className="num">{s.skipped ?? 0}</span>,
    },
    {
      title: (
        <Tooltip title="只因这一个条件被排除的股票数（其他硬条件都通过）">
          <span>单独卡点</span>
        </Tooltip>
      ),
      key: 'sole',
      width: 86,
      align: 'right',
      render: (_, s) => <span className="num">{s.hard ? s.sole_blocker ?? 0 : '—'}</span>,
    },
    {
      title: (
        <Tooltip title="去掉该条件、其余条件不变时精确重算的入选数（括号内为相对当前的变化）">
          <span>去掉后入选</span>
        </Tooltip>
      ),
      key: 'drop',
      width: 120,
      align: 'right',
      render: (_, s) =>
        !s.hard ? (
          <span className="muted">软条件不过滤</span>
        ) : s.drop_effect == null ? (
          <Tooltip title="该条件无法精确重算（如依赖未获取的数据）">
            <span className="muted">无法精确计算</span>
          </Tooltip>
        ) : (
          <span className="num">
            {s.drop_effect} <span className="muted">（{fmtSignedInt(s.drop_effect - includedCount)}）</span>
          </span>
        ),
    },
    {
      title: '',
      key: 'op',
      width: 96,
      render: (_, s) =>
        s.hard ? (
          <Button size="small" onClick={() => tryDrop(s.condition_id)} disabled={wi.loading}>
            试算去掉
          </Button>
        ) : null,
    },
  ];

  const nearColumns: TableColumnsType<StockResult> = [
    {
      title: '股票',
      key: 'stock',
      width: 200,
      render: (_, r) => (
        <Space size={6}>
          <Typography.Link onClick={() => onOpen(r.thscode)}>{r.name ?? r.thscode}</Typography.Link>
          <span className="mono muted">{r.thscode}</span>
        </Space>
      ),
    },
    {
      title: '唯一未通过的条件',
      key: 'cond',
      render: (_, r) => {
        const id = (r.failed_conditions ?? [])[0];
        return id ? label(id) : '--';
      },
    },
    {
      title: '值 vs 阈值',
      key: 'vs',
      render: (_, r) => {
        const id = (r.failed_conditions ?? [])[0];
        return <span className="num">{id ? evalVersus(evalsOf(r).get(id)) : '--'}</span>;
      },
    },
    {
      title: '差距',
      key: 'gap',
      width: 160,
      align: 'right',
      render: (_, r) => {
        const id = (r.failed_conditions ?? [])[0];
        return <span className="num">{id ? gapText(evalsOf(r).get(id), cmap.get(id)) : '--'}</span>;
      },
    },
  ];

  const sweepStats = stats.filter((s) => (s.sweep ?? []).length > 1);
  const resp = wi.response;
  const diff = resp?.diff;
  const openWhatIfStock = (code: string) => {
    if (resp) onOpen(code, { runId: resp.result.run_id, strategy: resp.result.strategy });
  };

  return (
    <div className="panel-stack">
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={11}>
          <Card size="small" title="筛选漏斗" extra={<span className="chart-note">按条件顺序依次过滤，顺序不影响最终结果</span>}>
            <FunnelView funnel={funnel} />
          </Card>
        </Col>
        <Col xs={24} xl={13}>
          <Card size="small" title="试试别的阈值" extra={<ExperimentOutlined />}>
            <Space wrap>
              <Select
                value={wi.condId ?? undefined}
                placeholder="选择要调整的条件"
                style={{ width: 300 }}
                onChange={(id: string) => pickCond(id)}
                options={whatIfConds.map((c) => ({ value: condId(c), label: label(condId(c)) }))}
                aria-label="选择要调整的条件"
              />
              {selCond && (
                <Space size={4}>
                  <span className="muted">{isRankOp(selCond.op) ? '分位' : selCond.op === 'between' ? '区间' : '阈值'}</span>
                  <InputNumber
                    value={wi.value}
                    onChange={(v) => setWi((w) => ({ ...w, value: toNum(v) }))}
                    style={{ width: 110 }}
                    suffix={!isRankOp(selCond.op) && selCond.op !== 'between' ? selMetric?.unit || undefined : undefined}
                    aria-label="新阈值"
                  />
                  {selCond.op === 'between' && (
                    <>
                      <span>~</span>
                      <InputNumber
                        value={wi.value2}
                        onChange={(v) => setWi((w) => ({ ...w, value2: toNum(v) }))}
                        style={{ width: 110 }}
                        aria-label="新上界"
                      />
                      <span className="muted">{selMetric?.unit}</span>
                    </>
                  )}
                </Space>
              )}
              <Button
                type="primary"
                onClick={tryThreshold}
                loading={wi.loading}
                disabled={!selCond || wi.value == null || (selCond.op === 'between' && wi.value2 == null)}
              >
                试算
              </Button>
            </Space>
            <div className="chart-note" style={{ marginTop: 8 }}>
              试算复用本次数据快照，只重新判定，不改动当前条件；确认后可“采用此修改”。也可以点击下方扫描图上的点带入阈值（区间条件带入的是上界）。
            </div>
            {wi.error && <Alert style={{ marginTop: 8 }} type="error" showIcon message="试算失败" description={wi.error} />}
            {resp && diff && (
              <Alert
                style={{ marginTop: 10 }}
                type="info"
                showIcon
                message={
                  <span>
                    {wi.desc ? `${wi.desc}：` : ''}新增 <b>{diff.added?.length ?? 0}</b> 只 / 移除 <b>{diff.removed?.length ?? 0}</b> 只
                    （不变 {diff.unchanged ?? 0} 只），入选 {includedCount} → {(resp.result.included ?? []).length}
                  </span>
                }
                description={
                  <div>
                    {(diff.changes_text ?? []).length > 0 && (
                      <ul className="alert-list">
                        {(diff.changes_text ?? []).map((t, i) => (
                          <li key={i}>{t}</li>
                        ))}
                      </ul>
                    )}
                    <Row gutter={16}>
                      <Col span={12}>
                        <DiffList title="新增" items={diff.added ?? []} color="blue" onOpen={openWhatIfStock} />
                      </Col>
                      <Col span={12}>
                        <DiffList title="移除" items={diff.removed ?? []} color="default" onOpen={openWhatIfStock} />
                      </Col>
                    </Row>
                    <Space style={{ marginTop: 10 }}>
                      <Button type="primary" size="small" onClick={adopt}>
                        采用此修改
                      </Button>
                      <Button size="small" onClick={() => setWi(WI_INIT)}>
                        放弃
                      </Button>
                    </Space>
                  </div>
                }
              />
            )}
          </Card>
        </Col>
      </Row>

      <Card size="small" title="逐条件影响">
        <Table<ConditionStat>
          rowKey="condition_id"
          size="small"
          columns={statColumns}
          dataSource={stats}
          pagination={false}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: '后端未返回条件统计' }}
        />
      </Card>

      {sweepStats.length > 0 && (
        <div>
          <div className="section-title">
            阈值扫描 <span className="muted">（横轴为阈值，区间条件为上界；纵轴为其余条件不变时的入选数；橙点为当前阈值）</span>
          </div>
          <div className="sweep-grid">
            {sweepStats.map((s) => {
              const c = cmap.get(s.condition_id);
              const unit = c && isRankOp(c.op) ? '分位' : c ? metrics[c.metric]?.unit ?? '' : '';
              const axisName = c?.op === 'between' ? '上界' : '阈值';
              return (
                <Card
                  size="small"
                  className="sweep-card"
                  key={s.condition_id}
                  title={s.label || label(s.condition_id)}
                  extra={
                    <span className="chart-note">
                      横轴：{axisName}
                      {unit ? `（${unit}）` : ''}
                      {c?.op === 'between' ? '，下界不变' : ''}
                    </span>
                  }
                >
                  <SweepChart stat={s} unit={unit} axisName={axisName} onPick={(v) => pickCond(s.condition_id, v)} />
                </Card>
              );
            })}
          </div>
        </div>
      )}

      <Card
        size="small"
        title={`差一点入选（${nearMiss.length}）`}
        extra={<span className="chart-note">只差 1 个硬条件，且差距 ≤ 20%（分位类 ≤ 10 个分位点）</span>}
      >
        <Table<StockResult>
          rowKey="thscode"
          size="small"
          columns={nearColumns}
          dataSource={nearMiss}
          pagination={nearMiss.length > 10 ? { pageSize: 10 } : false}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: '没有差一点入选的股票' }}
        />
      </Card>
    </div>
  );
}
