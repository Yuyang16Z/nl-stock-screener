import { CheckOutlined, CloseOutlined, EditOutlined } from '@ant-design/icons';
import { Alert, Button, Input, Space, Tag, Typography } from 'antd';
import { useMemo, useState, type ReactNode } from 'react';
import { api, errorMessage } from '../api';
import { useMetrics, useStore } from '../store';
import type { Condition, MetricDef, ParseResult, Strategy } from '../types';
import { conditionLabel, describeCondition, reportLabel } from '../utils/format';
import { diffConditions, normalizeUniverse, universeKey, universeName } from '../utils/strategy';
import { ConflictList } from './ConflictList';
import { ParserBadge } from './ParserBadge';

const FIELD_LABEL: Record<string, string> = {
  metric: '指标',
  op: '算子',
  value: '阈值',
  value2: '上界',
  rank_scope: '分位口径',
  hard: '硬/软',
  weight: '权重',
  enabled: '启用',
};

function condText(c: Condition, metrics: Record<string, MetricDef>, preferLabel: boolean): string {
  const m = metrics[c.metric];
  const base = preferLabel ? conditionLabel(c, m) : describeCondition(c, m);
  const flags = [c.enabled === false ? '停用' : '', c.hard === false ? '软条件' : ''].filter(Boolean).join('、');
  return flags ? `${base}（${flags}）` : base;
}

/** 新旧策略对比：股票池/报告期/缺失策略 + 条件级增删改 */
function StrategyDiff({ base, next }: { base: Strategy; next: Strategy }) {
  const metrics = useMetrics();
  const changes = useMemo(() => diffConditions(base.conditions ?? [], next.conditions ?? []), [base, next]);
  const rows: ReactNode[] = [];
  const ub = normalizeUniverse(base.universe);
  const un = normalizeUniverse(next.universe);
  if (universeKey(ub) !== universeKey(un)) {
    rows.push(
      <div className="diff-row" key="u">
        <Tag color="blue">股票池</Tag>
        <span className="diff-before">{universeName(ub)}</span>
        <span className="diff-arrow">→</span>
        <span>{universeName(un)}</span>
      </div>,
    );
  }
  if ((base.report_period ?? null) !== (next.report_period ?? null)) {
    rows.push(
      <div className="diff-row" key="rp">
        <Tag color="blue">报告期</Tag>
        <span className="diff-before">{base.report_period ? reportLabel(base.report_period) : '自动'}</span>
        <span className="diff-arrow">→</span>
        <span>{next.report_period ? reportLabel(next.report_period) : '自动'}</span>
      </div>,
    );
  }
  if ((base.missing_policy ?? 'exclude') !== (next.missing_policy ?? 'exclude')) {
    rows.push(
      <div className="diff-row" key="mp">
        <Tag color="blue">缺失策略</Tag>
        <span>{next.missing_policy === 'keep_flagged' ? '保留并标注' : '缺失即排除'}</span>
      </div>,
    );
  }
  changes.forEach((ch, i) => {
    if (ch.kind === 'added' && ch.after) {
      rows.push(
        <div className="diff-row" key={`a${i}`}>
          <Tag color="green">新增</Tag>
          <span>{condText(ch.after, metrics, false)}</span>
        </div>,
      );
    } else if (ch.kind === 'removed' && ch.before) {
      rows.push(
        <div className="diff-row" key={`r${i}`}>
          <Tag color="red">删除</Tag>
          <span className="diff-before">{condText(ch.before, metrics, true)}</span>
        </div>,
      );
    } else if (ch.kind === 'changed' && ch.before && ch.after) {
      rows.push(
        <div className="diff-row" key={`c${i}`}>
          <Tag color="orange">修改</Tag>
          <span className="diff-before">{condText(ch.before, metrics, true)}</span>
          <span className="diff-arrow">→</span>
          <span>{condText(ch.after, metrics, false)}</span>
          <span className="muted">（{ch.fields.map((f) => FIELD_LABEL[f] ?? f).join('、')}）</span>
        </div>,
      );
    }
  });
  if (!rows.length) return <Typography.Text type="secondary">新策略与当前条件在判定上没有差异。</Typography.Text>;
  return <div>{rows}</div>;
}

