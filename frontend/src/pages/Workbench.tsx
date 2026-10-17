import { ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Col, Row, Skeleton, Typography } from 'antd';
import { QueryInput } from '../components/QueryInput';
import { ResultSection } from '../components/ResultSection';
import { RunProgress } from '../components/RunProgress';
import { StrategyPanel } from '../components/StrategyPanel';
import { UnderstandingPanel } from '../components/UnderstandingPanel';
import { fuyaoUnavailable, useStore } from '../store';

function TopAlerts() {
  const health = useStore((s) => s.health);
  const healthError = useStore((s) => s.healthError);
  const refreshHealth = useStore((s) => s.refreshHealth);
  const healthLoading = useStore((s) => s.healthLoading);
  const catalogError = useStore((s) => s.catalogError);
  const loadCatalog = useStore((s) => s.loadCatalog);
  const ds = fuyaoUnavailable(healthError ? null : health);

  return (
    <>
      {healthError && !health && (
        <Alert
          type="error"
          showIcon
          message="无法连接后端服务"
          description={`${healthError}。解析、校验与筛选都需要后端，请确认后端已启动后重试。`}
          action={
            <Button size="small" icon={<ReloadOutlined />} loading={healthLoading} onClick={() => void refreshHealth()}>
              重新检测
            </Button>
          }
        />
      )}
      {ds.unavailable && (
        <Alert
          type="error"
          showIcon
          banner
          message={<b>数据源不可用，无法执行筛选</b>}
          description={`扶摇：${ds.reason ?? '状态异常'}。自然语言解析与条件编辑仍可使用，数据源恢复后即可执行。`}
          action={
            <Button size="small" icon={<ReloadOutlined />} loading={healthLoading} onClick={() => void refreshHealth()}>
              重新检测
            </Button>
          }
        />
      )}
      {catalogError && (
        <Alert
          type="warning"
          showIcon
          message="指标目录加载失败"
          description={`${catalogError}。条件编辑器需要指标目录（名称、单位、可用算子），请重试。`}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadCatalog()}>
              重试
            </Button>
          }
        />
      )}
    </>
  );
}

/** 编辑区：左「AI 理解」+ 右「筛选条件」；合规阻断且无条件时不展示条件编辑 */
function EditArea() {
  const parseResult = useStore((s) => s.parseResult);
  const strategy = useStore((s) => s.strategy);
  const loadedFrom = useStore((s) => s.loadedFrom);
  const parsing = useStore((s) => s.parsing);
  const parseError = useStore((s) => s.parseError);
  const catalogLoading = useStore((s) => s.catalogLoading);

  if (parsing && !parseResult) {
    return (
      <Card className="panel">
        <Skeleton active paragraph={{ rows: 6 }} />
        <Typography.Text type="secondary">正在解析：AI 只会使用指标目录中的指标，输出经后端校验后展示…</Typography.Text>
      </Card>
    );
  }
  if (!parseResult && !strategy && !loadedFrom) {
    return parseError ? null : catalogLoading ? <Skeleton active paragraph={{ rows: 2 }} /> : null;
  }

  return (
    <Row gutter={[16, 16]} align="stretch">
      <Col xs={24} xl={9}>
        <UnderstandingPanel />
      </Col>
      <Col xs={24} xl={15}>
        {strategy ? (
          <StrategyPanel />
        ) : (
          <Card className="panel" title="筛选条件">
            <Alert
              type={parseResult?.compliance?.level === 'block' ? 'error' : 'info'}
              showIcon
              message={parseResult?.compliance?.level === 'block' ? '该请求不能转化为筛选条件' : '没有生成筛选条件'}
              description={
                parseResult?.compliance?.level === 'block'
                  ? '本产品只按可观察的历史数据做条件筛选，不预测涨跌、不提供买卖建议。请改为描述数据特征，例如“近60日涨跌幅为正、估值合理、负债率低”。'
                  : '可以换一种说法重新解析，或点击输入框下方的“跳过解析，手动设置条件”。'
              }
            />
          </Card>
        )}
      </Col>
    </Row>
  );
}

export default function Workbench() {
  const parseError = useStore((s) => s.parseError);
  return (
    <div className="panel-stack">
      <TopAlerts />
      <QueryInput />
      {parseError && <Alert type="error" showIcon message="解析失败" description={parseError} closable />}
      <EditArea />
      <RunProgress />
      <ResultSection />
    </div>
  );
}
