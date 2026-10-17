import { Button, Empty, Select, Space, Switch, Table, Tag, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useMemo, useState } from 'react';
import { useMetrics } from '../store';
import type { CondEval, Condition, MetricDef, ScreenResult, StockResult } from '../types';
import { COND_STATUS_META, conditionLabel, fmtPlain, fmtThreshold } from '../utils/format';
import { cmpNullableNum, condMap, evalVersus, evalsOf, labelResolver } from '../utils/result';
import { condId, enabledHard, enabledSoft } from '../utils/strategy';
import { CondStatusIcon } from './CondStatusIcon';
import { EvalTooltipContent } from './Evidence';

const PAGINATION = {
  defaultPageSize: 20,
  pageSizeOptions: [20, 50, 100],
  showSizeChanger: true,
  showTotal: (t: number) => `共 ${t} 只`,
};

/** 单元格：判定图标 + 值；悬停看阈值、分位、期间、来源、时点 */
export function CondCell({ ev, label }: { ev: CondEval | undefined; label: string }) {
  if (!ev) return <span className="muted">--</span>;
  const text =
    ev.status === 'missing' || ev.status === 'not_applicable' || ev.status === 'skipped'
      ? COND_STATUS_META[ev.status].label
      : ev.display || '--';
  return (
    <Tooltip title={<EvalTooltipContent ev={ev} label={label} />} mouseEnterDelay={0.12}>
      <span className="cond-cell">
        <CondStatusIcon status={ev.status} />
        <span className={`num${ev.status === 'missing' ? ' text-missing' : ''}`}>{text}</span>
      </span>
    </Tooltip>
  );
}

function CondColumnTitle({ c, m }: { c: Condition; m: MetricDef | undefined }) {
  return (
    <Tooltip title={conditionLabel(c, m)}>
      <div style={{ lineHeight: 1.3, textAlign: 'right' }}>
        <div>{m?.name ?? c.metric}</div>
        <div className="muted" style={{ fontSize: 11.5, fontWeight: 400 }}>
          {fmtThreshold(c, m)}
        </div>
      </div>
    </Tooltip>
  );
}

function CodeCell({ code }: { code: string }) {
  return <span className="mono">{code}</span>;
}

// ───────────────────────────── 入选 ─────────────────────────────

