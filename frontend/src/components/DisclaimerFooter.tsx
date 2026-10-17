import { SafetyCertificateOutlined } from '@ant-design/icons';
import { Layout } from 'antd';
import { useStore } from '../store';

/** 目录未加载时的兜底文案（与后端 catalog.DISCLAIMER 一致） */
export const FALLBACK_DISCLAIMER =
  '本产品基于扶摇（同花顺金融数据 API）等数据源做条件筛选与历史统计，结果仅供研究学习，不构成任何投资建议；' +
  '不预测股价涨跌、不承诺收益；历史回测不代表未来表现。投资有风险，决策请独立判断。';

export function useDisclaimer(): string {
  return useStore((s) => s.catalog?.disclaimer) || FALLBACK_DISCLAIMER;
}

export function DisclaimerFooter() {
  const disclaimer = useDisclaimer();
  const version = useStore((s) => s.health?.version);
  return (
    <Layout.Footer className="app-footer">
      <div className="footer-inner">
        <div className="footer-disclaimer">
          <SafetyCertificateOutlined style={{ marginTop: 3 }} />
          <span>{disclaimer}</span>
        </div>
        <div className="footer-meta">
          数据来源：扶摇（同花顺金融数据 API）· iFinD MCP（资讯背景，不参与筛选与打分）
          {version ? ` · 后端版本 ${version}` : ''}
        </div>
      </div>
    </Layout.Footer>
  );
}
