import { FolderOpenOutlined, QuestionCircleOutlined, StopOutlined, WarningOutlined } from '@ant-design/icons';
import { Alert, Card, Radio, Space, Tag, Tooltip, Typography } from 'antd';
import type { ReactNode } from 'react';
import { useMetrics, useStore } from '../store';
import type { ClarificationOption, ComplianceNotice, MetricDef, ParseIntent, ParseResult, Strategy } from '../types';
import { conditionLabel, describeCondition } from '../utils/format';
import { conditionsForInterpretation, universeName } from '../utils/strategy';
import { ConflictList } from './ConflictList';
import { ParserBadge } from './ParserBadge';

const CONF_META: Record<string, { label: string; color: string }> = {
  high: { label: '把握高', color: 'blue' },
  medium: { label: '把握中', color: 'gold' },
  low: { label: '把握低', color: 'orange' },
};

const INTENT_LABEL: Record<ParseIntent, string> = {
  screen: '选股',
  modify: '修改条件',
  advice_request: '荐股/买卖请求',
  prediction_request: '涨跌预测请求',
  out_of_scope: '超出可筛选范围',
};

/** 合规拦截时按 compliance.category 显示意图：后端把收益承诺也归为 prediction_request，直接用 intent 会标错 */
const COMPLIANCE_INTENT_LABEL: Partial<Record<ComplianceNotice['category'], string>> = {
  prediction: '涨跌预测请求',
  advice: '荐股/买卖建议请求',
  return_promise: '收益承诺请求',
};

function intentLabel(r: ParseResult): string {
  const c = r.compliance;
  // 只在被合规拦截（block，或 intent 已是预测/荐股类）时用类别；warn 级的弱预测措辞仍按原 intent（如“选股”）显示
  const byCategory =
    c && (c.level === 'block' || r.intent === 'prediction_request' || r.intent === 'advice_request')
      ? COMPLIANCE_INTENT_LABEL[c.category]
      : undefined;
  return byCategory ?? INTENT_LABEL[r.intent] ?? r.intent;
}

type MarkKind = 'interp' | 'unsupported' | 'compliance';
interface Mark {
  phrase: string;
  kind: MarkKind;
  tip: ReactNode;
}
interface Segment {
  text: string;
  mark?: Mark;
}

/** 把被识别的短语切分出来高亮；长短语优先，不重叠 */
export function buildSegments(text: string, marks: Mark[]): Segment[] {
  const ranges: { start: number; end: number; mark: Mark }[] = [];
  const sorted = marks.filter((m) => m.phrase).sort((a, b) => b.phrase.length - a.phrase.length);
  for (const m of sorted) {
    let from = 0;
    for (;;) {
      const idx = text.indexOf(m.phrase, from);
      if (idx < 0) break;
      const end = idx + m.phrase.length;
      if (!ranges.some((r) => idx < r.end && end > r.start)) {
        ranges.push({ start: idx, end, mark: m });
        break;
      }
      from = idx + 1;
    }
  }
  ranges.sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  let pos = 0;
  for (const r of ranges) {
    if (r.start > pos) out.push({ text: text.slice(pos, r.start) });
    out.push({ text: text.slice(r.start, r.end), mark: r.mark });
    pos = r.end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos) });
  return out;
}

function HighlightedQuery({ text, r }: { text: string; r: ParseResult }) {
  const marks: Mark[] = [
    ...(r.interpretations ?? []).map((it) => ({ phrase: it.phrase, kind: 'interp' as const, tip: it.meaning })),
    ...(r.unsupported ?? []).map((u) => ({ phrase: u.phrase, kind: 'unsupported' as const, tip: `暂不支持：${u.reason}` })),
    ...(r.compliance?.matched ?? []).map((w) => ({
      phrase: w,
      kind: 'compliance' as const,
      tip: r.compliance?.message ?? '合规提示',
    })),
  ];
  const segs = buildSegments(text, marks);
  return (
    <div>
      <div className="hl-query" aria-label="原句（已高亮被识别的短语）">
        {segs.map((s, i) =>
          s.mark ? (
            <Tooltip key={i} title={s.mark.tip}>
              <span className={`hl hl-${s.mark.kind}`}>{s.text}</span>
            </Tooltip>
          ) : (
            <span key={i}>{s.text}</span>
          ),
        )}
      </div>
      <div className="hl-legend">
        <span>
          <i style={{ background: '#e3ebff', borderBottom: '2px solid #1f4fd8' }} />
          已识别
        </span>
        <span>
          <i style={{ background: '#f1f3f5', border: '1px dashed #adb5bd' }} />
          暂不支持
        </span>
        <span>
          <i style={{ background: '#fff0f0', borderBottom: '2px solid #e03131' }} />
          合规提示
        </span>
      </div>
    </div>
  );
}

