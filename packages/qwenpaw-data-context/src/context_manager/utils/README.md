# Utils

`context_manager.utils` 收纳可跨模块复用、语义一致的通用工具。它是只依赖 Python 标准库的叶子包。

## 文件职责

- `batches.py`：在调用方选定的批次和独立阶段内稳定聚合条目，保留组首次出现和组内输入顺序。调用方决定操作类型、Spec 和完整写 Scope 的分组键；函数不推断依赖、不跨事务聚合、不重排状态检查点。
- `validation.py`：标量形状校验，包括 trimmed text、单个路径片段、有限整数区间和 `[0, 1]` 概率。路径片段校验只检查字符串；目录拼接和符号链接检查由文件存储模块负责。
- `parsing.py`：解析纯 JSON 或 fenced JSON，并校验 JSON array/object/envelope。
- `immutable.py`：把嵌套 mapping、sequence 和 set 递归分离为不可变快照。
- `vector.py`：校验有限向量/向量批次并计算非负 cosine similarity。
- `values.py`：处理不带业务含义的空值判断、文本序列投影与保留大小写的文本归一化。
- `redaction.py`：脱敏日志中的凭据文本与结构化字段，并提供由服务启动入口显式调用的日志安装函数。

## 使用

上层直接从职能文件导入工具：

```python
from context_manager.utils.validation import require_trimmed_text

owner_id = require_trimmed_text(raw_owner_id, "owner_id")
```

普通函数按输入计算结果。日志脱敏由 `create_service_app()` 显式安装到标准
`logging.Handler.format`，覆盖已有和后续 Handler，保留原 Formatter；自定义 Handler 可复用该方法。

新增工具应具有明确的通用语义，复用同职能函数。
领域校验、异常和业务规则留在对应模块；普通函数无状态，日志安装是显式的进程配置操作。
