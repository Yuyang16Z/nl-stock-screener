import { useEffect } from 'react';
import { App as AntApp, Button, ConfigProvider, Layout, Result } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { AppHeader } from './components/AppHeader';
import { DisclaimerFooter } from './components/DisclaimerFooter';
import Library from './pages/Library';
import Workbench from './pages/Workbench';
import { useStore } from './store';
import { theme } from './theme';

/** 启动时加载指标目录；健康状态轮询（预热中 3 秒，否则 20 秒） */
function Bootstrap() {
  const loadCatalog = useStore((s) => s.loadCatalog);
  const refreshHealth = useStore((s) => s.refreshHealth);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      await refreshHealth();
      if (stopped) return;
      const { health, healthError } = useStore.getState();
      const warming = Object.values(health?.warm ?? {}).some((v) => v.startsWith('warming'));
      timer = window.setTimeout(tick, warming ? 3000 : healthError ? 8000 : 20000);
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [refreshHealth]);

  return null;
}

function NotFound() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="你访问的页面不存在，请从顶部导航进入。"
      extra={
        <Link to="/">
          <Button type="primary">返回选股工作台</Button>
        </Link>
      }
    />
  );
}

export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={theme} button={{ autoInsertSpace: false }}>
      <AntApp>
        <BrowserRouter>
          <Bootstrap />
          <Layout className="app-layout">
            <AppHeader />
            <Layout.Content className="app-content">
              <Routes>
                <Route path="/" element={<Workbench />} />
                <Route path="/library" element={<Library />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Layout.Content>
            <DisclaimerFooter />
          </Layout>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}
