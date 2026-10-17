import { BulbOutlined, LinkOutlined, ReadOutlined } from '@ant-design/icons';
import { Alert, Button, Drawer, Empty, Skeleton, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api, errorMessage } from '../api';
import { useMetrics } from '../store';
import type { Explanation, MetricValue, NewsItem, NewsResult, StockDetail, Strategy } from '../types';
import { BUCKET_META, METRIC_STATUS_LABEL, fmtDateTime, fmtPlain, fmtWithUnit } from '../utils/format';
import { condMap } from '../utils/result';
import { Chart, esc, type ChartOption } from './Chart';
import { EvalDetail, SourceLine } from './Evidence';
import { ExplanationView } from './ExplanationView';

interface Props {
  open: boolean;
  runId: string | null;
  thscode: string | null;
  strategy: Strategy | null;
  onClose: () => void;
}

function PriceChart({ detail }: { detail: StockDetail }) {
  const prices = detail.prices ?? [];
  const option = useMemo<ChartOption>(() => {
    const first = prices[0]?.close;
    const last = prices[prices.length - 1]?.close;
    const up = first != null && last != null ? last >= first : true;
    return {
      grid: { left: 56, right: 18, top: 16, bottom: 50 },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: (ps: unknown) => {
          const p = (ps as { axisValue: string; data: number }[])[0];
          return p ? `${esc(p.axisValue)}<br/>收盘（后复权）${esc(fmtPlain(p.data, 2))}` : '';
        },
      },
      xAxis: {
        type: 'category',
        data: prices.map((p) => p.date),
        boundaryGap: false,
        axisLabel: { fontSize: 11, color: '#7a8499' },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 11, color: '#7a8499' },
        splitLine: { lineStyle: { color: '#eef1f6' } },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          type: 'line',
          data: prices.map((p) => p.close),
          showSymbol: false,
          // 区间整体颜色按 A 股习惯：上涨红、下跌绿
          lineStyle: { width: 1.6, color: up ? '#d9363e' : '#20a162' },
          areaStyle: { opacity: 0.06, color: up ? '#d9363e' : '#20a162' },
        },
      ],
    };
  }, [prices]);
  if (!prices.length) return <Empty description="没有价格数据（可能 K 线接口失败或未请求）" />;
  return <Chart option={option} height={260} />;
}

const NEWS_NOTE = '第三方资讯，仅作背景参考，可能含预测或评级，不代表本产品观点，数值口径可能与筛选数据不同；不参与筛选与打分';

/** 标题/摘要含预测、评级、荐股类措辞的资讯：标“含第三方观点” */
const THIRD_PARTY_VIEW_RE = /预测|评级|目标价|买入|增持|推荐|有望|看好|跑赢|首次覆盖|预计/;

function hasThirdPartyView(it: NewsItem): boolean {
  return THIRD_PARTY_VIEW_RE.test(`${it.title ?? ''} ${it.summary ?? ''}`);
}

function NewsBlock({ news, loading, error }: { news: NewsResult | null; loading: boolean; error: string | null }) {
  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (error) return <Alert type="error" showIcon message="资讯获取失败" description={error} />;
  if (!news) return null;
  if (news.status === 'disabled')
    return <Alert type="info" className="alert-gray" showIcon message="iFinD 未配置，资讯不可用" description={news.error || undefined} />;
  if (news.status === 'failed')
    return <Alert type="warning" showIcon message="iFinD 资讯获取失败" description={news.error || '未知错误'} />;
  if (news.status === 'empty' || !(news.items ?? []).length)
    return <Typography.Text type="secondary">暂无相关资讯。</Typography.Text>;
  return (
    <div>
      {(news.items ?? []).map((it, i) => (
        <div className="news-item" key={`${it.title}-${i}`}>
          <div>
            {hasThirdPartyView(it) && (
              <Tooltip title="标题或摘要含预测、评级或买卖类表述，属于第三方观点，不是本产品的结论">
                <Tag color="orange" className="tag-mini">
                  含第三方观点
                </Tag>
              </Tooltip>
            )}
            {it.url ? (
              <a href={it.url} target="_blank" rel="noopener noreferrer">
                {it.title} <LinkOutlined />
              </a>
            ) : (
              <b>{it.title}</b>
            )}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {[it.time, it.source].filter(Boolean).join(' · ')}
          </div>
          {it.summary && <div style={{ fontSize: 12.5, marginTop: 2 }}>{it.summary}</div>}
        </div>
      ))}
      {news.source && <SourceLine s={news.source} />}
    </div>
  );
}

