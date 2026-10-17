import { ClockCircleOutlined } from '@ant-design/icons';
import { Alert, Space, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';
import { useMetrics } from '../store';
import type { ScreenResult } from '../types';
import { fmtDateTime, fmtInt } from '../utils/format';
import { labelResolver } from '../utils/result';
import { ConflictList } from './ConflictList';
import { DatasetStatusTag } from './Evidence';
import { ExplanationView } from './ExplanationView';

function StatBlock({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: number | null | undefined;
  sub?: ReactNode;
  tone?: 'primary' | 'warn';
}) {
  return (
    <div className={`stat-block${tone ? ` stat-${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {fmtInt(value)}
        <small>只</small>
      </div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

/** 摘要卡：规模统计、数据时点、带标签的摘要句子、警告与数据集异常 */
export function SummaryCard({ result }: { result: ScreenResult }) {
  const metrics = useMetrics();
  const included = result.included ?? [];
  const excluded = result.excluded ?? [];
  const missing = result.missing ?? [];
  const flagged = included.filter((s) => s.flagged).length;
  const nearMiss = excluded.filter((s) => s.near_miss).length;
  const datasets = result.datasets ?? [];
  const bad = datasets.filter((d) => d.status === 'partial' || d.status === 'failed');
  const warnings = result.warnings ?? [];
  const runConflicts = result.conflicts ?? [];
  const policy = result.strategy.missing_policy ?? 'exclude';
  const label = labelResolver(result.strategy, metrics);
  const shown = datasets.filter((d) => d.status !== 'skipped');

  return (
    <div className="panel-stack">
      <div className="stat-grid">
        <StatBlock label="股票池规模" value={result.universe?.size} sub={result.universe?.name ?? '--'} />
        <StatBlock
          label="入选"
          value={included.length}
          tone="primary"
          sub={flagged ? `其中 ${flagged} 只含缺失（已标注）` : '全部硬条件通过'}
        />
        <StatBlock label="排除" value={excluded.length} sub={nearMiss ? `其中差一点入选 ${nearMiss} 只` : '至少一个硬条件未通过'} />
        <StatBlock
          label="数据缺失"
          value={missing.length}
          tone={missing.length ? 'warn' : undefined}
          sub={policy === 'exclude' ? '缺失即排除：不计入入选' : '保留并标注：缺失股已计入入选'}
        />
      </div>

      <div>
        <div className="section-title">
          <ClockCircleOutlined /> 数据时点
        </div>
        <div className="asof-list">
          <div className="asof-item">
            <span className="k">报告期</span>
            <span>
              {result.report_period ? `${result.report_period.label}（${result.report_period.report}）` : '--'}
            </span>
          </div>
          <div className="asof-item">
            <span className="k">价格窗口截止交易日</span>
            <span>{result.trade_date ?? '--'}</span>
          </div>
          <div className="asof-item">
            <span className="k">完成时间</span>
            <span>{fmtDateTime(result.finished_at ?? result.created_at, true)}</span>
          </div>
          {shown.map((d) => (
            <div className="asof-item" key={d.dataset}>
              <DatasetStatusTag status={d.status} />
              <span className="k">{d.label}</span>
              <span>{d.as_of ? fmtDateTime(d.as_of) : '无数据时点'}</span>
            </div>
          ))}
        </div>
      </div>

      <div>
        <div className="section-title">结果解读</div>
        {result.summary ? (
          <ExplanationView exp={result.summary} />
        ) : (
          <Typography.Text type="secondary">后端未返回摘要。</Typography.Text>
        )}
      </div>

      {bad.map((d) => (
        <Alert
          key={d.dataset}
          type="error"
          showIcon
          message={
            <Space size={6} wrap>
              <span>
                {d.label}
                {d.status === 'failed' ? '全部失败' : '部分失败'}
              </span>
              <Tag>{d.endpoint}</Tag>
              <span className="muted">
                请求 {fmtInt(d.requested)} / 成功 {fmtInt(d.succeeded)} / 失败 {fmtInt(d.failed)}
              </span>
            </Space>
          }
          description={
            <div style={{ fontSize: 12.5 }}>
              依赖该数据集的条件已显式标为“缺失”，不会被当作通过。
              {(d.errors ?? []).length > 0 && (
                <ul className="alert-list" style={{ marginTop: 4 }}>
                  {(d.errors ?? []).slice(0, 3).map((e, i) => (
                    <li key={i} className="mono">
                      {e}
                    </li>
                  ))}
                </ul>
              )}
              {d.note && <div>{d.note}</div>}
            </div>
          }
        />
      ))}

      {warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="运行提示"
          description={
            <ul className="alert-list">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          }
        />
      )}

      {runConflicts.length > 0 && (
        <div>
          <div className="section-title">运行期提示</div>
          <ConflictList conflicts={runConflicts} condLabel={label} />
        </div>
      )}
    </div>
  );
}
