import { LineChartOutlined, WarningOutlined } from '@ant-design/icons';
import { Alert, Button, DatePicker, Drawer, Empty, Form, InputNumber, Progress, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, errorMessage, sleep } from '../api';
import { useMetrics, useStore } from '../store';
import type { BacktestRequest, BacktestResult, PerfStats, Rebalance, RebalanceRecord, Strategy } from '../types';
import { JOB_STATUS_META, conditionLabel, fmtNumber, fmtPlain, fmtSignedPct, progressPercent, todayCN, trendClass } from '../utils/format';
import { CHART_COLORS, Chart, esc, type ChartOption } from './Chart';
import { DatasetTable } from './DataProvenance';
import { useDisclaimer } from './DisclaimerFooter';

interface FormValues {
  range: [Dayjs, Dayjs];
  rebalance: Rebalance;
  top_n: number | null;
  cost_bps: number;
  benchmark: string;
}

type StatKey = keyof PerfStats;
const STAT_ROWS: { key: StatKey; label: string; kind: 'ret' | 'pct' | 'num' | 'cnt'; strategyOnly?: boolean; tip?: string }[] = [
  { key: 'total_return', label: '累计收益', kind: 'ret' },
  { key: 'annual_return', label: '年化收益', kind: 'ret' },
  { key: 'annual_vol', label: '年化波动', kind: 'pct' },
  { key: 'max_drawdown', label: '最大回撤', kind: 'pct' },
  { key: 'sharpe', label: '夏普比率', kind: 'num', tip: '无风险利率按 0 计' },
  { key: 'excess_total_return', label: '相对基准超额（累计）', kind: 'ret', strategyOnly: true },
  { key: 'avg_holdings', label: '平均持仓数', kind: 'cnt', strategyOnly: true },
  { key: 'avg_turnover', label: '平均单边换手（每次调仓）', kind: 'pct', strategyOnly: true },
];

function StatValue({ v, kind }: { v: number | null | undefined; kind: 'ret' | 'pct' | 'num' | 'cnt' }) {
  if (v == null) return <span className="muted">--</span>;
  if (kind === 'ret') return <span className={`num ${trendClass(v)}`}>{fmtSignedPct(v)}</span>;
  if (kind === 'pct') return <span className="num">{fmtNumber(v, 2)}%</span>;
  if (kind === 'cnt') return <span className="num">{fmtNumber(v, 1)}</span>;
  return <span className="num">{fmtNumber(v, 2)}</span>;
}

function CurveChart({ bt }: { bt: BacktestResult }) {
  const curve = bt.curve ?? [];
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 54, right: 18, top: 34, bottom: 54 },
      legend: { top: 0, data: ['策略', '基准'] },
      tooltip: {
        trigger: 'axis',
        confine: true,
        valueFormatter: (v: unknown) => esc(typeof v === 'number' ? fmtPlain(v, 4) : '--'),
      },
      xAxis: {
        type: 'category',
        data: curve.map((p) => p.date),
        boundaryGap: false,
        axisLabel: { fontSize: 11, color: '#7a8499' },
      },
      yAxis: {
        type: 'value',
        scale: true,
        name: '净值',
        nameTextStyle: { color: '#7a8499', fontSize: 11 },
        axisLabel: { fontSize: 11, color: '#7a8499' },
        splitLine: { lineStyle: { color: '#eef1f6' } },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: '策略',
          type: 'line',
          showSymbol: false,
          data: curve.map((p) => p.strategy),
          lineStyle: { width: 2, color: CHART_COLORS[0] },
          itemStyle: { color: CHART_COLORS[0] },
        },
        {
          name: '基准',
          type: 'line',
          showSymbol: false,
          data: curve.map((p) => (p.benchmark == null ? null : p.benchmark)),
          lineStyle: { width: 1.5, color: '#8a94a6' },
          itemStyle: { color: '#8a94a6' },
        },
      ],
    }),
    [curve],
  );
  if (!curve.length) return <Empty description="没有净值数据" />;
  return <Chart option={option} height={300} />;
}

