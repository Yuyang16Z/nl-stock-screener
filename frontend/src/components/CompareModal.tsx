import { Alert, Empty, Modal, Skeleton, Table, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api, errorMessage } from '../api';
import type { CompareResult, CompareRow } from '../types';
import { fmtPlain } from '../utils/format';
import { CHART_COLORS, Chart, esc, type ChartOption } from './Chart';

/** 比较 2–5 只股票：指标 × 股票表格（含期间）+ 归一化走势 + 说明 */
export function CompareModal({
  open,
  runId,
  codes,
  onClose,
}: {
  open: boolean;
  runId: string | null;
  codes: string[];
  onClose: () => void;
}) {
  const [data, setData] = useState<CompareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = codes.join(',');

  useEffect(() => {
    if (!open || !runId || codes.length < 2) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    setData(null);
    api
      .compare({ run_id: runId, thscodes: codes }, { signal: ctrl.signal })
      .then(setData)
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, runId, key]);

  const stocks = data?.stocks ?? [];

  const columns: TableColumnsType<CompareRow> = [
    {
      title: '指标',
      key: 'label',
      fixed: 'left',
      width: 210,
      render: (_, r) => (
        <div>
          <div>
            {r.label}
            {r.unit ? <span className="muted">（{r.unit}）</span> : null}
          </div>
          {r.period && (
            <div className="muted" style={{ fontSize: 12 }}>
              {r.period}
            </div>
          )}
        </div>
      ),
    },
    ...stocks.map((s, i) => ({
      title: (
        <div style={{ textAlign: 'right' }}>
          <div>{s.name ?? s.thscode}</div>
          <div className="mono muted" style={{ fontSize: 11.5 }}>
            {s.thscode}
          </div>
        </div>
      ),
      key: `s${i}`,
      align: 'right' as const,
      width: 130,
      render: (_: unknown, r: CompareRow) => {
        const v = r.values[i];
        return v == null || v === '' ? <span className="muted">缺失</span> : <span className="num">{v}</span>;
      },
    })),
  ];

  const option = useMemo<ChartOption | null>(() => {
    const prices = data?.prices ?? {};
    const series = stocks
      .filter((s) => (prices[s.thscode] ?? []).length > 0)
      .map((s, i) => ({
        name: s.name ?? s.thscode,
        type: 'line' as const,
        showSymbol: false,
        lineStyle: { width: 1.6 },
        color: CHART_COLORS[i % CHART_COLORS.length],
        data: (prices[s.thscode] ?? []).map((p) => [p.date, p.close]),
      }));
    if (!series.length) return null;
    return {
      grid: { left: 50, right: 18, top: 36, bottom: 30 },
      legend: { top: 0, type: 'scroll' },
      tooltip: {
        trigger: 'axis',
        confine: true,
        valueFormatter: (v: unknown) => esc(fmtPlain(typeof v === 'number' ? v : Number(v), 2)),
      },
      xAxis: { type: 'time', axisLabel: { fontSize: 11, color: '#7a8499' } },
      yAxis: {
        type: 'value',
        scale: true,
        name: '起点=100',
        nameTextStyle: { color: '#7a8499', fontSize: 11 },
        axisLabel: { fontSize: 11, color: '#7a8499' },
        splitLine: { lineStyle: { color: '#eef1f6' } },
      },
      series,
    } as ChartOption;
  }, [data, stocks]);

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={980} title={`比较所选股票（${codes.length} 只）`}>
      {loading && <Skeleton active paragraph={{ rows: 6 }} />}
      {error && <Alert type="error" showIcon message="比较数据获取失败" description={error} />}
      {data && (
        <div className="panel-stack">
          <Table<CompareRow>
            rowKey="metric"
            size="small"
            columns={columns}
            dataSource={data.rows}
            pagination={false}
            scroll={{ x: 'max-content' }}
            locale={{ emptyText: '没有可比较的指标' }}
          />
          <div>
            <div className="section-title">
              归一化走势 <span className="muted">（后复权收盘价，区间起点 = 100，仅描述历史）</span>
            </div>
            {option ? <Chart option={option} height={280} /> : <Empty description="没有价格数据" />}
          </div>
          {(data.notes ?? []).length > 0 && (
            <ul className="alert-list" style={{ fontSize: 12.5, color: '#4a5468' }}>
              {(data.notes ?? []).map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            比较只并列展示数据，不对个股做优劣判断。
          </Typography.Text>
        </div>
      )}
    </Modal>
  );
}