/** 一句话修改：/api/parse(带 base_strategy) → 变更说明与新旧对比 → 用户确认后应用 */
export function ModifyByText() {
  const strategy = useStore((s) => s.strategy);
  const strategyVersion = useStore((s) => s.strategyVersion);
  const applyModification = useStore((s) => s.applyModification);
  const metrics = useMetrics();
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ r: ParseResult; base: Strategy; version: number; text: string } | null>(null);

  if (!strategy) return null;

  const submit = async () => {
    const q = text.trim();
    if (!q || loading) return;
    setLoading(true);
    setError(null);
    setPending(null);
    const base = strategy;
    const version = strategyVersion;
    try {
      const r = await api.parse({ query: q, base_strategy: base });
      setPending({ r, base, version, text: q });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  const stale = pending ? pending.version !== strategyVersion : false;
  const next = pending?.r.strategy ?? null;
  const condLabelById = (id: string) => {
    const c = next?.conditions?.find((x) => x.id === id) ?? strategy.conditions?.find((x) => x.id === id);
    return c ? conditionLabel(c, metrics[c.metric]) : id;
  };

  return (
    <div className="modify-box">
      <div className="section-title">
        <EditOutlined /> 一句话修改 <span className="muted">（AI 生成修改草案，确认后才生效）</span>
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPressEnter={(e) => {
            if (e.nativeEvent.isComposing) return;
            void submit();
          }}
          placeholder="例如：估值放宽到行业 70% 分位、去掉回撤条件"
          maxLength={300}
          aria-label="一句话修改"
        />
        <Button type="primary" ghost loading={loading} disabled={!text.trim()} onClick={() => void submit()}>
          生成修改
        </Button>
      </Space.Compact>
      {error && <Alert style={{ marginTop: 8 }} type="error" showIcon message="生成修改失败" description={error} />}
      {pending && (
        <div style={{ marginTop: 10 }}>
          <Space size={6} wrap style={{ marginBottom: 6 }}>
            <Typography.Text strong>修改草案：</Typography.Text>
            <Typography.Text type="secondary">“{pending.text}”</Typography.Text>
            <ParserBadge r={pending.r} />
          </Space>
          {pending.r.compliance && (
            <Alert
              style={{ marginBottom: 8 }}
              type={pending.r.compliance.level === 'block' ? 'error' : 'warning'}
              showIcon
              message="合规提示"
              description={pending.r.compliance.message}
            />
          )}
          {(pending.r.changes ?? []).length > 0 && (
            <ul className="alert-list" style={{ marginBottom: 8 }}>
              {(pending.r.changes ?? []).map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
          {next ? (
            <div style={{ border: '1px dashed #d5dbe7', borderRadius: 6, padding: '6px 10px', background: '#fff' }}>
              <StrategyDiff base={pending.base} next={next} />
            </div>
          ) : (
            <Alert type="info" showIcon message="未能生成可应用的修改，请换一种说法。" />
          )}
          {(pending.r.unsupported ?? []).map((u, i) => (
            <Alert
              key={i}
              className="alert-gray"
              style={{ marginTop: 6 }}
              type="info"
              showIcon
              message={`暂不支持：“${u.phrase}”`}
              description={`${u.reason}${u.alternative ? `；可改为：${u.alternative}` : ''}`}
            />
          ))}
          {(pending.r.conflicts ?? []).filter((c) => c.source === 'ai').length > 0 && (
            <div style={{ marginTop: 6 }}>
              <ConflictList conflicts={(pending.r.conflicts ?? []).filter((c) => c.source === 'ai')} condLabel={condLabelById} />
            </div>
          )}
          {(pending.r.warnings ?? []).length > 0 && (
            <Alert
              style={{ marginTop: 6 }}
              type="warning"
              showIcon
              message="提示"
              description={
                <ul className="alert-list">
                  {(pending.r.warnings ?? []).map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}
          {stale && (
            <Alert
              style={{ marginTop: 6 }}
              type="warning"
              showIcon
              message="生成草案后条件又被编辑过，为避免覆盖你的编辑，请重新生成修改。"
            />
          )}
          <Space style={{ marginTop: 10 }}>
            <Button
              type="primary"
              icon={<CheckOutlined />}
              disabled={!next || stale}
              onClick={() => {
                if (!next) return;
                applyModification(next, pending.r.changes?.length ? pending.r.changes : [`按“${pending.text}”修改`]);
                setPending(null);
                setText('');
              }}
            >
              应用修改
            </Button>
            <Button icon={<CloseOutlined />} onClick={() => setPending(null)}>
              放弃
            </Button>
          </Space>
        </div>
      )}
    </div>
  );
}