/** 回测：参数 → 轮询 → 净值曲线、统计对照、忽略条件、假设与警示、调仓记录 */
export function BacktestDrawer({
  open,
  strategy,
  stale,
  onClose,
}: {
  open: boolean;
  strategy: Strategy | null;
  stale: boolean;
  onClose: () => void;
}) {
  const disclaimer = useDisclaimer();
  const catalog = useStore((s) => s.catalog);
  const metrics = useMetrics();
  const [form] = Form.useForm<FormValues>();
  const [bt, setBt] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  useEffect(
    () => () => {
      seqRef.current += 1;
    },
    [],
  );

  const benchmarks = useMemo(() => {
    const list = (catalog?.universes ?? []).filter((u) => u.kind === 'index' && u.code);
    const opts = list.map((u) => ({ value: u.code as string, label: `${u.name}（${u.code}）` }));
    if (!opts.some((o) => o.value === '000300.SH')) opts.unshift({ value: '000300.SH', label: '沪深300（000300.SH）' });
    return opts;
  }, [catalog]);

  const notBacktestable = (strategy?.conditions ?? []).filter((c) => (c.enabled ?? true) && metrics[c.metric] && !metrics[c.metric].backtestable);

  const start = async () => {
    if (!strategy) return;
    let v: FormValues;
    try {
      v = await form.validateFields();
    } catch {
      return;
    }
    const req: BacktestRequest = {
      strategy,
      start: v.range[0].format('YYYY-MM-DD'),
      end: v.range[1].format('YYYY-MM-DD'),
      rebalance: v.rebalance,
      top_n: v.top_n ?? null,
      cost_bps: v.cost_bps ?? 10,
      benchmark: v.benchmark,
    };
    const seq = ++seqRef.current;
    setRunning(true);
    setError(null);
    setBt(null);
    try {
      const { backtest_id } = await api.startBacktest(req);
      let failures = 0;
      for (;;) {
        if (seq !== seqRef.current) return;
        await sleep(1500);
        if (seq !== seqRef.current) return;
        try {
          const r = await api.getBacktest(backtest_id);
          failures = 0;
          if (seq !== seqRef.current) return;
          setBt(r);
          if (r.status === 'done' || r.status === 'failed') break;
        } catch (e) {
          failures += 1;
          if ((e instanceof ApiError && e.status === 404) || failures >= 5) throw e;
        }
      }
    } catch (e) {
      if (seq === seqRef.current) setError(errorMessage(e));
    } finally {
      if (seq === seqRef.current) setRunning(false);
    }
  };

  const stop = () => {
    seqRef.current += 1;
    setRunning(false);
  };

  const rebalanceColumns: TableColumnsType<RebalanceRecord> = [
    { title: '调仓日', dataIndex: 'date', key: 'date', width: 110, render: (v: string) => <span className="num">{v}</span> },
    {
      title: '候选数',
      key: 'cand',
      width: 80,
      align: 'right',
      render: (_, r) => <span className="num">{r.candidates ?? 0}</span>,
    },
    {
      title: '持仓数',
      key: 'n',
      width: 80,
      align: 'right',
      render: (_, r) => <span className="num">{r.holdings.length}</span>,
    },
    {
      title: '持仓',
      key: 'h',
      render: (_, r) => {
        const names = r.holdings.map((code, i) => r.names?.[i] || code);
        const text = names.join('、');
        return (
          <Tooltip title={text || '空仓'}>
            <span style={{ fontSize: 12 }}>{text ? (text.length > 60 ? `${text.slice(0, 60)}…` : text) : '空仓'}</span>
          </Tooltip>
        );
      },
    },
    { title: '说明', key: 'note', width: 180, render: (_, r) => <span className="muted" style={{ fontSize: 12 }}>{r.note ?? '--'}</span> },
  ];

  const status = bt ? JOB_STATUS_META[bt.status] : null;
  const ignored = bt?.ignored_conditions ?? [];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={900}
      title={
        <Space>
          <LineChartOutlined /> 历史回测
        </Space>
      }
    >
      <div className="panel-stack">
        <Alert
          type="warning"
          showIcon
          icon={<WarningOutlined />}
          style={{ position: 'sticky', top: -24, zIndex: 5 }}
          message="历史回测不代表未来表现"
          description={<span style={{ fontSize: 12.5 }}>{disclaimer}</span>}
        />
        {stale && <Alert type="info" showIcon message="将使用当前编辑中的条件进行回测（与结果所用条件不同）。" />}

        <Form<FormValues>
          form={form}
          layout="inline"
          initialValues={{
            range: [todayCN().subtract(3, 'year'), todayCN()],
            rebalance: 'monthly',
            top_n: 20,
            cost_bps: 10,
            benchmark: '000300.SH',
          }}
          style={{ rowGap: 8 }}
        >
          <Form.Item name="range" label="区间" rules={[{ required: true, message: '请选择区间' }]}>
            <DatePicker.RangePicker allowClear={false} disabledDate={(d) => d.isAfter(dayjs())} />
          </Form.Item>
          <Form.Item name="rebalance" label="调仓">
            <Select
              style={{ width: 96 }}
              options={[
                { value: 'monthly', label: '每月' },
                { value: 'quarterly', label: '每季' },
              ]}
            />
          </Form.Item>
          <Form.Item name="top_n" label="持仓上限">
            <InputNumber min={1} max={200} style={{ width: 90 }} placeholder="不限" />
          </Form.Item>
          <Form.Item name="cost_bps" label="单边成本">
            <InputNumber min={0} max={500} step={1} style={{ width: 110 }} suffix="bps" />
          </Form.Item>
          <Form.Item name="benchmark" label="基准">
            <Select style={{ width: 190 }} options={benchmarks} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" loading={running} disabled={!strategy} onClick={() => void start()}>
                开始回测
              </Button>
              {running && <Button onClick={stop}>停止跟踪</Button>}
            </Space>
          </Form.Item>
        </Form>

        {notBacktestable.length > 0 && !bt && (
          <Alert
            type="info"
            className="alert-gray"
            showIcon
            message="以下条件的指标在回测中可能无法按时点一致重建，后端会列入“忽略的条件”并说明原因："
            description={
              <ul className="alert-list">
                {notBacktestable.map((c) => (
                  <li key={c.id}>{conditionLabel(c, metrics[c.metric])}</li>
                ))}
              </ul>
            }
          />
        )}

        {error && <Alert type="error" showIcon message="回测失败" description={error} />}

        {bt && (bt.status === 'pending' || bt.status === 'running') && (
          <div>
            <Progress percent={progressPercent(bt.progress)} status="active" />
            <Typography.Text type="secondary">{bt.progress_text || '回测进行中…'}</Typography.Text>
          </div>
        )}
        {!bt && running && <Progress percent={0} status="active" />}

        {bt && bt.status === 'failed' && (
          <Alert type="error" showIcon message="回测未能完成" description={bt.error || '未知错误'} />
        )}

        {bt && (bt.status === 'done' || (bt.curve ?? []).length > 0) && (
          <>
            <Space size={8}>
              {status && <Tag color={status.color}>{status.label}</Tag>}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {bt.request.start} ~ {bt.request.end ?? '今天'} · {bt.request.rebalance === 'quarterly' ? '每季' : '每月'}调仓 · 持仓上限{' '}
                {bt.request.top_n ?? '不限'} · 单边成本 {bt.request.cost_bps ?? 10} bps · 基准 {bt.request.benchmark}
              </Typography.Text>
            </Space>
            <div>
              <div className="section-title">净值曲线（策略 vs 基准）</div>
              <CurveChart bt={bt} />
            </div>
            <div>
              <div className="section-title">统计对照</div>
              <Table
                rowKey="key"
                size="small"
                pagination={false}
                dataSource={STAT_ROWS}
                columns={[
                  {
                    title: '指标',
                    key: 'label',
                    render: (_, r) => (
                      <span>
                        {r.label}
                        {r.tip && <span className="muted">（{r.tip}）</span>}
                      </span>
                    ),
                  },
                  {
                    title: '策略',
                    key: 's',
                    align: 'right',
                    width: 150,
                    render: (_, r) => <StatValue v={bt.stats?.[r.key]} kind={r.kind} />,
                  },
                  {
                    title: '基准',
                    key: 'b',
                    align: 'right',
                    width: 150,
                    render: (_, r) =>
                      r.strategyOnly ? <span className="muted">—</span> : <StatValue v={bt.benchmark_stats?.[r.key]} kind={r.kind} />,
                  },
                ]}
              />
              <div className="chart-note" style={{ marginTop: 4 }}>
                收益类数值按 A 股习惯着色：红色为正、绿色为负；仅为历史统计。
              </div>
            </div>
          </>
        )}

        {bt && ignored.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`有 ${ignored.length} 个条件未参与回测（结果不代表完整策略）`}
            description={
              <ul className="alert-list">
                {ignored.map((c) => (
                  <li key={c.condition_id}>
                    <b>{c.label}</b>：{c.reason}
                  </li>
                ))}
              </ul>
            }
          />
        )}

        {bt && ((bt.assumptions ?? []).length > 0 || (bt.caveats ?? []).length > 0) && (
          <div>
            {(bt.assumptions ?? []).length > 0 && (
              <>
                <div className="section-title">回测假设</div>
                <ul className="alert-list" style={{ fontSize: 13 }}>
                  {(bt.assumptions ?? []).map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </>
            )}
            {(bt.caveats ?? []).length > 0 && (
              <>
                <div className="section-title" style={{ marginTop: 10 }}>
                  局限与警示
                </div>
                <ul className="alert-list" style={{ fontSize: 13 }}>
                  {(bt.caveats ?? []).map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}

        {bt && (bt.rebalances ?? []).length > 0 && (
          <div>
            <div className="section-title">调仓记录（{(bt.rebalances ?? []).length} 次）</div>
            <Table<RebalanceRecord>
              rowKey="date"
              size="small"
              columns={rebalanceColumns}
              dataSource={bt.rebalances ?? []}
              pagination={(bt.rebalances ?? []).length > 12 ? { pageSize: 12 } : false}
              scroll={{ x: 'max-content' }}
            />
          </div>
        )}

        {bt && (bt.datasets ?? []).length > 0 && (
          <div>
            <div className="section-title">回测数据集</div>
            <DatasetTable datasets={bt.datasets ?? []} />
          </div>
        )}
      </div>
    </Drawer>
  );
}
