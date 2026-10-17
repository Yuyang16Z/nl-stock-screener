import {
  BugOutlined,
  CheckCircleOutlined,
  LoadingOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Alert, App, Button, Card, Checkbox, Collapse, Input, Radio, Segmented, Space, Tag, Tooltip, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { fuyaoUnavailable, useMetrics, useStore } from '../store';
import type { Conflict } from '../types';
import { AUTO_REPORT_RULE, FAULT_DATASETS, conditionLabel, faultLabel, isValidReport, reportLabel, uniformReportPeriod } from '../utils/format';
import { AddConditionSelect, ConditionTable } from './ConditionTable';
import { ConflictList } from './ConflictList';
import { ModifyByText } from './ModifyByText';
import { UniverseSelect } from './UniverseSelect';

function ReportPeriodControl({ value, onChange }: { value: string | null; onChange: (v: string | null) => void }) {
  const [mode, setMode] = useState<'auto' | 'manual'>(value ? 'manual' : 'auto');
  const [text, setText] = useState(value ?? '');
  useEffect(() => {
    if (value) {
      setMode('manual');
      setText(value);
    } else {
      setMode('auto');
    }
  }, [value]);
  const auto = uniformReportPeriod();
  const invalid = mode === 'manual' && !!text && !isValidReport(text);
  return (
    <div>
      <div className="rp-control">
        <Segmented
          size="small"
          value={mode}
          options={[
            { label: '自动', value: 'auto' },
            { label: '手动指定', value: 'manual' },
          ]}
          onChange={(v) => {
            const m = v as 'auto' | 'manual';
            setMode(m);
            if (m === 'auto') onChange(null);
            else if (isValidReport(text)) onChange(text.trim());
          }}
        />
        {mode === 'manual' ? (
          <>
            <Input
              size="small"
              value={text}
              placeholder="如 2026-3"
              status={invalid ? 'error' : undefined}
              style={{ width: 100 }}
              aria-label="报告期"
              onChange={(e) => {
                setText(e.target.value);
                if (isValidReport(e.target.value)) onChange(e.target.value.trim());
              }}
            />
            <span className={invalid ? 'text-error' : 'muted'} style={{ fontSize: 12 }}>
              {invalid ? '格式：年份-季度（1~4），如 2026-3' : text ? reportLabel(text) : '1=一季报 2=中报 3=三季报 4=年报'}
            </span>
          </>
        ) : (
          <span className="muted" style={{ fontSize: 12 }}>
            按规则推算为 {reportLabel(auto)}（{auto}），以执行结果为准
          </span>
        )}
        <Tooltip title={AUTO_REPORT_RULE}>
          <QuestionCircleOutlined className="help-icon" />
        </Tooltip>
      </div>
      {mode === 'auto' && (
        <div className="ctl-help">法定披露截止日：一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30；取“全部公司都应已披露”的最近一期。</div>
      )}
    </div>
  );
}

function DemoFaultsPanel() {
  const enabled = useStore((s) => s.health?.demo_faults_enabled ?? false);
  const selected = useStore((s) => s.simulateFailures);
  const setSelected = useStore((s) => s.setSimulateFailures);
  if (!enabled) return null;
  return (
    <Collapse
      size="small"
      items={[
        {
          key: 'demo',
          label: (
            <Space size={6}>
              <BugOutlined />
              <span>异常演示（故障注入）</span>
              {selected.length > 0 && <Tag color="error">已选 {selected.length} 项</Tag>}
            </Space>
          ),
          children: (
            <div>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12.5, marginBottom: 8 }}>
                勾选后，下一次“执行筛选”会让对应数据集返回模拟故障（只作用于该次运行），用来检验缺失与失败是否被如实展示、绝不被当作“通过”。
              </Typography.Paragraph>
              <Checkbox.Group
                value={selected}
                onChange={(v) => setSelected(v as string[])}
                options={FAULT_DATASETS.map((f) => ({ label: f.label, value: f.id }))}
              />
            </div>
          ),
        },
      ]}
    />
  );
}

