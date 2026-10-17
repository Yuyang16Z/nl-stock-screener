import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发：/api 代理到本地 FastAPI（默认 http://localhost:8000，可用环境变量 SCREENLENS_API_TARGET 覆盖）；
// 构建产物输出到 frontend/dist，由后端静态托管
const apiTarget = process.env.SCREENLENS_API_TARGET || 'http://localhost:8000';

export default defineConfig({
  base: '/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  preview: {
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        // 只拆出两个大依赖，其余第三方合并为 vendor，避免分块之间循环引用
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (/[\\/](echarts|zrender|echarts-for-react)[\\/]/.test(id)) return 'vendor-echarts';
          if (/[\\/](antd|@ant-design|@rc-component|rc-[\w-]+)[\\/]/.test(id)) return 'vendor-antd';
          return 'vendor';
        },
      },
    },
  },
});
