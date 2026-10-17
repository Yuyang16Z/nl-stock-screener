import { CheckCircleFilled, CloseCircleFilled, MinusCircleFilled, QuestionCircleFilled } from '@ant-design/icons';
import type { ReactNode } from 'react';
import type { CondStatus } from '../types';
import { COND_STATUS_META } from '../utils/format';

const ICONS: Record<CondStatus, ReactNode> = {
  pass: <CheckCircleFilled />,
  fail: <CloseCircleFilled />,
  missing: <QuestionCircleFilled />,
  not_applicable: <MinusCircleFilled />,
  skipped: <MinusCircleFilled />,
};

/** ✓绿 通过 / ✗红 未通过 / ?橙 缺失 / —灰 不适用：形状 + 文字，不只靠颜色 */
export function CondStatusIcon({ status, showText = false }: { status: CondStatus; showText?: boolean }) {
  const meta = COND_STATUS_META[status] ?? COND_STATUS_META.skipped;
  return (
    <span className="cond-status" style={{ color: meta.color }} role="img" aria-label={meta.label} title={showText ? undefined : meta.label}>
      {ICONS[status] ?? ICONS.skipped}
      {showText && <span className="cond-status-text">{meta.label}</span>}
    </span>
  );
}
