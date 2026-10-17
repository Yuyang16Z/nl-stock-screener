/** 证据链展示：来源接口/字段/request_id/时点、派生公式与输入、逐条件判定。 */

import { Collapse, Tag } from 'antd';
import type { CondEval, Condition, DatasetStatus, Evidence, MetricDef, SourceRef } from '../types';
import {
  COND_STATUS_META,
  DATASET_STATUS_META,
  PROVIDER_LABEL,
  conditionLabel,
  fmtDateTime,
  fmtPlain,
  truncate,
} from '../utils/format';
import { CondStatusIcon } from './CondStatusIcon';

export function DatasetStatusTag({ status }: { status: DatasetStatus['status'] }) {
  const m = DATASET_STATUS_META[status] ?? DATASET_STATUS_META.skipped;
  return <Tag color={m.color}>{m.label}</Tag>;
}

function paramsText(p: Record<string, unknown> | undefined): string {
  if (!p) return '';
  const entries = Object.entries(p);
  if (!entries.length) return '';
  const text = entries
    .slice(0, 6)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(', ');
  return entries.length > 6 ? `${text} …` : text;
}

export function SourceLine({ s }: { s: SourceRef }) {
  const params = paramsText(s.params);
  return (
    <div className="source-line">
      <Tag className="tag-mini" color={s.provider === 'derived' ? 'purple' : 'blue'} style={{ marginInlineStart: 0 }}>
        {PROVIDER_LABEL[s.provider] ?? s.provider}
      </Tag>{' '}
      {s.endpoint && <span className="mono">{s.endpoint}</span>}
      {s.field && (
        <>
          {' · '}
          <span className="k">字段</span>
          <span className="mono">{s.field}</span>
        </>
      )}
      {params && (
        <>
          {' · '}
          <span className="k">参数</span>
          <span className="mono">{truncate(params, 120)}</span>
        </>
      )}
      {s.request_id && (
        <>
          {' · '}
          <span className="k">request_id</span>
          <span className="mono">{s.request_id}</span>
        </>
      )}
      {s.data_timestamp != null && (
        <>
          {' · '}
          <span className="k">数据时点</span>
          {fmtDateTime(s.data_timestamp)}
        </>
      )}
      {s.fetched_at && (
        <>
          {' · '}
          <span className="k">获取于</span>
          {fmtDateTime(s.fetched_at, true)}
        </>
      )}
      {s.cache_hit && (
        <>
          {' · '}
          <span className="k">缓存命中</span>
        </>
      )}
    </div>
  );
}

/** 递归展示证据及其派生输入 */
export function EvidenceView({
  ev,
  metrics,
  depth = 0,
}: {
  ev: Evidence;
  metrics: Record<string, MetricDef>;
  depth?: number;
}) {
  const name = metrics[ev.metric]?.name ?? ev.metric;
  const inputs = ev.inputs ?? [];
  return (
    <div className={depth ? 'evidence-inputs' : undefined}>
      <div className="eval-item-vals">
        <span>
          <span className="k">{name}</span>
          <b className="num">{ev.display || '--'}</b>
        </span>
        {ev.period && (
          <span>
            <span className="k">期间/窗口</span>
            {ev.period}
          </span>
        )}
        {ev.as_of && (
          <span>
            <span className="k">数据时点</span>
            {fmtDateTime(ev.as_of)}
          </span>
        )}
        {ev.note && (
          <span>
            <span className="k">口径</span>
            {ev.note}
          </span>
        )}
      </div>
      {(ev.sources ?? []).map((s, i) => (
        <SourceLine key={i} s={s} />
      ))}
      {ev.formula && (
        <div className="source-line">
          <span className="k">公式</span>
          {ev.formula}
        </div>
      )}
      {depth < 4 && inputs.map((inp, i) => <EvidenceView key={i} ev={inp} metrics={metrics} depth={depth + 1} />)}
    </div>
  );
}

/** 某条证据的首要来源（派生指标取其输入的来源） */
export function primarySources(ev: Evidence | null | undefined): SourceRef[] {
  if (!ev) return [];
  const direct = (ev.sources ?? []).filter((s) => s.provider !== 'derived' || s.endpoint);
  if (direct.length) return direct;
  return (ev.inputs ?? []).flatMap((i) => i.sources ?? []).slice(0, 3);
}

