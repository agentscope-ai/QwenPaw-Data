# QwenPaw-Data Frontend

这是 QwenPaw-Data 管理台的 Vite + React 前端项目，位于 `packages/qwenpaw-data-context/frontend`。

页面范围包括数据源管理、业务域管理、数据集管理、列管理、维度管理、维度口径、指标管理、指标口径、语义编织、Excel 导入、CM Graph 和 KG Docs。

## 本地开发

推荐从仓库根目录统一初始化和启动 Context：

```bash
scripts/init_context.sh
scripts/start_context.sh
```

这会同时启动 `http://localhost:3000` 的管理前端和 `http://localhost:8765` 的 Context API。

如需单独运行前端，先确保后端已启动：

```bash
cd packages/qwenpaw-data-context/frontend

npm ci
npm run dev
```

前端读取仓库根目录 `.env`。开发代理默认使用根 `.env` 中的：

```bash
SERVICE_BASE_URL=http://localhost:8765
```

开发服务固定监听 `3000` 并启用 `strictPort`。如果端口被占用，Vite 会直接失败，避免 QwenPawData 的 Context Management 入口跳到错误端口。

## 可用脚本

```bash
npm run dev      # 启动本地开发服务
npm run build    # TypeScript 检查并生成生产产物
npm run lint     # ESLint 检查
npm run preview  # 预览 dist 产物
```

生产构建产物输出到：

```bash
packages/qwenpaw-data-context/frontend/dist
```

## 后端配合

后端 FastAPI 入口是 `context_manager.server:app`。从仓库根目录统一启动服务：

```bash
bash scripts/start.sh
```

启动入口会同时运行 CM 前端，无需重复执行 `npm run dev`。图存储由仓库根目录 `.env` 的 `GRAPH_BACKEND` 选择 Neo4j 或 Holo AGE。账号、模型和聊天验证见[统一启动与聊天准备说明](../../../scripts/chat_setup/README.md)。

## 生产构建

### 同源部署推荐

如果前端和后端使用同一个域名，推荐让浏览器请求相对路径 `/api/...`，再由 Nginx 代理到后端。

构建时不要写死后端域名：

```bash
cd packages/qwenpaw-data-context/frontend
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

1. CM 后端正常监听配置的端口（默认 `8765`）。
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

## 本地登录快捷账号

开发登录页读取与聊天初始化相同的 `QWENPAW_DATA_ENV_FILE` 和 `accounts` 字段，默认使用初始化工具示例中的账号矩阵。账号文件的相对路径以所选 env 文件目录为基准；修改 env 或账号文件后开发服务自动更新。快捷密码优先读取单账号的 `VITE_DEV_LOGIN_PASSWORD_OVERRIDES`，其次读取 `VITE_DEV_LOGIN_PASSWORD`。均未配置时手动输入密码。名单表示初始化配置，不代表账号已经创建成功。生产构建不展示快捷账号，也不包含快捷密码。

## Packaged Console deployment

`python scripts/build_console.py` builds this UI with `--mode console` into
the `qwenpaw-data-console` wheel, mounted at `/databridge/`. This mode reads
no checkout `.env`; the local portal supplies routing and authentication
settings through `/console-config.js`. Standalone builds keep their existing
root routing and authentication. No source rewriting is needed.
