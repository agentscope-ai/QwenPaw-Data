# -*- coding: utf-8 -*-
"""Stage prompts for unattended skill drafts. Always author a new skill."""

from __future__ import annotations

SYSTEM_SKILL_WRITER = """你是技能沉淀专家。把一次已完成的交互归纳为**一份新的**可复用技能。

- 面向人读的文字一律用**简体中文**：技能卡、`description`、SKILL.md 正文、reference、脚本 `--help`/注释。`name`、路径、CLI flag、代码标识符保持英文
- 结论只来自证据摘要；无证据不写；拿不准写入 `open_questions`，按最窄解释继续
- 泛化：去掉本次取值（日期、产品码、客户名、临时表），保留可复用流程与约束
- **只新建**。禁止改写、补丁或建议覆盖任何既有技能正文。重叠只换更窄 kebab 名
- 主题锚定原始分析任务的泛化流程；局部失败只进 pitfall，不进技能名
- 本轮只输出当前要求的结构化对象，不要一次输出全部技能文件正文
"""

# 撰写阶段用的短边界：完整判定表只在 induct 注入 capture_boundary。
WRITE_BOUNDARY = """只沉淀风格 / 步骤 / 工具用法 / pitfall。
禁止：指标口径、维度绑列、列枚举、选表结论、金标 SQL、专有名词、环境故障。
禁止 SQL 代码块与 `列 = '字面量'`。表名只能说明命名约定，不能写成「这类分析查这张表」。
禁止具体 `datasource_id` 取值、图谱 key（`met:` / `dim:` / `ds:` 开头）、带业务字面量的 Cypher；图查询只写「先查哪类节点、再沿哪条关系」的形状。"""


def induct_prompt(
    *,
    capture_boundary: str,
    evidence_guide: str,
    digest: str,
    skill_inventory: str,
    task_query: str,
    failure_notes: str = "",
) -> str:
    retry = ""
    if failure_notes.strip():
        retry = f"""
## 上次失败，按报错重写技能卡与 `files` 计划

<failure>
{failure_notes.strip()}
</failure>
"""
    return f"""本阶段只做归纳：**不要撰写任何文件正文，不要提问。**

<capture_boundary>
{capture_boundary}
</capture_boundary>

<evidence_guide>
{evidence_guide}
</evidence_guide>

<task_query>
{task_query or "(未提供，以证据摘要第 1 节为准)"}
</task_query>

<digest>
{digest}
</digest>

## 现有技能（只避重名，禁止改写）

{skill_inventory}

## 要求

- 主题来自 digest **第 1 节任务**与 **第 4 节工作流骨架**的泛化（如「单指标时间趋势 + 单一维度下拆」）。第 5、6 节失败只进技能卡 pitfall，不得当技能名或步骤标题。反例：`semantic-layer-fallback` / `sql-retry` / `error-recovery`；应类似 `metric-trend-breakdown`
- 捕获边界用来从正文剔除业务知识，不是停写理由；`coverage` 只记录重叠
- 结构化输出：
  - `skill_card`：字段按 evidence_guide，每条带 `[锚点]`
  - `skill_name`：英文小写连字符，不与清单重名
  - `files`：`[{{path, purpose, contract}}]`，必须含 `SKILL.md`；脚本 `contract` 至少一个 `--flag`；无证据则只有 `SKILL.md`。不要写文件正文
{retry}"""


def author_skill_md_prompt(
    *,
    skill_card: str,
    skill_name: str,
    file_plan: str,
    previous_files: str = "",
    failure_notes: str = "",
    user_text: str = "",
    digest: str = "",
    checklist: str = "",
    effective_guide: str = "",
) -> str:
    retry = ""
    if failure_notes.strip():
        retry = f"""
## 上次失败，只重写 `SKILL.md`

<failure>
{failure_notes.strip()}
</failure>

<previous>
{previous_files or "(无)"}
</previous>
"""
    revise = ""
    if user_text.strip():
        revise = f"""
<digest>
{digest or "(无)"}
</digest>

<revise_instruction>
{user_text}
</revise_instruction>

按意见改 `SKILL.md`；计划中仍保留的捆绑文件必须继续引用。
"""
    return f"""只写 `SKILL.md`，一次只输出这一个文件。形态按 effective_skill_guide：frontmatter 触发、正文工作流、链接捆绑资源。输出前按清单自检，未通过先改。

{WRITE_BOUNDARY}

<effective_skill_guide>
{effective_guide}
</effective_skill_guide>

<authoring_checklist>
{checklist}
</authoring_checklist>

<skill_card>
{skill_card}
</skill_card>

<file_plan>
{file_plan}
</file_plan>
{revise}
## 格式

- frontmatter：`name` 必须恰好是 `{skill_name}`；`description` 写清 WHAT 与何时触发（含真实说法）。正文不要写触发条件
- 步骤是泛化后的**整段分析流程**（确认参数 → 取数 → 分析 → 交付）；局部失败只进 pitfall
- 引用 file_plan 中每一个捆绑文件：脚本给调用命令与参数表（覆盖其 `--flag`），reference 写阅读时机；有脚本则写 `## fallback`
- 不要引用计划外路径；不要写入技能卡的「证据不足项」「已丢弃的业务知识」
- 复用其它技能写「按 `<技能名>` 执行」
- ≤500 行
{retry}"""


def author_bundle_prompt(
    *,
    expected_path: str,
    purpose: str,
    contract: str,
    skill_excerpt: str,
    previous_content: str = "",
    failure_notes: str = "",
    checklist: str = "",
    effective_guide: str = "",
) -> str:
    retry = ""
    if failure_notes.strip():
        retry = f"""
## 上次失败，只重写 `{expected_path}`，不要改 CLI 名

<failure>
{failure_notes.strip()}
</failure>

<previous>
{previous_content or "(无)"}
</previous>
"""
    return f"""只写 `{expected_path}`，一次只输出这一个文件。形态按 effective_skill_guide：脚本做确定性执行，reference 做按需知识，信息只写一处。输出前按清单自检，未通过先改。

{WRITE_BOUNDARY}

<effective_skill_guide>
{effective_guide}
</effective_skill_guide>

<authoring_checklist>
{checklist}
</authoring_checklist>

<skill_excerpt>
{skill_excerpt or "(未找到对应段落，按 contract 实现)"}
</skill_excerpt>

- 路径：`{expected_path}`
- 用途：{purpose or "按契约实现"}
- contract：{contract or "见 SKILL.md 摘录"}

## 格式

- 必须实现摘录/contract 中每一个 `--flag`，不要改名
- 脚本：argparse、`--help`、只用标准库、可独立运行
- 不要输出其它文件
{retry}"""


def revise_prompt(
    *,
    current_files: str,
    digest: str,
    user_text: str,
    file_plan: str = "",
    skill_name: str = "",
    previous_files: str = "",
    failure_notes: str = "",
    skill_card: str = "",
    checklist: str = "",
    effective_guide: str = "",
) -> str:
    return author_skill_md_prompt(
        skill_card=skill_card or current_files,
        skill_name=skill_name or "",
        file_plan=file_plan or current_files,
        previous_files=previous_files,
        failure_notes=failure_notes,
        user_text=user_text,
        digest=digest,
        checklist=checklist,
        effective_guide=effective_guide,
    )
