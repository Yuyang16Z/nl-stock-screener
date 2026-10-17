import type { ThemeConfig } from 'antd';

export const PRIMARY = '#1f4fd8';

/** 专业、克制的金融产品风格：深蓝主色、浅灰底、细边框 */
export const theme: ThemeConfig = {
  token: {
    colorPrimary: PRIMARY,
    colorInfo: PRIMARY,
    colorLink: PRIMARY,
    colorBgLayout: '#f3f5f9',
    colorTextBase: '#1c2333',
    colorBorderSecondary: '#e8ebf1',
    borderRadius: 6,
    fontSize: 14,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      headerHeight: 64,
      headerPadding: '0 24px',
      footerBg: '#eaeef4',
      footerPadding: '16px 24px',
    },
    Table: {
      headerBg: '#f6f8fb',
      headerColor: '#4a5468',
      rowHoverBg: '#f5f8ff',
      cellPaddingBlockSM: 6,
    },
    Card: {
      headerFontSize: 15,
    },
    Tabs: {
      titleFontSize: 14,
    },
  },
};