/** 表格单元格悬停：阈值、分位（参考组 n）、期间、来源接口与字段、时点 */
export function EvalTooltipContent({ ev, label }: { ev: CondEval; label: string }) {
  const evd = ev.evidence;
  const sources = primarySources(evd).slice(0, 2);
  const asOf = evd?.as_of ?? sources[0]?.data_timestamp ?? null;
  return (
    <div className="eval-tip">
      <div className="eval-tip-title">{label}</div>
      <div>
        <span className="k">判定</span>
        {COND_STATUS_META[ev.status]?.label ?? ev.status}
      </div>
      <div>
        <span className="k">数值</span>
        {ev.display || '--'}
      </div>
      {ev.threshold && (
        <div>
          <span className="k">阈值</span>
          {ev.threshold}
        </div>
      )}
      {ev.rank_pct != null && (
        <div>
          <span className="k">分位</span>
          {fmtPlain(ev.rank_pct, 1)}（参考组 {ev.rank_group_size ?? '?'} 只）
        </div>
      )}
      {evd?.period && (
        <div>
          <span className="k">期间</span>
          {evd.period}
        </div>
      )}
      {sources.map((s, i) => (
        <div key={i}>
          <span className="k">来源</span>
          {PROVIDER_LABEL[s.provider] ?? s.provider} {s.endpoint ?? ''}
          {s.field ? ` · ${s.field}` : ''}
        </div>
      ))}
      {asOf != null && (
        <div>
          <span className="k">时点</span>
          {fmtDateTime(asOf)}
        </div>
      )}
      {ev.reason && (
        <div>
          <span className="k">原因</span>
          {ev.reason}
        </div>
      )}
    </div>
  );
}

/** 个股抽屉中的单条件依据 */
export function EvalDetail({
  ev,
  cond,
  metrics,
}: {
  ev: CondEval;
  cond?: Condition;
  metrics: Record<string, MetricDef>;
}) {
  const m = cond ? metrics[cond.metric] : ev.evidence ? metrics[ev.evidence.metric] : undefined;
  const label = cond ? conditionLabel(cond, m) : ev.condition_id;
  const evd = ev.evidence;
  const inputs = evd?.inputs ?? [];
  const showDerived = !!evd && (evd.derived || !!evd.formula || inputs.length > 0);
  return (
    <div className="eval-item">
      <div className="eval-item-head">
        <CondStatusIcon status={ev.status} showText />
        <span>{label}</span>
        {cond && !(cond.hard ?? true) && <Tag>软条件</Tag>}
        {cond && cond.enabled === false && <Tag>已停用</Tag>}
        {m?.estimate && <Tag color="gold">估算</Tag>}
      </div>
      <div className="eval-item-vals">
        <span>
          <span className="k">数值</span>
          <b className="num">{ev.display || '--'}</b>
        </span>
        {ev.threshold && (
          <span>
            <span className="k">阈值</span>
            {ev.threshold}
          </span>
        )}
        {ev.rank_pct != null && (
          <span>
            <span className="k">分位</span>
            <span className="num">{fmtPlain(ev.rank_pct, 1)}</span>（参考组 {ev.rank_group_size ?? '?'} 只；0=最低，100=最高）
          </span>
        )}
        {evd?.period && (
          <span>
            <span className="k">期间/窗口</span>
            {evd.period}
          </span>
        )}
        {evd?.as_of && (
          <span>
            <span className="k">数据时点</span>
            {fmtDateTime(evd.as_of)}
          </span>
        )}
      </div>
      {ev.reason && (
        <div className={ev.status === 'missing' || ev.status === 'not_applicable' ? 'reason-line' : 'source-line'}>
          原因：{ev.reason}
        </div>
      )}
      {evd?.note && (
        <div className="source-line">
          <span className="k">口径提示</span>
          {evd.note}
        </div>
      )}
      {(evd?.sources ?? []).map((s, i) => (
        <SourceLine key={i} s={s} />
      ))}
      {!evd && ev.status !== 'skipped' && <div className="source-line muted">后端未返回该条件的证据明细。</div>}
      {showDerived && evd && (
        <Collapse
          ghost
          size="small"
          style={{ marginTop: 4 }}
          items={[
            {
              key: 'derived',
              label: `派生公式与输入${inputs.length ? `（${inputs.length} 项）` : ''}`,
              children: (
                <div>
                  {evd.formula && (
                    <div className="source-line">
                      <span className="k">公式</span>
                      {evd.formula}
                    </div>
                  )}
                  {inputs.map((inp, i) => (
                    <EvidenceView key={i} ev={inp} metrics={metrics} depth={1} />
                  ))}
                  {!evd.formula && !inputs.length && <div className="source-line muted">无派生输入。</div>}
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}
