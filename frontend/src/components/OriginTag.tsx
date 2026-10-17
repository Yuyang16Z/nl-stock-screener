import { Tag, Tooltip } from 'antd';
import type { Condition, ConditionOrigin } from '../types';
import { ORIGIN_META } from '../utils/format';

/** 条件来源标签：AI / 规则 / 用户 / 系统；悬停显示来源短语与理由 */
export function OriginTag({ cond, editedFrom }: { cond: Condition; editedFrom?: ConditionOrigin }) {
  const origin = cond.origin ?? 'user';
  const meta = ORIGIN_META[origin] ?? ORIGIN_META.user;
  const tip = (
    <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 320 }}>
      <div>{meta.desc}</div>
      {editedFrom && <div>原由「{ORIGIN_META[editedFrom].label}」生成，已被你修改</div>}
      {cond.source_phrase && <div>来源短语：“{cond.source_phrase}”</div>}
      {cond.rationale && (
        <div>
          {editedFrom ? '修改前的理由' : '理由'}：{cond.rationale}
        </div>
      )}
    </div>
  );
  return (
    <Tooltip title={tip}>
      <Tag color={meta.color} className="origin-tag">
        {meta.label}
        {editedFrom ? '·改' : ''}
      </Tag>
    </Tooltip>
  );
}
