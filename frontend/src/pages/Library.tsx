import {
  DeleteOutlined,
  DownloadOutlined,
  ImportOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Alert, App, Button, Card, Empty, Popconfirm, Popover, Space, Table, Tabs, Tag, Tooltip, Typography, Upload } from 'antd';
import type { TableColumnsType } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, errorMessage, sleep } from '../api';
import { useMetrics, useStore } from '../store';
import type { MetricDef, Monitor, MonitorRun, ResultDiff, SavedStrategy, Strategy } from '../types';
import { JOB_STATUS_META, UNIVERSE_KIND_LABEL, conditionLabel, fmtDateTime, fromNow } from '../utils/format';
import { buildExport, downloadJson, parseImported, safeFileName } from '../utils/io';
import { normalizeUniverse, universeName } from '../utils/strategy';

function ConditionSummary({ s, metrics }: { s: Strategy; metrics: Record<string, MetricDef> }) {
  const conds = (s.conditions ?? []).filter((c) => c.enabled ?? true);
  if (!conds.length) return <span className="muted">无启用条件</span>;
  const labels = conds.map((c) => `${conditionLabel(c, metrics[c.metric])}${c.hard === false ? '（软）' : ''}`);
  const head = labels.slice(0, 3).join('；');
  return (
    <Tooltip
      title={
        <ul className="alert-list" style={{ fontSize: 12 }}>
          {labels.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      }
    >
      <span style={{ fontSize: 12.5 }}>
        {head}
        {labels.length > 3 ? ` 等 ${labels.length} 条` : ''}
      </span>
    </Tooltip>
  );
}

function UniverseCell({ s }: { s: Strategy }) {
  const u = normalizeUniverse(s.universe);
  return (
    <Space size={4}>
      <span>{universeName(u)}</span>
      <Tag className="tag-mini">{UNIVERSE_KIND_LABEL[u.kind ?? 'index']}</Tag>
    </Space>
  );
}

// ───────────────────────────── 已保存策略 ─────────────────────────────

function SavedStrategiesTab() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const loadStrategy = useStore((s) => s.loadStrategy);
  const metrics = useMetrics();
  const [list, setList] = useState<SavedStrategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setList(await api.listStrategies());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onLoad = (s: SavedStrategy) => {
    loadStrategy(s.strategy, { kind: 'library', name: s.name });
    message.success(`已载入「${s.name}」到选股工作台`);
    navigate('/');
  };

  const onDelete = async (s: SavedStrategy) => {
    if (!s.id) return;
    try {
      await api.deleteStrategy(s.id);
      setList((l) => l.filter((x) => x.id !== s.id));
      message.success(`已删除「${s.name}」`);
    } catch (e) {
      message.error(`删除失败：${errorMessage(e)}`);
    }
  };

  const onImport = async (file: File) => {
    setImporting(true);
    try {
      let data: unknown;
      try {
        data = JSON.parse(await file.text());
      } catch {
        message.error('导入失败：文件不是合法的 JSON');
        return;
      }
      const r = parseImported(data, file.name);
      if ('error' in r) {
        message.error(`导入失败：${r.error}`);
        return;
      }
      const saved = await api.createStrategy(r.saved);
      message.success(`已导入「${saved?.name ?? r.saved.name}」`);
      void load();
    } catch (e) {
      message.error(`导入失败：${errorMessage(e)}`);
    } finally {
      setImporting(false);
    }
  };

  const columns: TableColumnsType<SavedStrategy> = [
    {
      title: '名称',
      key: 'name',
      width: 220,
      render: (_, s) => (
        <div>
          <Typography.Text strong>{s.name}</Typography.Text>
          {s.note && (
            <div className="muted" style={{ fontSize: 12 }}>
              {s.note}
            </div>
          )}
        </div>
      ),
    },
    { title: '股票池', key: 'universe', width: 180, render: (_, s) => <UniverseCell s={s.strategy} /> },
    { title: '条件摘要', key: 'conds', render: (_, s) => <ConditionSummary s={s.strategy} metrics={metrics} /> },
    {
      title: '创建时间',
      key: 'created',
      width: 150,
      render: (_, s) => <span className="num">{fmtDateTime(s.created_at)}</span>,
    },
    {
      title: '上次入选',
      key: 'last',
      width: 130,
      align: 'right',
      render: (_, s) =>
        s.last_included != null ? (
          <div>
            <span className="num">{s.last_included} 只</span>
            {s.last_trade_date && (
              <div className="muted" style={{ fontSize: 12 }}>
                截至 {s.last_trade_date}
              </div>
            )}
          </div>
        ) : (
          <span className="muted">未记录</span>
        ),
    },
    {
      title: '操作',
      key: 'op',
      width: 236,
      fixed: 'right',
      render: (_, s) => (
        <Space size={4} wrap>
          <Button size="small" type="primary" ghost icon={<ImportOutlined />} onClick={() => onLoad(s)}>
            载入到工作台
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => downloadJson(`${safeFileName(s.name)}.json`, buildExport(s))}
          >
            导出 JSON
          </Button>
          <Popconfirm
            title={`删除「${s.name}」？`}
            description="删除后无法恢复（可先导出 JSON 备份）。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => onDelete(s)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} aria-label="删除策略" />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="table-toolbar">
        <Upload
          accept=".json,application/json"
          showUploadList={false}
          beforeUpload={(file) => {
            void onImport(file);
            return false;
          }}
        >
          <Button icon={<UploadOutlined />} loading={importing}>
            导入 JSON
          </Button>
        </Upload>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          导入支持本产品导出的文件，或包含 universe / conditions 的策略 JSON。
        </Typography.Text>
      </div>
      {error && (
        <Alert
          style={{ marginBottom: 12 }}
          type="error"
          showIcon
          message="策略列表加载失败"
          description={error}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      )}
      <Table<SavedStrategy>
        rowKey={(s) => s.id || s.name}
        size="middle"
        loading={loading}
        columns={columns}
        dataSource={list}
        pagination={list.length > 20 ? { pageSize: 20 } : false}
        scroll={{ x: 1080 }}
        locale={{
          emptyText: (
            <Empty description="还没有保存的策略：在工作台执行筛选后点击“保存策略”，或导入 JSON。" />
          ),
        }}
      />
    </div>
  );
}

// ───────────────────────────── 监控任务 ─────────────────────────────

function newestRun(runs: MonitorRun[]): MonitorRun | undefined {
  return [...runs].sort((a, b) => (b.run_at ?? '').localeCompare(a.run_at ?? ''))[0];
}

const UNRELIABLE_DIFF_TEXT = '本次数据不完整，变化未必可靠';

/** 数据不完整时的对比说明：后端给出的 changes_text + 本次 data_alerts */
function UnreliableDiffNotes({ diff, alerts }: { diff: ResultDiff | null | undefined; alerts: string[] }) {
  const changes = diff?.changes_text ?? [];
  return (
    <div style={{ maxWidth: 480, fontSize: 12.5 }}>
      {changes.length > 0 && (
        <ul className="alert-list" style={{ marginBottom: 6 }}>
          {changes.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      )}
      {alerts.length > 0 && (
        <>
          <div className="muted">数据告警：</div>
          <ul className="alert-list" style={{ marginBottom: 0 }}>
            {alerts.map((a, i) => (
              <li key={i} className="text-error">
                {a}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function DiffCell({ diff, alerts = [] }: { diff: ResultDiff | null | undefined; alerts?: string[] }) {
  if (!diff) return <span className="muted">首次运行（无对比）</span>;
  const added = diff.added ?? [];
  const removed = diff.removed ?? [];
  const unreliable = alerts.length > 0;
  if (!added.length && !removed.length) {
    if (unreliable) {
      // 数据集部分失败/失败时，“没有新增/移除”不代表名单真的没变：不写“无变化”，改为警示并给出原因
      return (
        <div>
          <Popover title="与上次运行相比" content={<UnreliableDiffNotes diff={diff} alerts={alerts} />}>
            <Tag color="warning" icon={<WarningOutlined />} style={{ cursor: 'help', marginInlineEnd: 0 }}>
              {UNRELIABLE_DIFF_TEXT}
            </Tag>
          </Popover>
          {(diff.changes_text ?? []).length > 0 && (
            <ul className="alert-list muted" style={{ fontSize: 12, margin: '4px 0 0' }}>
              {(diff.changes_text ?? []).map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ul>
          )}
        </div>
      );
    }
    return <span className="muted">入选名单无变化（不变 {diff.unchanged ?? 0} 只）</span>;
  }
  return (
    <Popover
      title="与上次运行相比"
      content={
        <div style={{ maxWidth: 480, maxHeight: 360, overflow: 'auto', fontSize: 12.5 }}>
          {unreliable && (
            <div className="text-error" style={{ marginBottom: 4 }}>
              <WarningOutlined /> {UNRELIABLE_DIFF_TEXT}
            </div>
          )}
          {unreliable ? (
            <div style={{ marginBottom: 6 }}>
              <UnreliableDiffNotes diff={diff} alerts={alerts} />
            </div>
          ) : (
            (diff.changes_text ?? []).length > 0 && (
              <ul className="alert-list" style={{ marginBottom: 6 }}>
                {(diff.changes_text ?? []).map((t, i) => (
                  <li key={i}>{t}</li>
                ))}
              </ul>
            )
          )}
          {added.map((it) => (
            <div className="diff-item" key={`a-${it.thscode}`}>
              <Tag color="blue" className="tag-mini" style={{ marginInlineStart: 0 }}>
                新增
              </Tag>{' '}
              {it.name ?? it.thscode} <span className="mono muted">{it.thscode}</span>
              <div className="muted">{it.reason}</div>
            </div>
          ))}
          {removed.map((it) => (
            <div className="diff-item" key={`r-${it.thscode}`}>
              <Tag className="tag-mini" style={{ marginInlineStart: 0 }}>
                移除
              </Tag>{' '}
              {it.name ?? it.thscode} <span className="mono muted">{it.thscode}</span>
              <div className="muted">{it.reason}</div>
            </div>
          ))}
        </div>
      }
    >
      <Space size={4} wrap style={{ cursor: 'help' }}>
        {unreliable && (
          <Tag color="warning" icon={<WarningOutlined />}>
            {UNRELIABLE_DIFF_TEXT}
          </Tag>
        )}
        <Tag color="blue">新增 {added.length}</Tag>
        <Tag>移除 {removed.length}</Tag>
        <span className="muted" style={{ fontSize: 12 }}>
          查看原因
        </span>
      </Space>
    </Popover>
  );
}

function AlertsCell({ alerts, error }: { alerts: string[]; error?: string | null }) {
  if (!alerts.length && !error) return <span className="muted">无</span>;
  return (
    <div style={{ fontSize: 12.5 }}>
      {alerts.map((a, i) => (
        <div key={i} className="text-error">
          ● {a}
        </div>
      ))}
      {error && <div className="text-error">● 运行错误：{error}</div>}
    </div>
  );
}

function RunsTable({ runs }: { runs: MonitorRun[] }) {
  const sorted = [...runs].sort((a, b) => (b.run_at ?? '').localeCompare(a.run_at ?? ''));
  const columns: TableColumnsType<MonitorRun> = [
    { title: '运行时间', key: 'at', width: 160, render: (_, r) => <span className="num">{fmtDateTime(r.run_at, true)}</span> },
    {
      title: '状态',
      key: 'status',
      width: 90,
      render: (_, r) => <Tag color={JOB_STATUS_META[r.status]?.color}>{JOB_STATUS_META[r.status]?.label ?? r.status}</Tag>,
    },
    {
      title: '入选数',
      key: 'n',
      width: 80,
      align: 'right',
      render: (_, r) => <span className="num">{r.included_count ?? '--'}</span>,
    },
    { title: '交易日', key: 'td', width: 110, render: (_, r) => r.trade_date ?? '--' },
    { title: '新增 / 移除', key: 'diff', render: (_, r) => <DiffCell diff={r.diff} alerts={r.data_alerts ?? []} /> },
    {
      title: '数据告警',
      key: 'alerts',
      width: 280,
      render: (_, r) => <AlertsCell alerts={r.data_alerts ?? []} error={r.error} />,
    },
  ];
  return (
    <Table<MonitorRun>
      rowKey={(r) => `${r.run_at}-${r.run_id ?? ''}`}
      size="small"
      columns={columns}
      dataSource={sorted}
      pagination={sorted.length > 10 ? { pageSize: 10 } : false}
      scroll={{ x: 900 }}
      locale={{ emptyText: '还没有运行记录' }}
    />
  );
}

function MonitorsTab() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const loadStrategy = useStore((s) => s.loadStrategy);
  const metrics = useMetrics();
  const [list, setList] = useState<Monitor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [runningIds, setRunningIds] = useState<Record<string, boolean>>({});
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setList(await api.listMonitors());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 立即运行：触发后轮询该监控，直到新运行出现并结束 */
  const runNow = async (m: Monitor) => {
    const id = m.id;
    if (!id) return;
    setRunningIds((r) => ({ ...r, [id]: true }));
    const before = (m.runs ?? []).length;
    try {
      const { run_id } = await api.runMonitor(id);
      const deadline = Date.now() + 10 * 60 * 1000;
      let failures = 0;
      let finished = false;
      while (alive.current && Date.now() < deadline) {
        await sleep(2000);
        let latest: Monitor;
        try {
          latest = await api.getMonitor(id);
          failures = 0;
        } catch (e) {
          failures += 1;
          if (failures >= 5) throw e;
          continue;
        }
        if (!alive.current) return;
        setList((l) => l.map((x) => (x.id === id ? latest : x)));
        const runs = latest.runs ?? [];
        const hit = runs.find((r) => !!r.run_id && r.run_id === run_id) ?? (runs.length > before ? newestRun(runs) : undefined);
        if (hit && (hit.status === 'done' || hit.status === 'failed')) {
          finished = true;
          if (hit.status === 'failed') message.warning(`「${m.name}」运行失败：${hit.error ?? '未知错误'}`);
          else if ((hit.data_alerts ?? []).length) message.warning(`「${m.name}」运行完成，但有 ${(hit.data_alerts ?? []).length} 条数据告警`);
          else message.success(`「${m.name}」运行完成：入选 ${hit.included_count ?? '--'} 只`);
          break;
        }
      }
      if (!finished && alive.current) message.info('运行仍在进行，可稍后点击“刷新”查看结果');
    } catch (e) {
      if (alive.current) message.error(`运行失败：${errorMessage(e)}`);
    } finally {
      if (alive.current) {
        setRunningIds((r) => {
          const n = { ...r };
          delete n[id];
          return n;
        });
      }
    }
  };

  const onDelete = async (m: Monitor) => {
    if (!m.id) return;
    try {
      await api.deleteMonitor(m.id);
      setList((l) => l.filter((x) => x.id !== m.id));
      message.success(`已删除监控「${m.name}」`);
    } catch (e) {
      message.error(`删除失败：${errorMessage(e)}`);
    }
  };

  const columns: TableColumnsType<Monitor> = [
    {
      title: '名称',
      key: 'name',
      width: 240,
      render: (_, m) => (
        <div>
          <Typography.Text strong>{m.name}</Typography.Text>
          <div style={{ fontSize: 12 }}>
            <UniverseCell s={m.strategy} />
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            <ConditionSummary s={m.strategy} metrics={metrics} />
          </div>
        </div>
      ),
    },
    {
      title: '频率',
      key: 'freq',
      width: 80,
      render: (_, m) => ((m.frequency ?? 'daily') === 'weekly' ? '每周' : '每日'),
    },
    {
      title: '上次运行',
      key: 'last',
      width: 150,
      render: (_, m) => (
        <div>
          <span className="num">{fmtDateTime(m.last_run_at)}</span>
          {m.last_run_at && (
            <div className="muted" style={{ fontSize: 12 }}>
              {fromNow(m.last_run_at)}
            </div>
          )}
        </div>
      ),
    },
    {
      title: '下次运行',
      key: 'next',
      width: 150,
      render: (_, m) => <span className="num">{fmtDateTime(m.next_run_at)}</span>,
    },
    {
      title: '启用',
      key: 'enabled',
      width: 80,
      render: (_, m) => ((m.enabled ?? true) ? <Tag color="success">已启用</Tag> : <Tag>已停用</Tag>),
    },
    {
      title: '最近一次',
      key: 'latest',
      width: 250,
      render: (_, m) => {
        const r = newestRun(m.runs ?? []);
        if (!r) return <span className="muted">尚未运行</span>;
        const runAlerts = r.data_alerts ?? [];
        const alerts = runAlerts.length;
        return (
          <Space size={4} wrap>
            <Tag color={JOB_STATUS_META[r.status]?.color}>{JOB_STATUS_META[r.status]?.label}</Tag>
            <span className="num">入选 {r.included_count ?? '--'}</span>
            {/* 有数据告警时不显示 “+0 / −0” 这类会被读成“无变化”的对比数 */}
            {r.diff && alerts === 0 && (
              <span className="muted" style={{ fontSize: 12 }}>
                +{r.diff.added?.length ?? 0} / −{r.diff.removed?.length ?? 0}
              </span>
            )}
            {alerts > 0 && (
              <Popover title="最近一次运行" content={<UnreliableDiffNotes diff={r.diff} alerts={runAlerts} />}>
                <Tag color="warning" icon={<WarningOutlined />} style={{ cursor: 'help' }}>
                  {UNRELIABLE_DIFF_TEXT}
                </Tag>
              </Popover>
            )}
            {alerts > 0 && <Tag color="error">数据告警 {alerts}</Tag>}
          </Space>
        );
      },
    },
    {
      title: '操作',
      key: 'op',
      width: 236,
      fixed: 'right',
      render: (_, m) => (
        <Space size={4} wrap>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<PlayCircleOutlined />}
            loading={!!(m.id && runningIds[m.id])}
            onClick={() => void runNow(m)}
          >
            {m.id && runningIds[m.id] ? '运行中' : '立即运行'}
          </Button>
          <Button
            size="small"
            icon={<ImportOutlined />}
            onClick={() => {
              loadStrategy(m.strategy, { kind: 'monitor', name: m.name });
              navigate('/');
            }}
          >
            载入策略
          </Button>
          <Popconfirm
            title={`删除监控「${m.name}」？`}
            description="历史运行记录将一并删除，无法恢复。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => onDelete(m)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} aria-label="删除监控" />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="table-toolbar">
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          监控按频率自动运行并与上次结果对比；数据集部分失败或失败时以红色“数据告警”标出，不会报告“无变化”。展开行可查看历次运行。
        </Typography.Text>
      </div>
      {error && (
        <Alert
          style={{ marginBottom: 12 }}
          type="error"
          showIcon
          message="监控列表加载失败"
          description={error}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      )}
      <Table<Monitor>
        rowKey={(m) => m.id || m.name}
        size="middle"
        loading={loading}
        columns={columns}
        dataSource={list}
        pagination={list.length > 20 ? { pageSize: 20 } : false}
        scroll={{ x: 1210 }}
        expandable={{
          expandedRowRender: (m) => <RunsTable runs={m.runs ?? []} />,
          rowExpandable: () => true,
        }}
        locale={{ emptyText: <Empty description="还没有监控任务：在工作台的结果区点击“转为监控”。" /> }}
      />
    </div>
  );
}

export default function Library() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'monitors' ? 'monitors' : 'strategies';
  return (
    <div className="panel-stack">
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          我的策略
        </Typography.Title>
        <Typography.Text type="secondary">保存的条件组合与监控任务。载入后可在工作台检查、修改并重新执行。</Typography.Text>
      </div>
      <Card className="panel">
        <Tabs
          activeKey={tab}
          onChange={(k) => setParams({ tab: k }, { replace: true })}
          items={[
            { key: 'strategies', label: '已保存策略', children: <SavedStrategiesTab /> },
            { key: 'monitors', label: '监控任务', children: <MonitorsTab /> },
          ]}
        />
      </Card>
    </div>
  );
}
