# Dataagent — 快捷入口
#
# 前置条件：
#   1. Neo4j 5.20+ Community 运行中（bolt://localhost:7687）
#   2. 环境变量已配置（见仓库根目录 .env.example）
#   3. 数据源已在语义配置层（semantic_config.db）中配置并 sync
#
# 用法：
#   make serve              启动 Web UI（http://localhost:8765）

PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
export PYTHONPATH := $(CURDIR):$(PYTHONPATH)
DATASOURCE ?= appdata

.PHONY: serve help

# ── Web UI ─────────────────────────────────────────────────────────────────────
serve:
	$(PYTHON) scripts/serve.py

# ── 帮助 ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "Dataagent — 可用目标"
	@echo ""
	@echo "  serve                 启动（http://localhost:8765）"
	@echo ""