export function IncludedTable({
  result,
  selected,
  onSelect,
  onOpen,
}: {
  result: ScreenResult;
  selected: string[];
  onSelect: (keys: string[]) => void;
  onOpen: (code: string) => void;
}) {
  const metrics = useMetrics();
  const hard = enabledHard(result.strategy);
  const soft = enabledSoft(result.strategy);
  const label = labelResolver(result.strategy, metrics);
  const data = result.included ?? [];

  const columns: TableColumnsType<StockResult> = [
    {
      title: '代码',
      dataIndex: 'thscode',
      key: 'code',
      width: 108,
      fixed: 'left',
      sorter: (a, b) => a.thscode.localeCompare(b.thscode),
      render: (v: string) => <CodeCell code={v} />,
    },
    {
      title: '名称',
      key: 'name',
      width: 128,
      fixed: 'left',
      render: (_, r) => (
        <Space size={4}>
          <Typography.Link onClick={() => onOpen(r.thscode)}>{r.name ?? r.thscode}</Typography.Link>
          {r.flagged && (
            <Tooltip title={`含缺失（缺失策略：保留并标注）：${(r.missing_conditions ?? []).map(label).join('、') || '—'}`}>
              <Tag color="orange" className="tag-mini">
                含缺失
              </Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '行业',
      dataIndex: 'industry',
      key: 'industry',
      width: 116,
      ellipsis: true,
      render: (v: string | null | undefined) => v || <span className="muted">--</span>,
    },
    ...hard.map((c) => {
      const id = condId(c);
      const lb = label(id);
      return {
        title: <CondColumnTitle c={c} m={metrics[c.metric]} />,
        key: `c_${id}`,
        width: 150,
        align: 'right' as const,
        sorter: (a: StockResult, b: StockResult) => cmpNullableNum(evalsOf(a).get(id)?.value, evalsOf(b).get(id)?.value),
        render: (_: unknown, r: StockResult) => <CondCell ev={evalsOf(r).get(id)} label={lb} />,
      };
    }),
    ...(soft.length
      ? [
          {
            title: (
              <Tooltip title="软分 = Σ(权重 × 是否满足) ÷ Σ权重，只用于排序，不参与过滤">
                <span>软分</span>
              </Tooltip>
            ),
            key: 'soft',
            width: 92,
            align: 'right' as const,
            sorter: (a: StockResult, b: StockResult) => cmpNullableNum(a.soft_score, b.soft_score),
            render: (_: unknown, r: StockResult) => (
              <Tooltip
                title={
                  <div style={{ fontSize: 12 }}>
                    {soft.map((c) => {
                      const ev = evalsOf(r).get(condId(c));
                      return (
                        <div key={condId(c)}>
                          {ev ? COND_STATUS_META[ev.status].label : '--'}：{label(condId(c))}
                        </div>
                      );
                    })}
                  </div>
                }
              >
                <span className="num">{r.soft_score == null ? '--' : `${fmtPlain(r.soft_score * 100, 0)}%`}</span>
              </Tooltip>
            ),
          },
        ]
      : []),
    {
      title: '',
      key: 'op',
      width: 64,
      fixed: 'right',
      render: (_, r) => (
        <Button size="small" type="link" onClick={() => onOpen(r.thscode)}>
          依据
        </Button>
      ),
    },
  ];

  return (
    <Table<StockResult>
      rowKey="thscode"
      size="small"
      columns={columns}
      dataSource={data}
      rowSelection={{
        selectedRowKeys: selected,
        hideSelectAll: true,
        preserveSelectedRowKeys: true,
        onChange: (keys) => onSelect(keys.map(String)),
        getCheckboxProps: (r) => ({ disabled: selected.length >= 5 && !selected.includes(r.thscode) }),
      }}
      pagination={PAGINATION}
      scroll={{ x: 'max-content' }}
      locale={{
        emptyText: (
          <Empty description="没有股票通过全部硬条件。可在“条件影响”中查看主要卡点，或试试放宽阈值。" />
        ),
      }}
    />
  );
}

// ───────────────────────────── 排除 ─────────────────────────────

export function ExcludedTable({ result, onOpen }: { result: ScreenResult; onOpen: (code: string) => void }) {
  const metrics = useMetrics();
  const [failFilter, setFailFilter] = useState<string[]>([]);
  const [onlyNear, setOnlyNear] = useState(false);
  const all = result.excluded ?? [];
  const hard = enabledHard(result.strategy);
  const cmap = condMap(result.strategy);
  const label = labelResolver(result.strategy, metrics);

  const failCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of all) for (const id of r.failed_conditions ?? []) m.set(id, (m.get(id) ?? 0) + 1);
    return m;
  }, [all]);
  const nearCount = all.filter((r) => r.near_miss).length;

  const data = useMemo(
    () =>
      all.filter(
        (r) =>
          (!onlyNear || r.near_miss) &&
          (failFilter.length === 0 || failFilter.some((id) => (r.failed_conditions ?? []).includes(id))),
      ),
    [all, onlyNear, failFilter],
  );

  const columns: TableColumnsType<StockResult> = [
    {
      title: '代码',
      dataIndex: 'thscode',
      key: 'code',
      width: 108,
      sorter: (a, b) => a.thscode.localeCompare(b.thscode),
      render: (v: string) => <CodeCell code={v} />,
    },
    {
      title: '名称',
      key: 'name',
      width: 150,
      render: (_, r) => (
        <Space size={4} wrap>
          <Typography.Link onClick={() => onOpen(r.thscode)}>{r.name ?? r.thscode}</Typography.Link>
          {r.near_miss && (
            <Tooltip title="只差 1 个硬条件，且差距在 20% 以内（分位类在 10 个分位点以内）">
              <Tag color="gold" className="tag-mini">
                差一点入选
              </Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '行业',
      dataIndex: 'industry',
      key: 'industry',
      width: 116,
      ellipsis: true,
      render: (v: string | null | undefined) => v || <span className="muted">--</span>,
    },
    {
      title: '未通过的条件（值 vs 阈值）',
      key: 'failed',
      render: (_, r) => (
        <div>
          {(r.failed_conditions ?? []).map((id) => {
            const c = cmap.get(id);
            const ev = evalsOf(r).get(id);
            return (
              <Tooltip key={id} title={ev ? <EvalTooltipContent ev={ev} label={label(id)} /> : label(id)}>
                <Tag color="red" className="fail-tag">
                  ✗ {c ? metrics[c.metric]?.name ?? c.metric : id}：{evalVersus(ev)}
                </Tag>
              </Tooltip>
            );
          })}
          {(r.missing_conditions ?? []).length > 0 && (
            <Tooltip title={`另有缺失：${(r.missing_conditions ?? []).map(label).join('、')}`}>
              <Tag color="orange" className="fail-tag">
                ? 另有 {(r.missing_conditions ?? []).length} 项缺失
              </Tag>
            </Tooltip>
          )}
        </div>
      ),
    },
    {
      title: '未通过数',
      key: 'nfail',
      width: 90,
      align: 'right',
      sorter: (a, b) => (a.failed_conditions?.length ?? 0) - (b.failed_conditions?.length ?? 0),
      render: (_, r) => <span className="num">{r.failed_conditions?.length ?? 0}</span>,
    },
    {
      title: '',
      key: 'op',
      width: 64,
      render: (_, r) => (
        <Button size="small" type="link" onClick={() => onOpen(r.thscode)}>
          依据
        </Button>
      ),
    },
  ];

  return (
    <div>
      <div className="table-toolbar">
        <Select
          mode="multiple"
          allowClear
          value={failFilter}
          onChange={(v: string[]) => setFailFilter(v)}
          placeholder="按未通过的条件筛选（任一）"
          style={{ minWidth: 320, maxWidth: 560 }}
          maxTagCount="responsive"
          options={hard
            .map((c) => ({ value: condId(c), label: `${label(condId(c))}（${failCounts.get(condId(c)) ?? 0} 只）` }))
            .filter((o) => (failCounts.get(o.value) ?? 0) > 0)}
          aria-label="按未通过条件筛选"
        />
        <Space size={6}>
          <Switch size="small" checked={onlyNear} onChange={setOnlyNear} aria-label="只看差一点入选" />
          <span>只看差一点入选（{nearCount}）</span>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          显示 {data.length} / {all.length} 只
        </Typography.Text>
      </div>
      <Table<StockResult>
        rowKey="thscode"
        size="small"
        columns={columns}
        dataSource={data}
        pagination={PAGINATION}
        scroll={{ x: 960 }}
        locale={{ emptyText: <Empty description={all.length ? '没有符合筛选的股票' : '没有被排除的股票'} /> }}
      />
    </div>
  );
}

// ───────────────────────────── 数据缺失 ─────────────────────────────

export function MissingTable({ result, onOpen }: { result: ScreenResult; onOpen: (code: string) => void }) {
  const metrics = useMetrics();
  const label = labelResolver(result.strategy, metrics);
  const rows = useMemo(
    () => [...(result.missing ?? []), ...(result.included ?? []).filter((r) => r.flagged)],
    [result.missing, result.included],
  );

  const columns: TableColumnsType<StockResult> = [
    {
      title: '代码',
      dataIndex: 'thscode',
      key: 'code',
      width: 108,
      sorter: (a, b) => a.thscode.localeCompare(b.thscode),
      render: (v: string) => <CodeCell code={v} />,
    },
    {
      title: '名称',
      key: 'name',
      width: 130,
      render: (_, r) => <Typography.Link onClick={() => onOpen(r.thscode)}>{r.name ?? r.thscode}</Typography.Link>,
    },
    {
      title: '行业',
      dataIndex: 'industry',
      key: 'industry',
      width: 116,
      ellipsis: true,
      render: (v: string | null | undefined) => v || <span className="muted">--</span>,
    },
    {
      title: '处理方式',
      key: 'bucket',
      width: 150,
      render: (_, r) =>
        r.bucket === 'included' ? (
          <Tag color="blue">已入选（含缺失标注）</Tag>
        ) : (
          <Tag color="orange">未计入入选</Tag>
        ),
    },
    {
      title: '缺失条件与原因',
      key: 'reasons',
      render: (_, r) => (
        <div>
          {(r.missing_conditions ?? []).map((id) => {
            const ev = evalsOf(r).get(id);
            return (
              <div className="missing-line" key={id}>
                <CondStatusIcon status={ev?.status ?? 'missing'} showText />
                <span>
                  <b>{label(id)}</b>
                  <span className="muted">：{ev?.reason || '无原因说明'}</span>
                </span>
              </div>
            );
          })}
          {!(r.missing_conditions ?? []).length && <span className="muted">--</span>}
        </div>
      ),
    },
    {
      title: '',
      key: 'op',
      width: 64,
      render: (_, r) => (
        <Button size="small" type="link" onClick={() => onOpen(r.thscode)}>
          依据
        </Button>
      ),
    },
  ];

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12.5 }}>
        数据缺失（接口返回空值、请求失败或指标对该股不适用）绝不会被当作“通过”。
        {(result.strategy.missing_policy ?? 'exclude') === 'exclude'
          ? '当前缺失策略为“缺失即排除”：这些股票未计入入选。'
          : '当前缺失策略为“保留并标注”：这些股票已计入入选并带“含缺失”标注。'}
      </Typography.Paragraph>
      <Table<StockResult>
        rowKey={(r) => `${r.bucket}-${r.thscode}`}
        size="small"
        columns={columns}
        dataSource={rows}
        pagination={PAGINATION}
        scroll={{ x: 900 }}
        locale={{ emptyText: <Empty description="没有数据缺失的股票" /> }}
      />
    </div>
  );
}
