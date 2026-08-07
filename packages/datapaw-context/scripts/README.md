# Scripts（dataagent）

在项目根目录执行；Python 优先使用 `.venv/bin/python`（与 `Makefile` 一致）。

## 常用入口

| 用途 | 命令 |
| --- | --- |
| 后端 API 服务 | `python scripts/serve.py` 或 `make serve` |
| 容器 | `make docker-up`（变量见 `Makefile`） |

## 目录约定

```
scripts/
├── setup/               构图、下载数据、build_topology、index_embeddings、物化 view
├── serve.py              后端 API 服务入口（前端需另起 frontend/）
└── run_*.sh              一键评测 shell 包装
```

- **`setup/`** — 下载数据、构图、`build_topology`、`index_embeddings`、`generate_rsa_keypair`。
