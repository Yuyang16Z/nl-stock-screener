import {
  AlertOutlined,
  BarChartOutlined,
  LineChartOutlined,
  ReloadOutlined,
  SaveOutlined,
  SwapOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Space, Tabs, Tag, Tooltip, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useStore } from '../store';
import type { ScreenResult, Strategy } from '../types';
import { faultLabel, fmtDateTime } from '../utils/format';
import { strategySignature } from '../utils/strategy';
import { BacktestDrawer } from './BacktestDrawer';
import { CompareModal } from './CompareModal';
import { DataProvenance, DatasetTable } from './DataProvenance';
import { ImpactPanel, type OpenStock } from './ImpactPanel';
import { MonitorModal } from './MonitorModal';
import { ExcludedTable, IncludedTable, MissingTable } from './ResultTables';
import { SaveStrategyModal } from './SaveStrategyModal';
import { StockDrawer } from './StockDrawer';
import { SummaryCard } from './SummaryCard';

function FaultDemoBanner({ result }: { result: ScreenResult }) {
  const sim = result.simulated_failures ?? [];
  if (!sim.length) return null;
  return (
    <Alert
      type="error"
      showIcon
      icon={<AlertOutlined />}
      message={<b>本次为故障演示</b>}
      description={`已模拟 ${sim.map((s) => `${faultLabel(s)}（${s}）`).join('、')} 接口失败。以下缺失与失败状态由演示注入产生，并非数据源真实故障；依赖这些数据集的条件被如实标为“缺失”，没有被当作通过。`}
    />
  );
}

interface DrawerState {
  code: string;
  runId: string;
  strategy: Strategy;
}