function RunBar() {
  const { message } = App.useApp();
  const strategy = useStore((s) => s.strategy);
  const validation = useStore((s) => s.validation);
  const validationVersion = useStore((s) => s.validationVersion);
  const strategyVersion = useStore((s) => s.strategyVersion);
  const validating = useStore((s) => s.validating);
  const validationError = useStore((s) => s.validationError);
  const run = useStore((s) => s.run);
  const health = useStore((s) => s.health);
  const healthError = useStore((s) => s.healthError);
  const forceRefresh = useStore((s) => s.forceRefresh);
  const setForceRefresh = useStore((s) => s.setForceRefresh);
  const simulate = useStore((s) => s.simulateFailures);
  const startRun = useStore((s) => s.startRun);

  const current = !!validation && validationVersion === strategyVersion;
  const blockedByConflict = current && !validation.can_run;
  const ds = fuyaoUnavailable(healthError ? null : health);
  const running = !!run && (run.status === 'pending' || run.status === 'running');
  const demo = health?.demo_faults_enabled ?? false;

  const disabledReason = !strategy
    ? '请先解析描述或手动创建条件'
    : ds.unavailable
      ? `数据源不可用：${ds.reason ?? ''}`
      : blockedByConflict
        ? '存在错误级冲突，修正后才能执行'
        : null;

  const onRun = async () => {
    const outcome = await startRun();
    if (outcome === 'blocked') message.warning('存在错误级冲突，已阻止执行，请先修正条件');
  };

  return (
    <div className="run-bar">
      <Tooltip title={disabledReason ?? ''}>
        <Button
          type="primary"
          size="large"
          icon={<PlayCircleOutlined />}
          disabled={!!disabledReason || running}
          loading={running}
          onClick={() => void onRun()}
        >
          执行筛选
        </Button>
      </Tooltip>
      <Checkbox checked={forceRefresh} onChange={(e) => setForceRefresh(e.target.checked)}>
        强制刷新数据
        <Tooltip title="默认 10 分钟内复用同一股票池/报告期/交易日的数据快照；勾选后重新取数">
          <QuestionCircleOutlined className="help-icon" style={{ marginLeft: 4 }} />
        </Tooltip>
      </Checkbox>
      {demo && simulate.length > 0 && <Tag color="error">本次将模拟：{simulate.map(faultLabel).join('、')} 失败</Tag>}
      {disabledReason ? (
        <Typography.Text type="danger">{disabledReason}</Typography.Text>
      ) : validating ? (
        <Typography.Text type="secondary">
          <LoadingOutlined /> 正在校验条件…
        </Typography.Text>
      ) : validationError ? (
        <Typography.Text type="warning">
          <WarningOutlined /> 条件校验请求失败（{validationError}），执行时由后端再次校验
        </Typography.Text>
      ) : current ? (
        <Typography.Text type="secondary">
          <CheckCircleOutlined style={{ color: '#2f9e44' }} /> 条件已通过确定性校验
          {validation.conflicts.length ? `（${validation.conflicts.length} 条提示）` : ''}
        </Typography.Text>
      ) : null}
    </div>
  );
}