/** 个股抽屉：逐条件依据、后复权价格走势、解读、iFinD 资讯 */
export function StockDrawer({ open, runId, thscode, strategy, onClose }: Props) {
  const metrics = useMetrics();
  const [detail, setDetail] = useState<StockDetail | null>(null);
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exp, setExp] = useState<Explanation | null>(null);
  const [expLoading, setExpLoading] = useState(false);
  const [expError, setExpError] = useState<string | null>(null);
  const [news, setNews] = useState<NewsResult | null>(null);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsError, setNewsError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !runId || !thscode) return;
    const ctrl = new AbortController();
    setDetail(null);
    setError(null);
    setExp(null);
    setExpError(null);
    setNews(null);
    setNewsError(null);
    setLoading(true);
    setNewsLoading(true);
    api
      .stockDetail(runId, thscode, { signal: ctrl.signal })
      .then((d) => {
        setDetail(d);
        setFetchedAt(new Date().toISOString());
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    api
      .stockNews(thscode, { signal: ctrl.signal })
      .then((n) => setNews(n))
      .catch((e) => {
        if (!ctrl.signal.aborted) setNewsError(errorMessage(e));
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setNewsLoading(false);
      });
    return () => ctrl.abort();
  }, [open, runId, thscode]);

  const explain = async () => {
    if (!runId || !thscode) return;
    setExpLoading(true);
    setExpError(null);
    try {
      setExp(await api.explainStock(runId, thscode));
    } catch (e) {
      setExpError(errorMessage(e));
    } finally {
      setExpLoading(false);
    }
  };

  const cmap = condMap(strategy);
  const evals = detail?.evaluations ?? [];
  const evalMetricIds = new Set(
    evals.map((e) => cmap.get(e.condition_id)?.metric ?? e.evidence?.metric).filter((x): x is string => !!x),
  );
  const otherMetrics = Object.values(detail?.metrics ?? {}).filter((mv) => !evalMetricIds.has(mv.metric));
  const info = detail?.info;
  const bucket = detail?.bucket ? BUCKET_META[detail.bucket] : null;

  const metricColumns: TableColumnsType<MetricValue> = [
    {
      title: '指标',
      key: 'name',
      render: (_, mv) => metrics[mv.metric]?.name ?? mv.metric,
    },
    {
      title: '数值',
      key: 'v',
      align: 'right',
      render: (_, mv) => (
        <span className="num">
          {mv.status === 'ok'
            ? mv.evidence?.display ?? fmtWithUnit(mv.value, metrics[mv.metric]?.unit ?? '')
            : METRIC_STATUS_LABEL[mv.status]}
        </span>
      ),
    },
    {
      title: '期间 / 时点',
      key: 'p',
      render: (_, mv) => (
        <span style={{ fontSize: 12 }}>
          {mv.evidence?.period ?? '--'}
          {mv.stale ? <Tag color="orange" className="tag-mini">数据时点偏旧</Tag> : null}
        </span>
      ),
    },
    {
      title: '说明',
      key: 'r',
      render: (_, mv) => <span className="muted" style={{ fontSize: 12 }}>{mv.reason ?? (mv.evidence?.note || '--')}</span>,
    },
  ];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={760}
      title={
        info ? (
          <Space size={8} wrap>
            <span>{info.name ?? info.thscode}</span>
            <span className="mono muted">{info.thscode}</span>
            {info.industry && <Tag>{info.industry}</Tag>}
            {bucket && <Tag color={bucket.color}>{bucket.label}</Tag>}
          </Space>
        ) : (
          <span>{thscode ?? '个股依据'}</span>
        )
      }
    >
      {loading && <Skeleton active paragraph={{ rows: 8 }} />}
      {error && <Alert type="error" showIcon message="个股依据获取失败" description={error} />}
      {detail && (
        <div className="panel-stack">
          <div>
            <div className="section-title">
              逐条件依据 <span className="muted">（数值、阈值、分位与参考组、期间、来源接口与字段、时点）</span>
            </div>
            {evals.length ? (
              evals.map((ev) => (
                <EvalDetail key={ev.condition_id} ev={ev} cond={cmap.get(ev.condition_id)} metrics={metrics} />
              ))
            ) : (
              <Typography.Text type="secondary">没有逐条件判定记录。</Typography.Text>
            )}
          </div>

          {otherMetrics.length > 0 && (
            <div>
              <div className="section-title">其他展示指标</div>
              <Table<MetricValue>
                rowKey="metric"
                size="small"
                pagination={false}
                columns={metricColumns}
                dataSource={otherMetrics}
              />
            </div>
          )}

          <div>
            <div className="section-title">
              价格走势 <span className="muted">（{detail.price_note || '后复权收盘价'}，仅描述历史）</span>
            </div>
            <PriceChart detail={detail} />
            {detail.price_source && <SourceLine s={detail.price_source} />}
          </div>

          <div>
            <div className="section-title">
              <BulbOutlined /> 解读
              <span className="muted">（只基于上方证据生成，区分事实 / 推断 / 不确定）</span>
            </div>
            {!exp && (
              <Button icon={<BulbOutlined />} loading={expLoading} onClick={() => void explain()}>
                生成解读
              </Button>
            )}
            {expError && <Alert style={{ marginTop: 8 }} type="error" showIcon message="解读生成失败" description={expError} />}
            {exp && <ExplanationView exp={exp} />}
          </div>

          <div>
            <div className="section-title">
              <ReadOutlined /> iFinD 资讯
            </div>
            <div className="chart-note" style={{ marginBottom: 6 }}>
              {NEWS_NOTE}
            </div>
            <NewsBlock news={news} loading={newsLoading} error={newsError} />
            {news?.note && (
              <div className="chart-note" style={{ marginTop: 4 }}>
                {news.note}
              </div>
            )}
          </div>

          <div className="chart-note">
            依据载入于 {fmtDateTime(fetchedAt, true)} · 运行 <span className="mono">{runId}</span>
          </div>
        </div>
      )}
    </Drawer>
  );
}

