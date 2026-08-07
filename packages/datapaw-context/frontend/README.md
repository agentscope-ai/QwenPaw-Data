# QwenPaw-Data Frontend

这是 QwenPaw-Data 管理台的 Vite + React 前端项目，位于 `packages/datapaw-context/frontend`。

页面范围包括数据源管理、业务域管理、数据集管理、列管理、维度管理、维度口径、指标管理、指标口径、语义编织、Excel 导入、CM Graph 和 KG Docs。

## 本地开发

推荐从仓库根目录统一初始化和启动 DataBridge：

```bash
scripts/init_databridge.sh
scripts/start_databridge.sh
```

这会同时启动 `http://localhost:3000` 的管理前端和
`http://localhost:8765` 的 DataBridge API。

如需单独运行前端，先确保后端已启动：

```bash
cd packages/datapaw-context/frontend

npm ci
npm run dev
```

前端读取仓库根目录 `.env`。开发代理默认使用根 `.env` 中的：

```bash
SERVICE_BASE_URL=http://localhost:8765
```

开发服务固定监听 `3000` 并启用 `strictPort`。如果端口被占用，Vite 会直接失败，
避免 DataBridge 管理入口跳到错误端口。

## 可用脚本

```bash
npm run dev      # 启动本地开发服务
npm run build    # TypeScript 检查并生成生产产物
npm run lint     # ESLint 检查
npm run preview  # 预览 dist 产物
```

生产构建产物输出到：

```bash
packages/datapaw-context/frontend/dist
```

## 后端配合

后端位于 `packages/datapaw-context`，FastAPI 入口是：

```bash
cd packages/datapaw-context
./.venv/bin/python scripts/serve.py --port 8765
```

后端需要 Neo4j 和仓库根目录 `.env` 配置。依赖服务可用包根目录下的 `docker-compose.yml` 启动：

```bash
cd packages/datapaw-context
docker compose up -d neo4j
```

## 生产构建

### 同源部署推荐

如果前端和后端使用同一个域名，推荐让浏览器请求相对路径 `/api/...`，再由 Nginx 代理到后端。

构建时不要写死后端域名：

```bash
cd packages/datapaw-context/frontend
VITE_API_BASE_URL= SERVICE_BASE_URL= npm run build
```

然后把 `dist` 目录发布到服务器，例如：

```bash
/opt/qwenpaw-data/frontend/dist
```

### 前后端不同域名

如果前端和后端不是同源，例如：

- 前端：`https://qwenpaw-data.example.com`
- 后端：`https://api.qwenpaw-data.example.com`

则构建时写入后端地址：

```bash
VITE_API_BASE_URL=https://api.qwenpaw-data.example.com npm run build
```

注意：`VITE_API_BASE_URL` 会被 Vite 编译进前端产物。后端地址变化后，需要重新构建前端。`SERVICE_BASE_URL` 仅保留给旧代码和本地 dev proxy 作为兼容兜底。

## Nginx 示例

同源部署示例：

```nginx
server {
  listen 80;
  server_name qwenpaw-data.example.com;

  root /opt/qwenpaw-data/frontend/dist;
  index index.html;

  location / {
    try_files $uri /index.html;
  }

  location /api/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

这里的 `try_files $uri /index.html` 是必须的，否则直接刷新 `/metric-lib`、`/column` 等 React Router 页面会 404。

## 部署检查清单

1. 后端 `scripts/serve.py` 正常监听 `8765`。
2. 服务器上的仓库根目录 `.env` 已配置 Neo4j、LLM 和 OSS 等参数。
3. 前端构建时的 `VITE_API_BASE_URL` 与部署方式一致。
4. Nginx 已代理 `/api/` 到后端。
5. Nginx 已配置 React Router fallback：`try_files $uri /index.html`。
6. 浏览器访问前端页面后，Network 中 `/api/...` 请求返回 200。

## 开发约定

路由级页面位于 `src/pages`。

公共逻辑位于：

- `src/services`：接口调用
- `src/hooks`：通用 hooks
- `src/design`：设计系统封装
- `src/layout`：全局布局
- `src/i18n`：国际化文案

应用代码中的 UI 组件应优先从 `@/design` 引入。这里封装了 `@agentscope-ai/design`、Ant Design Pro Table，以及项目内统一表格行为。不要在业务页面里直接从 `antd` 或 `@ant-design/*` 引入组件，除非封装层暂时没有提供对应能力。
