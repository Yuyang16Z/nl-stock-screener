import { DeleteOutlined, PlusOutlined, QuestionCircleOutlined } from '@ant-design/icons';
import { Button, InputNumber, Select, Switch, Table, Tag, Tooltip } from 'antd';
import type { TableColumnsType } from 'antd';
import { useMemo, type ReactNode } from 'react';
import { useMetrics, useStore } from '../store';
import type { Condition, MetricDef, Op, OpDef, RankScope } from '../types';
import { CATEGORY_LABEL, CATEGORY_ORDER, RANK_SCOPE_LABEL, describeCondition } from '../utils/format';
import { condId, isBoolOp, isRankOp, patchForMetric, patchForOp } from '../utils/strategy';
import { OriginTag } from './OriginTag';

const FALLBACK_OPS: OpDef[] = [
  { op: 'gt', label: '大于', symbol: '>' },
  { op: 'gte', label: '不低于', symbol: '≥' },
  { op: 'lt', label: '小于', symbol: '<' },
  { op: 'lte', label: '不高于', symbol: '≤' },
  { op: 'between', label: '介于', symbol: '∈', needs_value2: true },
  { op: 'pct_rank_lte', label: '分位不高于', symbol: '分位≤', needs_scope: true },
  { op: 'pct_rank_gte', label: '分位不低于', symbol: '分位≥', needs_scope: true },
  { op: 'is_true', label: '是', symbol: '是' },
  { op: 'is_false', label: '否', symbol: '否' },
];

const RANK_SCOPE_SHORT: Record<RankScope, string> = { universe: '股票池', industry: '行业内', market: '全A' };

