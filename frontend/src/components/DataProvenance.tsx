import { Card, Descriptions, Popover, Space, Table, Tag, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useMetrics } from '../store';
import type { DatasetStatus, MetricDef, ScreenResult } from '../types';
import { AUTO_REPORT_RULE, CATEGORY_LABEL, UNIVERSE_KIND_LABEL, fmtDateTime, fmtInt } from '../utils/format';
import { DatasetStatusTag, SourceLine } from './Evidence';

/** 数据集状态表：接口、状态、请求/成功/失败、数据时点、获取时间、错误样例 */
export function DatasetTable({ datasets }: { datasets: DatasetStatus[] }) {
  const columns: TableColumnsType<DatasetStatus> = [
    {
      title: '数据集',
      key: 'label',
      width: 190,
      render: (_, d) => (
        <div>
          <div>{d.label}</div>
          <div className="mono muted">{d.dataset}</div>
        </div>
      ),
    },
    {
      title: '接口',
      dataIndex: 'endpoint',
      key: 'endpoint',
      render: (v: string) => (
        <span className="mono" style={{ wordBreak: 'break-all' }}>
          {v}
        </span>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 92,
      render: (_, d) => <DatasetStatusTag status={d.status} />,
    },
    {
      title: '请求 / 成功 / 失败',
      key: 'counts',
      width: 140,
      align: 'right',
      render: (_, d) => (
        <span className="num">
          {fmtInt(d.requested ?? 0)} / {fmtInt(d.succeeded ?? 0)} /{' '}
          <span className={(d.failed ?? 0) > 0 ? 'text-error' : undefined}>{fmtInt(d.failed ?? 0)}</span>
        </span>
      ),
    },
    {
      title: '数据时点',
      key: 'as_of',
      width: 140,
      render: (_, d) => <span className="num">{fmtDateTime(d.as_of)}</span>,
    },
    {
      title: '获取时间',
      key: 'fetched',
      width: 160,
      render: (_, d) => <span className="num">{fmtDateTime(d.fetched_at, true)}</span>,
    },
    {
      title: '错误样例 / 说明',
      key: 'errors',
      width: 240,
      render: (_, d) => {
        const errs = d.errors ?? [];
        return (
          <div style={{ fontSize: 12 }}>
            {errs.length > 0 && (
              <Popover
                title="错误样例（最多 5 条）"
                content={
                  <ul className="alert-list mono" style={{ maxWidth: 520 }}>
                    {errs.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                }
              >
                <span className="text-error mono" style={{ cursor: 'help' }}>
                  {errs[0].length > 60 ? `${errs[0].slice(0, 60)}…` : errs[0]}
                  {errs.length > 1 ? `（共 ${errs.length} 条）` : ''}
                </span>
              </Popover>
            )}
            {d.note && <div className="muted">{d.note}</div>}
            {!errs.length && !d.note && <span className="muted">--</span>}
          </div>
        );
      },
    },
  ];
  return (
    <Table<DatasetStatus>
      rowKey="dataset"
      size="small"
      columns={columns}
      dataSource={datasets}
      pagination={false}
      scroll={{ x: 1080 }}
      locale={{ emptyText: '没有数据集记录' }}
    />
  );
}

function MetricDefTable({ defs }: { defs: MetricDef[] }) {
  const columns: TableColumnsType<MetricDef> = [
    {
      title: '指标',
      key: 'name',
      width: 190,
      render: (_, m) => (
        <div>
          <Space size={4} wrap>
            <span>{m.name}</span>
            {m.estimate && <Tag color="gold">估算</Tag>}
            {m.derived && <Tag color="purple">派生</Tag>}
          </Space>
          <div className="muted" style={{ fontSize: 12 }}>
            {CATEGORY_LABEL[m.category]} · <span className="mono">{m.id}</span>
          </div>
        </div>
      ),
    },
    { title: '口径说明', dataIndex: 'description', key: 'desc' },
    {
      title: '公式',
      key: 'formula',
      width: 200,
      render: (_, m) =>
        m.formula ? (
          <span className="mono" style={{ wordBreak: 'break-word' }}>
            {m.formula}
          </span>
        ) : (
          <span className="muted">数据源直接提供</span>
        ),
    },
    { title: '单位', key: 'unit', width: 70, render: (_, m) => m.unit || <span className="muted">无</span> },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 220,
      render: (v: string) => <span style={{ fontSize: 12, wordBreak: 'break-all' }}>{v}</span>,
    },
    {
      title: '注意事项',
      key: 'caveats',
      width: 220,
      render: (_, m) =>
        (m.caveats ?? []).length ? (
          <ul className="alert-list" style={{ fontSize: 12 }}>
            {(m.caveats ?? []).map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        ) : (
          <span className="muted">--</span>
        ),
    },
  ];
  return (
    <Table<MetricDef>
      rowKey="id"
      size="small"
      columns={columns}
      dataSource={defs}
      pagination={false}
      scroll={{ x: 1080 }}
      locale={{ emptyText: '没有启用的条件' }}
    />
  );
}

export function DataProvenance({ result }: { result: ScreenResult }) {
  const metrics = useMetrics();
  const used = [...new Set((result.strategy.conditions ?? []).filter((c) => c.enabled ?? true).map((c) => c.metric))];
  const defs = used.map((id) => metrics[id]).filter((m): m is MetricDef => !!m);
  const unknown = used.filter((id) => !metrics[id]);
  const rp = result.report_period;
  const u = result.universe;

  return (
    <div className="panel-stack">
      <Card size="small" title="数据集与接口状态">
        <DatasetTable datasets={result.datasets ?? []} />
        <div className="chart-note" style={{ marginTop: 6 }}>
          “部分失败/失败”的数据集对应的条件会显式标为缺失；数据时点取成功响应中最大的 data.timestamp。
        </div>
      </Card>

      <Card size="small" title="本次用到的指标口径">
        <MetricDefTable defs={defs} />
        {unknown.length > 0 && (
          <Typography.Text type="danger" style={{ fontSize: 12 }}>
            以下指标不在当前指标目录中：{unknown.join('、')}
          </Typography.Text>
        )}
      </Card>

      <Card size="small" title="报告期规则">
        <Descriptions
          size="small"
          column={{ xs: 1, md: 2, xl: 4 }}
          items={[
            { key: 'r', label: '本次报告期', children: rp ? `${rp.label}（${rp.report}）` : '--' },
            { key: 'y', label: '上年同期', children: rp?.yoy_report ?? '--' },
            { key: 'q', label: '包含季度数', children: rp ? `${rp.quarters} 个季度` : '--' },
            { key: 't', label: '价格窗口截止交易日', children: result.trade_date ?? '--' },
            { key: 'rule', label: '确定方式', span: 'filled', children: rp?.rule || '--' },
          ]}
        />
        <div className="chart-note" style={{ marginTop: 6 }}>
          {AUTO_REPORT_RULE}
        </div>
      </Card>

      <Card size="small" title="股票池来源与说明">
        {u ? (
          <>
            <Descriptions
              size="small"
              column={{ xs: 1, md: 2, xl: 4 }}
              items={[
                { key: 'n', label: '股票池', children: u.name },
                { key: 'k', label: '类型', children: UNIVERSE_KIND_LABEL[u.kind] ?? u.kind },
                { key: 'c', label: '代码', children: u.code ?? '--' },
                { key: 's', label: '成分数', children: `${fmtInt(u.size)} 只` },
                { key: 'a', label: '成分时点', children: fmtDateTime(u.as_of) },
                { key: 'note', label: '说明', span: 'filled', children: u.note || '--' },
              ]}
            />
            {u.source && <SourceLine s={u.source} />}
          </>
        ) : (
          <Typography.Text type="secondary">后端未返回股票池解析信息。</Typography.Text>
        )}
      </Card>

      <Card size="small" title="运行信息">
        <Descriptions
          size="small"
          column={{ xs: 1, md: 2, xl: 4 }}
          items={[
            { key: 'id', label: '运行 ID', children: <span className="mono">{result.run_id}</span> },
            {
              key: 'h',
              label: '策略哈希',
              children: <span className="mono">{result.strategy_hash ? result.strategy_hash.slice(0, 16) : '--'}</span>,
            },
            { key: 'c', label: '创建时间', children: fmtDateTime(result.created_at, true) },
            { key: 'f', label: '完成时间', children: fmtDateTime(result.finished_at, true) },
          ]}
        />
        <div className="chart-note" style={{ marginTop: 6 }}>
          同一策略（相同哈希）在同一数据快照上的判定结果完全确定、可复现。
        </div>
      </Card>
    </div>
  );
}
