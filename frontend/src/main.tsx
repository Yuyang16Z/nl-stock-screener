import React from 'react';
import ReactDOM from 'react-dom/client';
import 'antd/dist/reset.css';
import './styles.css';
import App from './App';

const root = document.getElementById('root');
if (!root) throw new Error('缺少 #root 挂载点');

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