function toNum(v: string | number | null): number | null {
  if (v === null || v === '') return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function stepFor(m: MetricDef | undefined): number {
  const sv = m?.sweep_values ?? [];
  if (sv.length && sv.every((x) => Math.abs(x) < 10)) return 0.1;
  return 1;
}

interface MetricOption {
  value: string;
  label: ReactNode;
  search: string;
}

/** 按类别分组的指标下拉选项（显示单位与“估算”标记） */
export function useMetricOptions(metrics: Record<string, MetricDef>) {
  return useMemo(() => {
    const list = Object.values(metrics);
    return CATEGORY_ORDER.map((cat) => ({
      label: CATEGORY_LABEL[cat],
      title: CATEGORY_LABEL[cat],
      options: list
        .filter((m) => m.category === cat)
        .map<MetricOption>((m) => ({
          value: m.id,
          label: (
            <div className="metric-opt">
              <span>
                {m.name}
                {m.estimate && (
                  <Tag color="gold" className="tag-mini">
                    估算
                  </Tag>
                )}
              </span>
              <span className="metric-opt-meta">{m.unit || '无单位'}</span>
            </div>
          ),
          search: [m.name, m.id, ...(m.aliases ?? [])].join(' ').toLowerCase(),
        })),
    })).filter((g) => g.options.length > 0);
  }, [metrics]);
}

export function metricFilter(input: string, option?: unknown): boolean {
  const s = (option as { search?: string } | undefined)?.search;
  return typeof s === 'string' && s.includes(input.trim().toLowerCase());
}

function MetricSelect({
  value,
  metrics,
  onChange,
}: {
  value: string;
  metrics: Record<string, MetricDef>;
  onChange: (id: string) => void;
}) {
  const options = useMetricOptions(metrics);
  const m = metrics[value];
  return (
    <Select
      size="small"
      showSearch
      value={value}
      onChange={onChange}
      options={options}
      filterOption={metricFilter}
      style={{ width: '100%' }}
      popupMatchSelectWidth={320}
      aria-label="指标"
      labelRender={() =>
        m ? (
          <span>
            {m.name}
            {m.estimate && (
              <Tag color="gold" className="tag-mini">
                估算
              </Tag>
            )}
          </span>
        ) : (
          <span className="text-error">{value}（目录中不存在）</span>
        )
      }
    />
  );
}

function ValueEditor({
  c,
  m,
  opDefs,
  onPatch,
}: {
  c: Condition;
  m: MetricDef | undefined;
  opDefs: Record<string, OpDef>;
  onPatch: (p: Partial<Condition>) => void;
}) {
  const allowed: Op[] = m?.allowed_ops?.length ? m.allowed_ops : (Object.keys(opDefs) as Op[]);
  const ops = allowed.includes(c.op) ? allowed : [c.op, ...allowed];
  const unit = m?.unit ?? '';
  const step = stepFor(m);

  const opSelect = (
    <Select<Op>
      size="small"
      value={c.op}
      style={{ width: 84 }}
      popupMatchSelectWidth={false}
      aria-label="算子"
      onChange={(op) => onPatch(patchForOp(c, op, m))}
      options={ops.map((op) => ({
        value: op,
        label: `${opDefs[op]?.symbol ?? op} ${opDefs[op]?.label ?? ''}`,
      }))}
      labelRender={() => <span>{opDefs[c.op]?.symbol ?? c.op}</span>}
    />
  );

  if (isBoolOp(c.op)) {
    return (
      <div className="cond-editor">
        {opSelect}
        <span className="unit-suffix">无需阈值</span>
      </div>
    );
  }

  if (isRankOp(c.op)) {
    const scopes: RankScope[] = m?.rank_scopes?.length ? m.rank_scopes : ['universe'];
    return (
      <div className="cond-editor">
        <Select<RankScope>
          size="small"
          value={c.rank_scope ?? undefined}
          placeholder="参考组"
          status={c.rank_scope ? undefined : 'error'}
          style={{ width: 82 }}
          popupMatchSelectWidth={false}
          aria-label="分位口径（参考组）"
          onChange={(v) => onPatch({ rank_scope: v })}
          options={scopes.map((s) => ({ value: s, label: RANK_SCOPE_LABEL[s] }))}
          labelRender={() => <span>{c.rank_scope ? RANK_SCOPE_SHORT[c.rank_scope] : '参考组'}</span>}
        />
        {opSelect}
        <Tooltip title="分位：0=参考组内最低，100=最高（平均秩法）">
          <InputNumber
            size="small"
            value={c.value ?? null}
            onChange={(v) => onPatch({ value: toNum(v) })}
            style={{ width: 82 }}
            controls={false}
            step={5}
            suffix={<span className="unit-suffix">分位</span>}
            status={c.value == null ? 'error' : undefined}
            aria-label="分位阈值"
          />
        </Tooltip>
      </div>
    );
  }

  if (c.op === 'between') {
    return (
      <div className="cond-editor">
        {opSelect}
        <InputNumber
          size="small"
          value={c.value ?? null}
          onChange={(v) => onPatch({ value: toNum(v) })}
          style={{ width: 70 }}
          controls={false}
          step={step}
          status={c.value == null ? 'error' : undefined}
          aria-label="下界"
        />
        <span className="unit-suffix">~</span>
        <InputNumber
          size="small"
          value={c.value2 ?? null}
          onChange={(v) => onPatch({ value2: toNum(v) })}
          style={{ width: 70 }}
          controls={false}
          step={step}
          status={c.value2 == null ? 'error' : undefined}
          aria-label="上界"
        />
        {unit && <span className="unit-suffix">{unit}</span>}
      </div>
    );
  }

  return (
    <div className="cond-editor">
      {opSelect}
      <InputNumber
        size="small"
        value={c.value ?? null}
        onChange={(v) => onPatch({ value: toNum(v) })}
        style={{ width: 126 }}
        step={step}
        suffix={unit ? <span className="unit-suffix">{unit}</span> : undefined}
        status={c.value == null ? 'error' : undefined}
        aria-label="阈值"
      />
    </div>
  );
}

function HardSoftEditor({ c, onPatch }: { c: Condition; onPatch: (p: Partial<Condition>) => void }) {
  const hard = c.hard ?? true;
  return (
    <div>
      <Select
        size="small"
        value={hard ? 'hard' : 'soft'}
        style={{ width: 76 }}
        aria-label="硬条件或软条件"
        onChange={(v: string) => onPatch({ hard: v === 'hard' })}
        options={[
          { value: 'hard', label: '硬条件' },
          { value: 'soft', label: '软条件' },
        ]}
      />
      {!hard && (
        <Tooltip title="软条件权重：软分 = Σ(权重×是否满足) ÷ Σ权重">
          <InputNumber
            size="small"
            value={c.weight ?? 1}
            min={0.1}
            max={10}
            step={0.5}
            controls={false}
            prefix={<span className="unit-suffix">权重</span>}
            onChange={(v) => onPatch({ weight: toNum(v) ?? 1 })}
            style={{ width: 76, marginTop: 4 }}
            aria-label="软条件权重"
          />
        </Tooltip>
      )}
    </div>
  );
}

/** 可编辑条件表：指标/算子/阈值/口径/硬软/启用/来源/删除；修改即触发防抖校验 */
export function ConditionTable({ errorIds, warnIds }: { errorIds: Set<string>; warnIds: Set<string> }) {
  const strategy = useStore((s) => s.strategy);
  const ops = useStore((s) => s.catalog?.ops);
  const editedFrom = useStore((s) => s.editedFrom);
  const update = useStore((s) => s.updateCondition);
  const remove = useStore((s) => s.removeCondition);
  const metrics = useMetrics();
  const conds = strategy?.conditions ?? [];

  const opDefs = useMemo(
    () => Object.fromEntries((ops?.length ? ops : FALLBACK_OPS).map((o) => [o.op, o])) as Record<string, OpDef>,
    [ops],
  );

  const columns: TableColumnsType<Condition> = [
    {
      title: '启用',
      key: 'enabled',
      width: 48,
      align: 'center',
      render: (_, c) => (
        <Switch
          size="small"
          checked={c.enabled ?? true}
          onChange={(v) => update(condId(c), { enabled: v })}
          aria-label={c.enabled === false ? '启用该条件' : '停用该条件'}
        />
      ),
    },
    {
      title: '指标',
      key: 'metric',
      width: 168,
      render: (_, c) => (
        <Tooltip title={describeCondition(c, metrics[c.metric])} mouseEnterDelay={0.6}>
          <div>
            <MetricSelect
              value={c.metric}
              metrics={metrics}
              onChange={(mid) => {
                const m = metrics[mid];
                if (m && mid !== c.metric) update(condId(c), patchForMetric(c, m));
              }}
            />
          </div>
        </Tooltip>
      ),
    },
    {
      title: (
        <span>
          条件{' '}
          <Tooltip title="算子按指标允许的范围过滤；分位类条件需选择参考组（股票池内 / 所属行业内 / 全A股内）">
            <QuestionCircleOutlined className="help-icon" />
          </Tooltip>
        </span>
      ),
      key: 'cond',
      render: (_, c) => (
        <ValueEditor c={c} m={metrics[c.metric]} opDefs={opDefs} onPatch={(p) => update(condId(c), p)} />
      ),
    },
    {
      title: (
        <span>
          类型{' '}
          <Tooltip title="硬条件：不满足即排除（缺失按缺失策略处理）；软条件：不过滤，只参与软分排序">
            <QuestionCircleOutlined className="help-icon" />
          </Tooltip>
        </span>
      ),
      key: 'hard',
      width: 92,
      render: (_, c) => <HardSoftEditor c={c} onPatch={(p) => update(condId(c), p)} />,
    },
    {
      title: '来源',
      key: 'origin',
      width: 60,
      render: (_, c) => <OriginTag cond={c} editedFrom={editedFrom[condId(c)]} />,
    },
    {
      title: '',
      key: 'del',
      width: 40,
      align: 'center',
      render: (_, c) => (
        <Tooltip title="删除条件">
          <Button
            type="text"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() => remove(condId(c))}
            aria-label="删除条件"
          />
        </Tooltip>
      ),
    },
  ];

  return (
    <Table<Condition>
      className="cond-table"
      size="small"
      rowKey={(c) => condId(c)}
      columns={columns}
      dataSource={conds}
      pagination={false}
      scroll={{ x: 700 }}
      rowClassName={(c) => {
        const id = condId(c);
        const cls: string[] = [];
        if (c.enabled === false) cls.push('row-disabled');
        if (errorIds.has(id)) cls.push('row-error');
        else if (warnIds.has(id)) cls.push('row-warning');
        return cls.join(' ');
      }}
      locale={{ emptyText: '暂无条件，请从下方“添加条件”选择指标' }}
    />
  );
}

/** “添加条件”：先选指标，再按指标默认口径生成一行 */
export function AddConditionSelect() {
  const metrics = useMetrics();
  const add = useStore((s) => s.addCondition);
  const options = useMetricOptions(metrics);
  return (
    <Select
      showSearch
      value={null}
      placeholder={
        <span>
          <PlusOutlined /> 添加条件（选择指标）
        </span>
      }
      options={options}
      filterOption={metricFilter}
      onChange={(mid: string | null) => {
        if (mid) add(mid);
      }}
      style={{ width: 260 }}
      popupMatchSelectWidth={320}
      aria-label="添加条件"
      disabled={!Object.keys(metrics).length}
    />
  );
}
