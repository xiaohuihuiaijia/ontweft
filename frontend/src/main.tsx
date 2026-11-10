import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './app/App';
import './app/tokens.css';
import './app/styles.css';
import './shared/brand/brand.css';
import './app/branding.css';
import { captureFeishuCallback, FeishuCallback } from './features/sources/feishu/FeishuCallback';

const callback = captureFeishuCallback();
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode>{callback ? <FeishuCallback input={callback} /> : <App />}</React.StrictMode>);