export function ResultSection() {
  const result = useStore((s) => s.result);
  const strategy = useStore((s) => s.strategy);
  const resultSignature = useStore((s) => s.resultSignature);
  const startRun = useStore((s) => s.startRun);
  const run = useStore((s) => s.run);

  const [selected, setSelected] = useState<string[]>([]);
  const [tab, setTab] = useState('included');
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [btOpen, setBtOpen] = useState(false);
  const [monOpen, setMonOpen] = useState(false);

  const runId = result?.run_id;
  useEffect(() => {
    setSelected([]);
    setDrawer(null);
  }, [runId]);

  const stale = useMemo(
    () => !!result && resultSignature !== strategySignature(strategy),
    [result, resultSignature, strategy],
  );

  if (!result) return null;

  const running = !!run && (run.status === 'pending' || run.status === 'running');
  const openStock: OpenStock = (code, ctx) =>
    setDrawer({ code, runId: ctx?.runId ?? result.run_id, strategy: ctx?.strategy ?? result.strategy });
  const actionStrategy = strategy ?? result.strategy;

  if (result.status === 'failed') {
    return (
      <Card className="panel" title="筛选结果">
        <div className="panel-stack">
          <FaultDemoBanner result={result} />
          <Alert type="error" showIcon message="本次筛选失败" description={result.error || '未知错误'} />
          {(result.warnings ?? []).length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="运行提示"
              description={
                <ul className="alert-list">
                  {(result.warnings ?? []).map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}
          {(result.datasets ?? []).length > 0 && (
            <div>
              <div className="section-title">数据集状态</div>
              <DatasetTable datasets={result.datasets ?? []} />
            </div>
          )}
          <Space>
            <Button type="primary" icon={<ReloadOutlined />} onClick={() => void startRun()} disabled={running}>
              重新执行
            </Button>
          </Space>
        </div>
      </Card>
    );
  }

  const nIncluded = (result.included ?? []).length;
  const nExcluded = (result.excluded ?? []).length;
  const nMissing = (result.missing ?? []).length;
  const nFlagged = (result.included ?? []).filter((r) => r.flagged).length;

  return (
    <Card
      className="panel"
      title={
        <div className="result-head">
          <span>筛选结果</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            运行 <span className="mono">{result.run_id}</span> · {fmtDateTime(result.finished_at ?? result.created_at)}
          </Typography.Text>
        </div>
      }
      extra={
        <Space wrap size={6}>
          <Tooltip title="在“入选”表中勾选 2–5 只后比较">
            <Button
              icon={<SwapOutlined />}
              disabled={selected.length < 2 || selected.length > 5}
              onClick={() => setCompareOpen(true)}
            >
              比较所选{selected.length ? `（${selected.length}）` : ''}
            </Button>
          </Tooltip>
          <Button icon={<SaveOutlined />} onClick={() => setSaveOpen(true)}>
            保存策略
          </Button>
          <Button icon={<LineChartOutlined />} onClick={() => setBtOpen(true)}>
            回测
          </Button>
          <Button icon={<BarChartOutlined />} onClick={() => setMonOpen(true)}>
            转为监控
          </Button>
        </Space>
      }
    >
      <div className="panel-stack">
        <FaultDemoBanner result={result} />
        {stale && (
          <Alert
            type="info"
            showIcon
            message="当前条件已修改，下方结果仍对应上一次执行时的条件。"
            action={
              <Button size="small" type="primary" onClick={() => void startRun()} disabled={running}>
                重新执行筛选
              </Button>
            }
          />
        )}
        <SummaryCard result={result} />
        <Tabs
          className="result-tabs"
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'included',
              label: (
                <span>
                  入选 <Tag color="blue">{nIncluded}</Tag>
                </span>
              ),
              children: (
                <div>
                  <div className="table-toolbar">
                    <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                      每列为一个启用的硬条件：图标表示判定（✓通过 ✗未通过 ?缺失 —不适用），悬停查看阈值、分位、期间、来源与时点。
                      {nFlagged ? ` 其中 ${nFlagged} 只含缺失（已标注）。` : ''}
                    </Typography.Text>
                    {selected.length > 0 && (
                      <Space size={6}>
                        <Tag color="geekblue">已选 {selected.length} / 5</Tag>
                        <Button size="small" type="link" onClick={() => setSelected([])}>
                          清空
                        </Button>
                      </Space>
                    )}
                  </div>
                  <IncludedTable result={result} selected={selected} onSelect={setSelected} onOpen={(c) => openStock(c)} />
                </div>
              ),
            },
            {
              key: 'excluded',
              label: (
                <span>
                  排除 <Tag>{nExcluded}</Tag>
                </span>
              ),
              children: <ExcludedTable result={result} onOpen={(c) => openStock(c)} />,
            },
            {
              key: 'missing',
              label: (
                <span>
                  数据缺失 <Tag color={nMissing ? 'orange' : undefined}>{nMissing + nFlagged}</Tag>
                </span>
              ),
              children: <MissingTable result={result} onOpen={(c) => openStock(c)} />,
            },
            {
              key: 'impact',
              label: '条件影响',
              children: <ImpactPanel result={result} onOpen={openStock} />,
            },
            {
              key: 'provenance',
              label: '数据口径与时点',
              children: <DataProvenance result={result} />,
            },
          ]}
        />
      </div>

      <StockDrawer
        open={!!drawer}
        runId={drawer?.runId ?? null}
        thscode={drawer?.code ?? null}
        strategy={drawer?.strategy ?? null}
        onClose={() => setDrawer(null)}
      />
      <CompareModal open={compareOpen} runId={result.run_id} codes={selected} onClose={() => setCompareOpen(false)} />
      <SaveStrategyModal
        open={saveOpen}
        strategy={actionStrategy}
        result={result}
        stale={stale}
        onClose={() => setSaveOpen(false)}
      />
      <BacktestDrawer open={btOpen} strategy={actionStrategy} stale={stale} onClose={() => setBtOpen(false)} />
      <MonitorModal open={monOpen} strategy={actionStrategy} stale={stale} onClose={() => setMonOpen(false)} />
    </Card>
  );
}
