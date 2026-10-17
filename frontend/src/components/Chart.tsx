/** ECharts 按需引入（只注册用到的图表与组件，控制包体积） */

import ReactEChartsCore from 'echarts-for-react/esm/core';
import { BarChart, FunnelChart, LineChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
} from 'echarts/components';
import * as echarts from 'echarts/core';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';

echarts.use([
  LineChart,
  BarChart,
  FunnelChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

export type ChartOption = EChartsOption;

export const CHART_COLORS = ['#1f4fd8', '#f08c00', '#7048e8', '#0c8599', '#c2255c', '#5c940d'];

export function Chart({
  option,
  height = 260,
  onEvents,
}: {
  option: ChartOption;
  height?: number;
  onEvents?: Record<string, (params: unknown) => void>;
}) {
  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      style={{ height, width: '100%' }}
      onEvents={onEvents}
    />
  );
}

/** tooltip formatter 中拼 HTML 时转义 */
export function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
