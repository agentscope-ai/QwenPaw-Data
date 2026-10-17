import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// React 19 移除了 ReactDOM.render/unmountComponentAtNode，antd v5 的静态方法
// （Modal.confirm / message / notification）会静默失效；官方补丁经 unstableSetRender
// 换用 createRoot 实现。必须在任何 antd 静态调用前导入。
import '@ant-design/v5-patch-for-react-19'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
