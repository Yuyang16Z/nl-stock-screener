import { SafetyCertificateOutlined, WarningOutlined } from '@ant-design/icons';
import { Space, Tag, Tooltip, Typography } from 'antd';
import type { Explanation } from '../types';
import { SENTENCE_KIND_META } from '../utils/format';

/** 带“事实/推断/不确定/提示”标签的解释句子 + 数字校验徽标 */
export function ExplanationView({ exp }: { exp: Explanation }) {
  const verified = exp.verified ?? true;
  const notes = exp.verification_notes ?? [];
  return (
    <div className="explanation">
      <Space size={6} wrap>
        <Tag color={exp.generator === 'llm' ? 'geekblue' : 'default'}>
          {exp.generator === 'llm' ? 'AI 撰写（基于引擎证据）' : '模板生成（基于引擎证据）'}
        </Tag>
        {verified ? (
          <Tooltip title="文本中的每个数字都已与引擎证据（数值、阈值、计数）核对">
            <Tag color="success" icon={<SafetyCertificateOutlined />}>
              数字已校验
            </Tag>
          </Tooltip>
        ) : (
          <Tooltip title="部分数字未能与证据核对，请以表格中的数值为准">
            <Tag color="warning" icon={<WarningOutlined />}>
              数字未通过校验
            </Tag>
          </Tooltip>
        )}
        {notes.length > 0 && (
          <Tooltip
            title={
              <ul className="alert-list" style={{ fontSize: 12 }}>
                {notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            }
          >
            <Typography.Link style={{ fontSize: 12 }}>校验说明（{notes.length}）</Typography.Link>
          </Tooltip>
        )}
      </Space>
      {exp.sentences.length ? (
        <ul className="sentence-list">
          {exp.sentences.map((s, i) => {
            const meta = SENTENCE_KIND_META[s.kind] ?? SENTENCE_KIND_META.caveat;
            const hasNumber = /\d/.test(s.text);
            return (
              <li key={i}>
                <Tag color={meta.color}>{meta.label}</Tag>
                <span>
                  {s.text}
                  {verified && hasNumber && (
                    <Tooltip title="该句中的数字已与证据核对">
                      <SafetyCertificateOutlined className="sentence-verified" aria-label="数字已校验" />
                    </Tooltip>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <Typography.Text type="secondary">暂无解释内容。</Typography.Text>
      )}
    </div>
  );
}