/** 右栏「筛选条件」 */
export function StrategyPanel() {
  const strategy = useStore((s) => s.strategy);
  const parseResult = useStore((s) => s.parseResult);
  const validation = useStore((s) => s.validation);
  const validationVersion = useStore((s) => s.validationVersion);
  const strategyVersion = useStore((s) => s.strategyVersion);
  const validating = useStore((s) => s.validating);
  const appliedChanges = useStore((s) => s.appliedChanges);
  const setUniverse = useStore((s) => s.setUniverse);
  const setReportPeriod = useStore((s) => s.setReportPeriod);
  const setMissingPolicy = useStore((s) => s.setMissingPolicy);
  const metrics = useMetrics();

  const conflicts: Conflict[] = useMemo(() => {
    if (validation) return validation.conflicts;
    return (parseResult?.conflicts ?? []).filter((c) => c.source !== 'ai');
  }, [validation, parseResult]);

  const { errorIds, warnIds } = useMemo(() => {
    const e = new Set<string>();
    const w = new Set<string>();
    for (const c of conflicts) {
      for (const id of c.condition_ids ?? []) {
        if (c.severity === 'error') e.add(id);
        else if (c.severity === 'warning') w.add(id);
      }
    }
    return { errorIds: e, warnIds: w };
  }, [conflicts]);

  if (!strategy) return null;

  const stale = !!validation && validationVersion !== strategyVersion;
  const errCount = conflicts.filter((c) => c.severity === 'error').length;
  const condLabelById = (id: string) => {
    const c = strategy.conditions?.find((x) => x.id === id);
    return c ? conditionLabel(c, metrics[c.metric]) : id;
  };
  const hardCount = (strategy.conditions ?? []).filter((c) => (c.enabled ?? true) && (c.hard ?? true)).length;

  return (
    <Card
      className="panel"
      title="筛选条件"
      extra={
        validating || stale ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            <LoadingOutlined /> 校验中
          </Typography.Text>
        ) : validation ? (
          errCount ? (
            <Tag color="error">{errCount} 个错误</Tag>
          ) : (
            <Tag color="success">可执行</Tag>
          )
        ) : null
      }
    >
      <div className="panel-stack">
        <div className="strategy-controls">
          <div>
            <div className="ctl-label">股票池</div>
            <UniverseSelect value={strategy.universe} onChange={setUniverse} />
            {strategy.universe?.kind === 'all_a' && (
              <div className="ctl-help">全A 模式：先用估值/行情等廉价条件预筛，剩余数量超过上限时会明确提示收窄条件，不会截断。</div>
            )}
            {strategy.universe?.query && !strategy.universe?.code && strategy.universe?.kind !== 'all_a' && (
              <div className="ctl-help text-error">“{strategy.universe.query}”尚未解析为具体板块，请从下拉中选择。</div>
            )}
          </div>
          <div>
            <div className="ctl-label">报告期</div>
            <ReportPeriodControl value={strategy.report_period ?? null} onChange={setReportPeriod} />
          </div>
          <div style={{ gridColumn: '1 / -1' }}>
            <div className="ctl-label">
              缺失策略
              <Tooltip title="硬条件的数据缺失、获取失败或不适用时如何处理。两种方式都不会把缺失当作“通过”。">
                <QuestionCircleOutlined className="help-icon" />
              </Tooltip>
            </div>
            <Space size={12} wrap>
              <Radio.Group
                size="small"
                optionType="button"
                value={strategy.missing_policy ?? 'exclude'}
                onChange={(e) => setMissingPolicy(e.target.value)}
                options={[
                  { label: '缺失即排除', value: 'exclude' },
                  { label: '保留并标注', value: 'keep_flagged' },
                ]}
              />
              <span className="ctl-help" style={{ marginTop: 0 }}>
                {(strategy.missing_policy ?? 'exclude') === 'exclude'
                  ? '数据缺失的股票单独列入“数据缺失”，不计入入选'
                  : '数据缺失的股票计入入选，但带“含缺失”标注'}
              </span>
            </Space>
          </div>
        </div>

        <div>
          <div className="section-title">
            条件明细 <span className="muted">（共 {strategy.conditions?.length ?? 0} 条，启用硬条件 {hardCount} 条；修改后自动校验）</span>
          </div>
          <ConditionTable errorIds={errorIds} warnIds={warnIds} />
          <div className="add-cond-row">
            <AddConditionSelect />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              指标只能来自指标目录；口径说明见结果页“数据口径与时点”。
            </Typography.Text>
          </div>
        </div>

        {conflicts.length > 0 && (
          <div>
            <div className="section-title">
              确定性校验结果 <span className="muted">（错误会阻止执行；警告与提示供参考）</span>
            </div>
            <ConflictList conflicts={conflicts} condLabel={condLabelById} />
          </div>
        )}
        {(validation?.warnings ?? []).length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="校验提示"
            description={
              <ul className="alert-list">
                {(validation?.warnings ?? []).map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            }
          />
        )}

        {appliedChanges.length > 0 && (
          <Alert
            type="info"
            showIcon
            message="已应用的一句话修改"
            description={
              <ul className="alert-list">
                {appliedChanges.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            }
          />
        )}

        <ModifyByText />
        <DemoFaultsPanel />
        <RunBar />
      </div>
    </Card>
  );
}
