import { LoadingOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Progress, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { useStore } from '../store';
import { faultLabel, progressPercent } from '../utils/format';

/** 执行进度：每 1 秒轮询 lite 状态（轮询逻辑在 store.startRun） */
export function RunProgress() {
  const run = useStore((s) => s.run);
  const result = useStore((s) => s.result);
  const stopTracking = useStore((s) => s.stopTracking);
  const startRun = useStore((s) => s.startRun);
  const [now, setNow] = useState(() => Date.now());
  const active = !!run && (run.status === 'pending' || run.status === 'running');

  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);

  if (!run) return null;

  if (active) {
    const secs = Math.max(0, Math.round((now - run.startedAt) / 1000));
    return (
      <Card className="run-card" size="small">
        <div className="run-head">
          <LoadingOutlined style={{ color: '#1f4fd8' }} />
          <Typography.Text strong>正在执行筛选</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已用时 {secs} 秒{run.runId ? ` · 任务 ${run.runId}` : ''}
          </Typography.Text>
          {run.simulated.length > 0 && (
            <Tag color="error">故障演示：{run.simulated.map(faultLabel).join('、')}</Tag>
          )}
          <span className="spacer" />
          <Button size="small" onClick={stopTracking}>
            停止跟踪
          </Button>
        </div>
        <Progress percent={progressPercent(run.progress)} status="active" />
        <Typography.Text type="secondary">{run.progressText || '执行中…'}</Typography.Text>
        <div className="chart-note" style={{ marginTop: 4 }}>
          首次取数需逐股请求财务与 K 线，耗时与股票池规模相关；同一股票池 10 分钟内会复用数据快照。
        </div>
      </Card>
    );
  }

  if (run.status === 'failed' && !result) {
    return (
      <Alert
        type="error"
        showIcon
        message="筛选未能完成"
        description={run.error || '未知错误'}
        action={
          <Space direction="vertical">
            <Button size="small" type="primary" onClick={() => void startRun()}>
              重试
            </Button>
            <Button size="small" onClick={stopTracking}>
              关闭
            </Button>
          </Space>
        }
      />
    );
  }
  return null;
}
