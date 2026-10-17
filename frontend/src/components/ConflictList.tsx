import { CloseCircleFilled, ExclamationCircleFilled, InfoCircleFilled } from '@ant-design/icons';
import { Tag } from 'antd';
import type { ReactNode } from 'react';
import type { Conflict, ConflictSeverity } from '../types';
import { CONFLICT_KIND_LABEL } from '../utils/format';

const SEVERITY: Record<ConflictSeverity, { label: string; icon: ReactNode; order: number }> = {
  error: { label: '错误', icon: <CloseCircleFilled />, order: 0 },
  warning: { label: '警告', icon: <ExclamationCircleFilled />, order: 1 },
  info: { label: '提示', icon: <InfoCircleFilled />, order: 2 },
};

/** 冲突列表：error 红 / warning 橙 / info 灰，含处理方式 */
export function ConflictList({ conflicts, condLabel }: { conflicts: Conflict[]; condLabel: (id: string) => string }) {
  if (!conflicts.length) return null;
  const sorted = [...conflicts].sort((a, b) => SEVERITY[a.severity].order - SEVERITY[b.severity].order);
  return (
    <div className="conflict-list">
      {sorted.map((c, i) => {
        const sev = SEVERITY[c.severity] ?? SEVERITY.info;
        const ids = c.condition_ids ?? [];
        return (
          <div key={`${c.id}-${i}`} className={`conflict conflict-${c.severity}`} role={c.severity === 'error' ? 'alert' : undefined}>
            <span className="conflict-icon" aria-label={sev.label}>
              {sev.icon}
            </span>
            <div className="conflict-body">
              <div>
                <b>
                  {sev.label} · {CONFLICT_KIND_LABEL[c.kind] ?? c.kind}
                </b>
                {c.source === 'ai' && (
                  <Tag color="geekblue" className="tag-mini" style={{ marginInlineStart: 6 }}>
                    AI 提示
                  </Tag>
                )}
                <span style={{ marginLeft: 6 }}>{c.message}</span>
              </div>
              {ids.length > 0 && (
                <div className="conflict-refs">
                  涉及条件：
                  {ids.map((id) => (
                    <Tag key={id}>{condLabel(id)}</Tag>
                  ))}
                </div>
              )}
              {c.suggestion && <div className="conflict-sugg">处理方式：{c.suggestion}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
