import { EditOutlined, InfoCircleOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Button, Card, Input, Tag, Typography } from 'antd';
import { useRef } from 'react';
import { useStore } from '../store';

/** 大输入框 + 示例 chips + 解析；回车提交（输入法组字时不提交），Shift+回车换行 */
export function QueryInput() {
  const query = useStore((s) => s.query);
  const setQuery = useStore((s) => s.setQuery);
  const parsing = useStore((s) => s.parsing);
  const parseQuery = useStore((s) => s.parseQuery);
  const startManual = useStore((s) => s.startManual);
  const catalog = useStore((s) => s.catalog);
  const composing = useRef(false);
  const examples = catalog?.examples ?? [];

  const submit = () => {
    if (!parsing && query.trim()) void parseQuery();
  };

  return (
    <Card className="query-card">
      <div className="query-title">用一句话描述你想找的股票</div>
      <div className="query-row">
        <Input.TextArea
          className="query-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="例如：经营改善、估值合理、走势相对稳定"
          autoSize={{ minRows: 2, maxRows: 5 }}
          maxLength={500}
          aria-label="选股描述"
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
          onPressEnter={(e) => {
            if (e.shiftKey || composing.current || e.nativeEvent.isComposing) return;
            e.preventDefault();
            submit();
          }}
        />
        <Button type="primary" size="large" icon={<ThunderboltOutlined />} loading={parsing} disabled={!query.trim()} onClick={submit}>
          解析意图
        </Button>
      </div>
      {examples.length > 0 && (
        <div className="examples">
          <span className="examples-label">试试：</span>
          {examples.map((ex) => (
            <Tag.CheckableTag
              key={ex}
              checked={false}
              className="example-chip"
              onChange={() => {
                if (parsing) return;
                setQuery(ex);
                void parseQuery(ex);
              }}
            >
              {ex}
            </Tag.CheckableTag>
          ))}
        </div>
      )}
      <div className="query-foot">
        <Typography.Text type="secondary">
          <InfoCircleOutlined /> AI 只把描述翻译成条件草案；是否入选由确定性引擎在真实数据上逐条判定。回车提交，Shift+回车换行。
        </Typography.Text>
        <Button type="link" size="small" icon={<EditOutlined />} onClick={startManual} disabled={!catalog}>
          跳过解析，手动设置条件
        </Button>
      </div>
    </Card>
  );
}