function optionPreview(o: ClarificationOption, metrics: Record<string, MetricDef>): string {
  const parts: string[] = [];
  if (o.universe) parts.push(`股票池：${universeName(o.universe)}`);
  for (const c of o.conditions ?? []) parts.push(describeCondition(c, metrics[c.metric]));
  return parts.join('；');
}

function LoadedInfo({ kind, name }: { kind: string; name?: string | null }) {
  if (kind === 'manual') {
    return (
      <Alert
        type="info"
        showIcon
        message="手动设置模式"
        description="已放入系统默认风控条件（剔除 ST、上市不满一年），请在右侧选择股票池并添加条件。也可以在上方输入一句话让 AI 起草。"
      />
    );
  }
  return (
    <Alert
      type="info"
      showIcon
      icon={<FolderOpenOutlined />}
      message={`已从${kind === 'monitor' ? '监控任务' : '「我的策略」'}载入${name ? `：${name}` : ''}`}
      description="该策略未经过本次解析。你可以直接在右侧检查、修改条件后执行，或在上方重新输入描述。"
    />
  );
}

export function UnderstandingPanel() {
  const r = useStore((s) => s.parseResult);
  const parsedQuery = useStore((s) => s.parsedQuery);
  const strategy = useStore((s) => s.strategy);
  const loadedFrom = useStore((s) => s.loadedFrom);
  const clarSelections = useStore((s) => s.clarSelections);
  const selectClarification = useStore((s) => s.selectClarification);
  const metrics = useMetrics();

  if (!r) {
    return (
      <Card className="panel" title="AI 理解">
        {loadedFrom ? (
          <LoadedInfo kind={loadedFrom.kind} name={loadedFrom.name} />
        ) : (
          <Typography.Text type="secondary">输入描述并点击“解析意图”后，这里会逐短语展示 AI 的理解。</Typography.Text>
        )}
      </Card>
    );
  }

  const interps = r.interpretations ?? [];
  const clars = r.clarifications ?? [];
  const unsupported = r.unsupported ?? [];
  const aiConflicts = (r.conflicts ?? []).filter((c) => c.source === 'ai');
  const warnings = r.warnings ?? [];
  const compliance = r.compliance;
  const defaultLabel = r.parser === 'llm' ? 'AI 默认' : '规则默认';
  const condLabelById = (id: string) => {
    const c = strategy?.conditions?.find((x) => x.id === id);
    return c ? conditionLabel(c, metrics[c.metric]) : id;
  };

  return (
    <Card
      className="panel"
      title={
        <Space size={8}>
          <span>AI 理解</span>
          <Tooltip title="AI/规则只提出条件草案并标注依据；条件是否合理由你在右侧确认，入选与否由引擎判定。">
            <QuestionCircleOutlined className="help-icon" />
          </Tooltip>
        </Space>
      }
      extra={
        <Space size={6} wrap>
          <Tag>{intentLabel(r)}</Tag>
          <ParserBadge r={r} />
        </Space>
      }
    >
      <div className="panel-stack">
        {compliance && (
          <Alert
            type={compliance.level === 'block' ? 'error' : 'warning'}
            showIcon
            message={compliance.level === 'block' ? '合规提示：无法按原意执行' : '合规提示'}
            description={
              <div>
                <div>{compliance.message}</div>
                {(compliance.matched ?? []).length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    触发词：
                    {(compliance.matched ?? []).map((w) => (
                      <Tag key={w} color="red">
                        {w}
                      </Tag>
                    ))}
                  </div>
                )}
                {compliance.level === 'block' &&
                  (strategy ? (
                    <div style={{ marginTop: 4 }}>
                      <b>已转为仅描述历史的数据条件</b>（不代表未来走势，不构成买卖依据），请在右侧检查后再决定是否执行。
                    </div>
                  ) : (
                    <div style={{ marginTop: 4 }}>
                      本产品不预测涨跌、不提供买卖建议。可以改为描述可观察的数据特征，例如“近60日涨跌幅为正、估值合理”。
                    </div>
                  ))}
              </div>
            }
          />
        )}

        {parsedQuery && <HighlightedQuery text={parsedQuery} r={r} />}

        {interps.length > 0 && (
          <div>
            <div className="section-title">
              逐短语理解 <span className="muted">（条件随右侧编辑实时更新）</span>
            </div>
            {interps.map((it, i) => {
              const conds = conditionsForInterpretation(it, strategy as Strategy | null);
              const conf = CONF_META[it.confidence ?? 'medium'] ?? CONF_META.medium;
              return (
                <div className="interp-item" key={`${it.phrase}-${i}`}>
                  <div className="interp-head">
                    <span className="phrase-chip">“{it.phrase}”</span>
                    <Tag color={conf.color}>{conf.label}</Tag>
                    <Tag>{it.origin === 'rule' ? '规则' : 'AI'}</Tag>
                  </div>
                  <div className="interp-meaning">{it.meaning}</div>
                  <div className="interp-conds">
                    {conds.length ? (
                      conds.map((c) => (
                        <Tooltip key={c.id} title={c.rationale || undefined}>
                          <Tag color="blue" className={c.enabled === false ? 'cond-chip disabled' : 'cond-chip'}>
                            {conditionLabel(c, metrics[c.metric])}
                            {c.enabled === false ? '（已停用）' : ''}
                            {!(c.hard ?? true) ? '（软）' : ''}
                          </Tag>
                        </Tooltip>
                      ))
                    ) : (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        （该短语当前没有对应条件：可能已被删除，或只影响股票池）
                      </Typography.Text>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {clars.length > 0 && (
          <div>
            <div className="section-title">
              需要你确认的口径 <span className="muted">（切换后右侧条件随之替换）</span>
            </div>
            {clars.map((clar) => (
              <div className="clar-card" key={clar.id}>
                <div className="clar-q">
                  <QuestionCircleOutlined /> {clar.question}
                </div>
                <Radio.Group
                  value={clarSelections[clar.id] ?? clar.default_option_id}
                  onChange={(e) => selectClarification(clar.id, e.target.value as string)}
                  disabled={!strategy}
                >
                  <Space direction="vertical" size={6}>
                    {clar.options.map((o) => (
                      <Radio value={o.id} key={o.id}>
                        <span className="clar-opt-label">{o.label}</span>
                        {o.id === clar.default_option_id && (
                          <Tag color="geekblue" className="tag-mini" style={{ marginInlineStart: 6 }}>
                            {defaultLabel}
                          </Tag>
                        )}
                        {o.description && <div className="clar-opt-desc">{o.description}</div>}
                        {optionPreview(o, metrics) && <div className="clar-opt-preview">{optionPreview(o, metrics)}</div>}
                      </Radio>
                    ))}
                  </Space>
                </Radio.Group>
              </div>
            ))}
          </div>
        )}

        {unsupported.length > 0 && (
          <div>
            <div className="section-title">暂不支持的说法</div>
            <Space direction="vertical" style={{ width: '100%' }} size={6}>
              {unsupported.map((u, i) => (
                <Alert
                  key={`${u.phrase}-${i}`}
                  className="alert-gray"
                  type="info"
                  showIcon
                  icon={<StopOutlined />}
                  message={`“${u.phrase}”`}
                  description={
                    <div style={{ fontSize: 12.5 }}>
                      <div>{u.reason}</div>
                      {u.alternative && <div style={{ marginTop: 2 }}>可改为：{u.alternative}</div>}
                    </div>
                  }
                />
              ))}
            </Space>
          </div>
        )}

        {aiConflicts.length > 0 && (
          <div>
            <div className="section-title">
              AI 提示的语义冲突 <span className="muted">（基于解析时的条件）</span>
            </div>
            <ConflictList conflicts={aiConflicts} condLabel={condLabelById} />
          </div>
        )}

        {warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            icon={<WarningOutlined />}
            message="解析提示"
            description={
              <ul className="alert-list">
                {warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            }
          />
        )}

        {!interps.length && !clars.length && !unsupported.length && !compliance && (
          <Typography.Text type="secondary">没有识别到可映射为数据条件的短语，可以换一种说法或手动添加条件。</Typography.Text>
        )}
      </div>
    </Card>
  );
}
