import { RobotOutlined, ToolOutlined } from '@ant-design/icons';
import { Tag, Tooltip } from 'antd';
import type { ParseResult } from '../types';
import { truncate } from '../utils/format';

/** “DeepSeek 解析” 或 “规则解析（AI 不可用：原因）” */
export function ParserBadge({ r }: { r: ParseResult }) {
  if (r.parser === 'llm') {
    const name = !r.llm_model || /deepseek/i.test(r.llm_model) ? 'DeepSeek 解析' : `${r.llm_model} 解析`;
    return (
      <Tooltip title={`模型：${r.llm_model ?? '未知'}。AI 输出已经后端校验：只保留指标目录内的指标与合法算子。`}>
        <Tag color="geekblue" icon={<RobotOutlined />} style={{ marginInlineEnd: 0 }}>
          {name}
        </Tag>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={r.llm_error ? `AI 不可用原因：${r.llm_error}；已改用规则词典解析，主链路不受影响。` : '使用规则词典解析'}>
      <Tag color="orange" icon={<ToolOutlined />} style={{ marginInlineEnd: 0, whiteSpace: 'normal' }}>
        规则解析{r.llm_error ? `（AI 不可用：${truncate(r.llm_error, 28)}）` : ''}
      </Tag>
    </Tooltip>
  );
}
