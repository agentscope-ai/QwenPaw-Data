# -*- coding: utf-8 -*-
"""Prompts for presentation cards and the two Trace2Segment judge steps.

The response object is never described here. Every call goes out as a forced
tool call whose parameters *are* the JSON schema, so keys, types, enums and
which of them may be null are already stated where the provider enforces them;
restating that in the prompt only buys input tokens. What these prompts carry
is the part a schema cannot express: what each field should say.
"""

from __future__ import annotations

from typing import Literal

from qwenpaw_data.host.core.algo.biztrace.tools import tool_purpose

PromptLang = Literal["zh", "en"]
DEFAULT_PROMPT_LANG: PromptLang = "zh"

PresentationTask = Literal[
    "thinking",
    "text",
    "PlanCreate",
    "PlanUpdate",
    "spawn_subagent",
]


# --------------------------------------------------------------------------- #
# BizTrace presentation cards
# --------------------------------------------------------------------------- #

PRESENTATION_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。你的任务是把 Agent 轨迹中的一段原始内容改写成面向业务\
用户的简短说明。

写作要求：

- 用简洁的自然语言陈述，不要使用列表符号、标题或代码块；
- 只写可直接展示的正文，不要输出 JSON，也不要用 summary / body 等字段包裹；
- 只描述给定内容中确实发生的事，不臆测、不补充外部信息；
- 不要复述原文，要概括其要点；
- 控制在 2 句以内，避免技术黑话，业务用户能看懂；
- 使用中文。
"""

PRESENTATION_SYSTEM_PROMPT_EN = """\
You write business-facing trace cards. Rewrite a raw slice of an agent trajectory \
into a short description that a frontend card can render directly.

Requirements:

- plain concise prose; no bullet lists, headings, or code blocks;
- write display-ready prose only; never emit JSON or wrap text in keys such as \
summary or body;
- describe only what the given content actually shows; never speculate or add \
outside information;
- summarize rather than restate;
- at most 2 sentences, understandable by a business user without jargon.
"""

THINKING_PRESENTATION_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。把 Agent 的一段思考改写成面向业务用户的说明，\
供前端卡片直接展示。

写作要求：

- 用自然语言陈述，可以分几句或短段落，不要使用代码块；
- 只写可直接展示的正文，不要输出 JSON，也不要用 summary / body 等字段包裹；
- 只描述给定内容中确实出现的信息，不臆测、不补充外部信息；
- 必须保留思考中的关键信息：错误或失败原因、关键决策与取舍、下一步打算；
- 不要为了短而丢掉上述关键信息，不必压到两句以内；
- 避免无信息量的过程描写（如「正在思考」）；
- 使用中文。
"""

THINKING_PRESENTATION_SYSTEM_PROMPT_EN = """\
You write business-facing trace cards. Rewrite a slice of the agent's reasoning \
into a description a frontend card can render directly.

Requirements:

- plain natural language; a few sentences or a short paragraph is fine; no code \
blocks;
- write display-ready prose only; never emit JSON or wrap text in keys such as \
summary or body;
- describe only what the given content actually shows; never speculate or add \
outside information;
- keep the key information: error or failure analysis, important decisions and \
trade-offs, and what the agent plans to do next;
- do not drop those details to stay short; do not force a two-sentence limit;
- skip empty process talk such as "still thinking".
"""

_PRESENTATION_TASK_ZH: dict[str, str] = {
    "thinking": (
        "概括这段思考：它发现了什么问题、做了哪些关键判断、接下来准备怎么做。"
        "错误分析、关键决策和取舍必须保留。"
    ),
    "text": "概括 Agent 这段回复告诉了用户什么。",
    "PlanCreate": "用自然语言简述这个计划：目标是什么、拆成了哪几步。",
    "PlanUpdate": "用自然语言描述这次计划改动：改了什么、为什么改。",
    "spawn_subagent": "概括这次子代理调用的任务与背景：要它做什么、给了什么前提。",
}

_PRESENTATION_TASK_EN: dict[str, str] = {
    "thinking": (
        "Summarize this reasoning: what problem it found, which decisions it "
        "made, and what it plans to do next. Keep error analysis, key "
        "decisions, and trade-offs."
    ),
    "text": "Summarize what this reply tells the user.",
    "PlanCreate": "Describe this plan in plain language: its goal and the steps "
    "it breaks into.",
    "PlanUpdate": "Describe this plan revision: what changed and why.",
    "spawn_subagent": "Summarize this sub-agent call: the task it was given and "
    "the context supplied.",
}

TOOL_PRESENTATION_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者，负责为 Agent 的工具调用生成卡片文案，供业务用户阅读。

写作要求：

- caption 是卡片小标题，一句短语概括这次调用在做什么，不超过 20 字，不要以句号结尾；
- purpose 用一句话说明这个工具在做什么；
- input_summary 概括入参要点，不超过 200 字；
- 各字段只写自然语言，不要把整段回复写成 JSON；
- 只描述给定内容中确实存在的信息，不臆测；
- 不使用列表符号、标题或代码块，使用中文。
"""

TOOL_PRESENTATION_SYSTEM_PROMPT_EN = """\
You write business-facing trace cards for an agent's tool calls.

Requirements:

- caption is the card title: one short phrase naming what this call does, under \
20 words, no trailing period;
- purpose is one sentence on what the tool does;
- input_summary summarizes the arguments, at most 200 characters;
- each field is plain prose; never emit the whole reply as JSON;
- describe only information actually present; never speculate;
- no bullet lists, headings, or code blocks.
"""

TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者，只负责概括工具调用的出参或失败原因。

写作要求：

- 一句或一段话概括结果（成功）或错误原因（失败），不超过 200 字；
- 只写可直接展示的正文，不要输出 JSON，也不要用字段名包裹；
- 这次调用本身已在卡片其他位置展示，不要复述；
- 只描述出参 / 错误中确实存在的信息，不臆测；
- 不使用列表符号、标题或代码块，使用中文。
"""

TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_EN = """\
You write the result section of a business-facing tool card.

Requirements:

- one short paragraph on the result (success) or the error (failure), at most \
200 characters;
- write display-ready prose only; never emit JSON or wrap text in field names;
- the call itself is already shown elsewhere on the card — do not repeat it;
- describe only information actually present in the result; never speculate;
- no bullet lists, headings, or code blocks.
"""


def get_presentation_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for free-text presentation bodies (plans, sub-agents)."""
    if lang == "en":
        return PRESENTATION_SYSTEM_PROMPT_EN
    return PRESENTATION_SYSTEM_PROMPT_ZH


def get_thinking_presentation_system_prompt(
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """System prompt for assistant-thinking cards; keeps key decisions."""
    if lang == "en":
        return THINKING_PRESENTATION_SYSTEM_PROMPT_EN
    return THINKING_PRESENTATION_SYSTEM_PROMPT_ZH


def get_tool_presentation_system_prompt(
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """System prompt for the call-card template (caption / purpose / input)."""
    if lang == "en":
        return TOOL_PRESENTATION_SYSTEM_PROMPT_EN
    return TOOL_PRESENTATION_SYSTEM_PROMPT_ZH


def get_tool_output_presentation_system_prompt(
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """System prompt for the result-card template (output_summary only)."""
    if lang == "en":
        return TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_EN
    return TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_ZH


def build_presentation_summary_prompt(
    *,
    task: PresentationTask,
    content: str,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt asking for one summarized presentation body."""
    if lang == "en":
        return (
            f"## Instruction\n\n{_PRESENTATION_TASK_EN[task]}\n\n"
            f"## Content\n\n{content}\n"
        )
    return f"## 任务\n\n{_PRESENTATION_TASK_ZH[task]}\n\n## 内容\n\n{content}\n"


def _purpose_section(tool_name: str, lang: PromptLang) -> str:
    """What the tool is for, so a bare name is not all the model has to go on.

    Empty for unregistered tools. It is background about the tool, not about
    this call, hence the warning against copying it into the card.
    """
    purpose = tool_purpose(tool_name, lang)
    if not purpose:
        return ""
    if lang == "en":
        return f"## What the tool is for (background; do not copy)\n\n{purpose}\n\n"
    return f"## 工具用途（仅供理解，不要照抄）\n\n{purpose}\n\n"


def build_tool_running_prompt(
    *,
    tool_name: str,
    tool_input: str,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt for a call card's caption, purpose and input."""
    about = _purpose_section(tool_name, lang)
    if lang == "en":
        return f"## Tool\n\n{tool_name}\n\n{about}## Arguments\n\n{tool_input}\n"
    return f"## 工具\n\n{tool_name}\n\n{about}## 入参\n\n{tool_input}\n"


def build_tool_output_prompt(
    *,
    tool_name: str,
    purpose: str,
    call_context: str,
    tool_output: str,
    failed: bool,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt for a result card's output summary.

    ``call_context`` is the call card's body: background only. The model must
    still write solely about the result / error, never restate that context.
    """

    about = _purpose_section(tool_name, lang)
    if lang == "en":
        state = (
            "The call FAILED; summarize only the error from the Result."
            if failed
            else "The call succeeded; summarize only the Result."
        )
        return (
            f"## Instruction\n\nSummarize the Result only. Do not restate the "
            f"tool name, purpose, or The call section.\n\n"
            f"## Tool\n\n{tool_name} — {purpose}\n\n{about}"
            f"## The call (context only; do not repeat)\n\n"
            f"{call_context or '-'}\n\n"
            f"## Outcome\n\n{state}\n\n"
            f"## Result\n\n{tool_output}\n"
        )
    state = (
        "本次调用失败，请只概括「出参」中的错误原因。"
        if failed
        else "本次调用成功，请只概括「出参」中的结果要点。"
    )
    return (
        f"## 任务\n\n只概括「出参」。"
        f"不要重复工具名、目的或「调用信息」中的内容。\n\n"
        f"## 工具\n\n{tool_name} — {purpose}\n\n{about}"
        f"## 调用信息（仅供参考，勿重复）\n\n{call_context or '-'}\n\n"
        f"## 结果状态\n\n{state}\n\n"
        f"## 出参\n\n{tool_output}\n"
    )


# --------------------------------------------------------------------------- #
# Per-family tool cards
# --------------------------------------------------------------------------- #

SQL_CALL_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用业务用户能读懂的自然语言概括这次 SQL 查询。\
不要把 SQL 全文写进 purpose。

- caption：一句短语概括这次查询在做什么，不超过 20 字，不要以句号结尾。
- purpose：一两句通顺的话说明查什么。SQL 里有的必须写进句子：表（`项目.表`）、\
时间窗/分区（字段 + 取值）、过滤条件、JOIN 表与关联键、聚合粒度、指标字段与公式。\
没有的不要编。禁止「执行 SQL 检索最新分区」这类空话。
"""

SQL_CALL_SYSTEM_PROMPT_EN = """\
You write a business-facing call card for one SQL invocation. Use readable \
prose. Do not paste the full SQL into purpose.

- caption: a short phrase naming what this query does, under 20 words, no \
trailing period.
- purpose: one or two sentences on what is being queried. Weave in whatever \
the SQL actually contains: `project.table` names, time/partition window \
(field + value), filters, JOINs and keys, aggregation grain, metric fields \
and formulas. Do not invent missing pieces. Never write empty process talk \
such as "ran a SQL query".
"""

SQL_RESULT_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。只概括这条 SQL 的结果或失败原因。不要复述调用摘要。

- output_summary：通顺一两句话。成功写行数/规模 + 1～3 个锚点数字或枚举；\
探表写有用字段名及用途；失败写影响到的对象。禁止「返回 70 余个字段」「调用失败」\
这类空话。
- 失败时 failure_kind 取 no_table / no_permission / no_column / blocked / other；\
成功填 null。
"""

SQL_RESULT_SYSTEM_PROMPT_EN = """\
You write the result of one SQL invocation. Summarize only the result or \
error. Do not restate the call summary.

- output_summary: one or two sentences. On success, row count / scale plus \
1-3 anchor numbers or enums; on a table probe, useful column names; on \
failure, the affected object. Never write empty process talk such as \
"returned 70 fields" or "the call failed".
- on failure, failure_kind is no_table / no_permission / no_column / blocked \
/ other; on success, null.
"""

SEMANTIC_CALL_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用自然语言概括这次业务图谱检索。入参里的 Cypher \
是查询语句，要翻译成业务语言，不要引用 Cypher 或属性名。

- caption：一句短语，不超过 20 字，不要以句号结尾。
- purpose：一句话说清楚在查哪类业务对象（指标 / 口径 / 数据集 / 字段 / 维度 / 表 / 列 / \
数据源 / 已验证查询 / 事件 / 记忆）。
- input_summary：把检索的对象和限定条件写进句子（如「名称含 DAU 的指标及其口径」）；\
参数里的名称与关键词也写进去。
- 不要写检索过程，不要写 MATCH / WHERE / RETURN 等语法。使用中文。
"""

SEMANTIC_CALL_SYSTEM_PROMPT_EN = """\
You write a business-facing call card for a business-graph lookup. The Cypher \
in the arguments is a query: translate it into business terms and never quote \
Cypher or property names.

- caption: a short phrase, under 20 words, no trailing period.
- purpose: one sentence naming the kind of business object being searched \
(metric / definition / dataset / field / dimension / table / column / \
datasource / verified query / event / memory).
- input_summary: weave the searched object and its constraints into sentences \
(e.g. "metrics whose name contains DAU, with their definitions"); include the \
names and keywords from the parameters.
- no search process and no MATCH / WHERE / RETURN syntax.
"""

SEMANTIC_RESULT_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用一两句自然语言写这次检索命中了哪些业务对象、选用了\
哪一个（如指标名与口径所在数据集、表与数据源）。出参是 JSON 行，只取其中有业务\
含义的值。结果为空要明说「未命中」；`truncated` 为真要说明结果被截断。不要列无关\
命中，不要复述整份清单，不要提列名。使用中文。
"""

SEMANTIC_RESULT_SYSTEM_PROMPT_EN = """\
You write the result of a business-graph lookup as one or two readable \
sentences: which business objects hit and which one was chosen (for example a \
metric and the dataset of its definition, or a table and its datasource). The \
output is JSON rows; use only the business-meaningful values. If the result is \
empty say nothing matched; if `truncated` is true say the result was cut off. \
No unrelated hits, no full listings, no column names.
"""

FILE_WRITE_CALL_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用自然语言说明这次写文件或改文件。

- caption：一句短语，不超过 20 字，不要以句号结尾。
- purpose：一句话说清楚要写或改什么。
- input_summary：点名文件名（不要完整路径）以及内容类型\
（schema / 指标定义 / SQL / 明细 / 报告等），写成句子。使用中文。
"""

FILE_WRITE_CALL_SYSTEM_PROMPT_EN = """\
You write a business-facing call card for writing or editing a file.

- caption: a short phrase, under 20 words, no trailing period.
- purpose: one sentence on what is being written or changed.
- input_summary: name the file (no full path) and its content type \
(schema / metric definition / SQL / detail / report) in sentences.
"""

FILE_WRITE_RESULT_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用自然语言概括这次写入或改动的内容要点：字段清单、\
公式、改了哪条口径。禁止只写「写入 N 行」或仅路径。使用中文。
"""

FILE_WRITE_RESULT_SYSTEM_PROMPT_EN = """\
You write the result of a file write or edit as readable prose: field list, \
formulas, which definition changed. Never write only a line count or only a \
path.
"""

SHELL_CALL_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用自然语言说明这次脚本调用。

- caption：一句短语，不超过 20 字，不要以句号结尾。
- purpose：用业务动词说明脚本在做什么。
- input_summary：把读入的文件/表、写出的文件写进句子。
- 不要写 ls/pwd 过程。使用中文。
"""

SHELL_CALL_SYSTEM_PROMPT_EN = """\
You write a business-facing call card for a shell script.

- caption: a short phrase, under 20 words, no trailing period.
- purpose: a business verb for what the script does.
- input_summary: weave which files or tables it reads and which files it \
writes into sentences.
- no ls/pwd process.
"""

SHELL_RESULT_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者。用自然语言写这次脚本结果。成功：产出文件与关键校验/\
数字。失败：错误类型与卡在哪一步。不要复述原始 traceback 全文。使用中文。
"""

SHELL_RESULT_SYSTEM_PROMPT_EN = """\
You write the result of a shell script as readable prose. On success: \
produced files and key checks or numbers. On failure: error type and the \
step it stalled on. Never restate a raw traceback.
"""


def get_sql_call_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a SQL call card (execute_read / modify / create)."""
    return SQL_CALL_SYSTEM_PROMPT_EN if lang == "en" else SQL_CALL_SYSTEM_PROMPT_ZH


def get_sql_result_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a SQL result card (execute_read / modify / create)."""
    return SQL_RESULT_SYSTEM_PROMPT_EN if lang == "en" else SQL_RESULT_SYSTEM_PROMPT_ZH


def get_semantic_call_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a business-graph lookup call card."""
    return (
        SEMANTIC_CALL_SYSTEM_PROMPT_EN
        if lang == "en"
        else SEMANTIC_CALL_SYSTEM_PROMPT_ZH
    )


def get_semantic_result_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a business-graph lookup result card."""
    return (
        SEMANTIC_RESULT_SYSTEM_PROMPT_EN
        if lang == "en"
        else SEMANTIC_RESULT_SYSTEM_PROMPT_ZH
    )


def get_file_write_call_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a write/edit call card."""
    return (
        FILE_WRITE_CALL_SYSTEM_PROMPT_EN
        if lang == "en"
        else FILE_WRITE_CALL_SYSTEM_PROMPT_ZH
    )


def get_file_write_result_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a write/edit result card."""
    return (
        FILE_WRITE_RESULT_SYSTEM_PROMPT_EN
        if lang == "en"
        else FILE_WRITE_RESULT_SYSTEM_PROMPT_ZH
    )


def get_shell_call_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a shell call card."""
    return SHELL_CALL_SYSTEM_PROMPT_EN if lang == "en" else SHELL_CALL_SYSTEM_PROMPT_ZH


def get_shell_result_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for a shell result card."""
    return (
        SHELL_RESULT_SYSTEM_PROMPT_EN if lang == "en" else SHELL_RESULT_SYSTEM_PROMPT_ZH
    )


def build_family_call_prompt(
    *,
    tool_name: str,
    tool_input: str,
    filename: str | None = None,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt for a family-specific call card."""
    about = _purpose_section(tool_name, lang)
    extra = ""
    if filename:
        extra = (
            f"## Filename\n\n{filename}\n\n"
            if lang == "en"
            else f"## 文件名\n\n{filename}\n\n"
        )
    if lang == "en":
        return f"## Tool\n\n{tool_name}\n\n{about}{extra}## Arguments\n\n{tool_input}\n"
    return f"## 工具\n\n{tool_name}\n\n{about}{extra}## 入参\n\n{tool_input}\n"


def build_family_result_prompt(
    *,
    tool_name: str,
    call_context: str,
    tool_output: str,
    failed: bool,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt for a family-specific result card."""
    about = _purpose_section(tool_name, lang)
    if lang == "en":
        state = (
            "The call FAILED; summarize only the error from the Result."
            if failed
            else "The call succeeded; summarize only the Result."
        )
        return (
            f"## Tool\n\n{tool_name}\n\n{about}"
            f"## The call (context only; do not repeat)\n\n"
            f"{call_context or '-'}\n\n"
            f"## Outcome\n\n{state}\n\n"
            f"## Result\n\n{tool_output}\n"
        )
    state = (
        "本次调用失败，请只概括「出参」中的错误原因。"
        if failed
        else "本次调用成功，请只概括「出参」中的结果要点。"
    )
    return (
        f"## 工具\n\n{tool_name}\n\n{about}"
        f"## 调用信息（仅供参考，勿重复）\n\n{call_context or '-'}\n\n"
        f"## 结果状态\n\n{state}\n\n"
        f"## 出参\n\n{tool_output}\n"
    )


# --------------------------------------------------------------------------- #
# Trace2Segment step 1: ContinuityJudge
# --------------------------------------------------------------------------- #

CONTINUITY_SYSTEM_PROMPT_ZH = """\
你是一名轨迹分段判定官。只判断前瞻节点是否与候选窗口属于同一个子任务。

## 子任务的定义

子任务是最小的、语义完整的工作单元，其目标应能脱离上下文被独立理解。

一个子任务不应只由一句收尾汇报、交付确认或状态确认构成；这类内容归入它所总结的\
那段工作。

## 判定准则

判断依据是**窗口的局部行为**，而不是全局任务是否完成——不要因为「整个任务还没做完」\
就倾向 continues=true。

也不要仅因为窗口「看起来已经做完」就切断：窗口内出现任务 completed、任务归档、\
文件已交付等完成信号，本身不构成边界。

窗口若以 `user`、`hint` 或向用户提问的节点开头，那些节点只提供动机与上下文，说明\
这个窗口在为什么服务；它们本身不是已完成的工作，判定前瞻节点时以其中的诉求为准。

**收尾发言属于它所总结的那段工作。** 前瞻节点若是对窗口内刚完成工作的汇报、交付\
确认、结果总结或状态确认，一律 continues=true：先有工作、后有交代，交代不是新子\
任务的开端。新子任务从「开始做下一件事」起算，而不是从「说完上一件事」起算。

典型 continues=true：

- 前瞻与窗口围绕**同一工件**展开（生成 → 校验 → 修改 → 交付）；
- 失败后的重试、修补；
- 同一目标下的连续推理或紧随其后的同类操作；
- 同一交付物的收尾（向用户汇报、交付确认、结果总结）紧接在产出之后；
- 仅工具名发生变化（如 write_file → edit_file），目标未变。

典型 continues=false：

- 局部目标切换：开始处理新的数据源、新的问题；
- 计划节点切换，且目标不同；
- 离开窗口的主要工件，转向另一段产物。

reason 用一句中文说明判定理由。
"""

CONTINUITY_SYSTEM_PROMPT_EN = """\
You are a trajectory segmentation judge. Decide only whether the peek node \
belongs to the same sub-task as the candidate window.

## What a sub-task is

A sub-task is the smallest semantically complete unit of work; its goal should be \
understandable on its own, without surrounding context. A sub-task must never \
consist only of a closing report, delivery confirmation, or status check; such \
content belongs to the work it wraps up.

## Decision criteria

Judge by the **local behavior of the window**, not by whether the overall task is \
finished — do not lean toward continues=true merely because the global task is \
still incomplete.

Nor should you cut merely because the window already looks finished: completion \
signals inside the window — task completed, task archived, file delivered — are \
not boundaries by themselves.

When the window opens with a `user`, `hint`, or ask-the-user node, those nodes \
supply motivation and context rather than completed work; judge the peek against \
the request they state.

**A closing remark belongs to the work it closes.** When the peek reports on, \
confirms delivery of, summarizes, or acknowledges completion of work just done in \
the window, always set continues=true: the account of the work is not the start of \
the next sub-task. A new sub-task begins when the next thing is started, not when \
the last thing is described.

Typical continues=true:

- peek and window revolve around the **same artifact** (produce, verify, revise, \
deliver);
- retries or fixes after a failure;
- continued reasoning or a back-to-back action under the same goal;
- closeout of the same deliverable (reporting back, delivery confirmation, result \
summary) immediately after producing it;
- only the tool name changed (e.g. write_file to edit_file) while the goal did not.

Typical continues=false:

- the local goal switches: a new data source or a new question begins;
- a plan-node switch with a different objective;
- the window's primary artifact is left behind for a different product.

Justify the decision in one sentence.
"""


def get_continuity_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for the two-field continuity judge."""
    if lang == "en":
        return CONTINUITY_SYSTEM_PROMPT_EN
    return CONTINUITY_SYSTEM_PROMPT_ZH


def build_continuity_user_prompt(
    *,
    query: str,
    scope: str | None,
    start_index: int,
    end_index: int,
    rendered_window: str,
    peek_index: int,
    rendered_peek: str,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the continuity judge's user prompt."""
    if lang == "en":
        scope_section = f"## Current sub-task scope\n\n{scope}\n\n" if scope else ""
        return (
            f"## Global query (background only)\n\n{query}\n\n"
            f"{scope_section}"
            f"## Candidate window\n\nChain indices [{start_index}, {end_index}] "
            f"(inclusive)\n\n{rendered_window}\n\n"
            f"## Node awaiting judgment (peek)\n\nChain index [{peek_index}]\n\n"
            f"{rendered_peek}\n"
        )
    scope_section = f"## 当前子任务作用域\n\n{scope}\n\n" if scope else ""
    return (
        f"## 全局 query（仅作背景）\n\n{query}\n\n"
        f"{scope_section}"
        f"## 候选窗口\n\n链内下标 [{start_index}, {end_index}]（含两端）\n\n"
        f"{rendered_window}\n\n"
        f"## 待判定节点（peek）\n\n链内下标 [{peek_index}]\n\n{rendered_peek}\n"
    )


# --------------------------------------------------------------------------- #
# Trace2Segment: SegmentClassifier
# --------------------------------------------------------------------------- #

CLASSIFIER_SYSTEM_PROMPT_ZH = """\
你是一名子轨迹分类器。只判断这段已定界子轨迹的**主活动**属于哪一类。

按窗口里实际在做什么归类，不要用标题关键词硬套。长段常混合多种活动，仍选主活动。\
交付覆盖 HTML 报告、看板、CSV、zip，形态不换类型。

## 类型

- probe：确认表能不能用、结构是什么、分区到哪天，尚未稳定出数。
- fetch_transform：按已有口径过滤、关联、聚合并写出明细/汇总。数据流主干。
- verify_fix：对用户口播数、上一版 CSV 或参考报告做对账，并改条件重跑。
- metric_modeling：定义或修正渠道、新老客、GC、产品 code 归属，而不是第一次出全量明细。
- delivery：不再取新口径，把已有汇总收成用户可拿走的东西（报告 / 看板 / CSV / zip）。
- other：澄清/规划、资产盘点、洞察归因、非数据，或无法归入以上五类。

reason 用一句中文说明主活动是什么。
"""

CLASSIFIER_SYSTEM_PROMPT_EN = """\
You classify a delimited sub-trajectory by its **primary activity**.

Classify by what the window is actually doing, not by title keywords. Long \
windows often mix activities; still pick the primary one. Delivery covers \
HTML reports, dashboards, CSVs and zip packages alike.

## Kinds

- probe: checking whether tables exist, their shape, and how far partitions \
go; no stable extract yet.
- fetch_transform: filtering, joining and aggregating under an existing \
definition, then writing detail or summary. The data-flow trunk.
- verify_fix: reconciling against a user-stated number, a prior CSV or a \
reference report, then re-running with a fix.
- metric_modeling: defining or revising channel / new-vs-old / GC / product \
code ownership, not the first full extract.
- delivery: no new definition; packing existing totals into something the \
user can take (report / dashboard / CSV / zip).
- other: clarification / planning, asset inventory, insight, non-data, or \
anything that does not fit the five kinds.

Justify the primary activity in one sentence.
"""


def get_classifier_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for the six-way segment kind classifier."""
    return CLASSIFIER_SYSTEM_PROMPT_EN if lang == "en" else CLASSIFIER_SYSTEM_PROMPT_ZH


def build_classifier_user_prompt(
    *,
    query: str,
    scope: str | None,
    rendered_window: str,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the classifier's user prompt over a frozen window snapshot."""
    if lang == "en":
        scope_section = f"## Sub-task scope\n\n{scope}\n\n" if scope else ""
        return (
            f"## Global query (background only)\n\n{query}\n\n"
            f"{scope_section}"
            f"## Sub-trajectory\n\n{rendered_window}\n"
        )
    scope_section = f"## 子任务作用域\n\n{scope}\n\n" if scope else ""
    return (
        f"## 全局 query（仅作背景）\n\n{query}\n\n"
        f"{scope_section}"
        f"## 子轨迹\n\n{rendered_window}\n"
    )


# --------------------------------------------------------------------------- #
# Trace2Segment step 2: SegmentExtractor
# --------------------------------------------------------------------------- #

EXTRACTOR_ROLE_ZH = """\
你是一名子轨迹归纳者。抽取这段已定界子轨迹的元数据。只描述其中确实发生的事，\
使用中文；title / behavior / conclusion 必须非空。
"""

EXTRACTOR_FIELD_RULES_ZH = """\
## 字段写作规范

- title：描述这一小段的**局部业务目标**。
- input：该段**消费**的关键输入——用户口径、业务/ODPS 表、**前序段落已有**的上游\
产物。没有文件名可写时，用口径/表名简述亦可。
- behavior：回答「业务上数据/指标如何被处理」。
- conclusion：回答「得出了什么」——数据发现、校验结果或决策。若本段在等待用户补齐\
信息或材料，须写明已确认事项与当前阻塞的下一步。
- artifact：面向用户的最终数据集 / 报告 / 看板；查询脚本本身就是交付物时也可。
"""

EXTRACTOR_SHARED_TAIL_ZH = """\
## 各字段通用约束

- title：约 10 个词。**禁止**照抄全局 query，**禁止**「路由 / 委托 / 调用工具」等\
编排口吻。
- input：**markdown**，推荐无序列表。已作答的 `ask_user_question` 必须写入（选项\
标签与补充文本）。工作区文件写成「简述 + 文件名」（如「6 月 GAAP 明细 \
`june_gaap.csv`」），只写文件名，**禁止**完整路径、skill 路径与工具名。**禁止**把\
本段产出写进 input——本段刚写出的结果文件不是输入；确实没有上游输入才填 null。
- behavior：**markdown**，推荐有序步骤。主语是业务过程，不是 Agent。**禁止**出现\
工具名、子 Agent 名、skill 路径。
- conclusion：**markdown**，短段落；本段含计划时改用有序列表，见下方「计划节点」。\
不得与 behavior 重复；关键数字按下方「单位」与「数字颜色」书写。窗口里已经说出的\
门槛、基准、校验项数、模块构成、对照数字必须写入，见下方「窗口事实」。
- artifact：只收录本段写出的**关键产物**。探测文件、生成脚本、中间结果、supporting \
文件不要写入；没有则 null。

## artifact 格式

- **一项只写一个文件**；name 必须是候选列表中的**确切文件名**，不得发明候选\
之外的名字，不得并列多个文件。
- description：一句话说清这个文件是什么，**不超过 25 个字**，不要复述文件名。
- 无 workspace 路径的 ODPS 临时表、仅下载 URL、任务图、计划、口径结论不是文件，\
不要写入 artifact。

## 窗口事实（conclusion 不得收成进度汇报）

behavior / conclusion 是对窗口的压缩，不是另写一份状态。窗口里已经出现的下列事实\
必须写入 conclusion，禁止用过程句替代：

- 门槛与基准（如支持度 ≥ 30、lift ≥ 1.5、业务基线 299 户、指引金额）。
- 对照（修正前 → 后、名单户数 vs 基线、无 JOIN 合计 vs JOIN 后合计）。
- 校验或自检（过了几项、哪项未过）。
- 构成（五大板块、几张表、报告模块名、zip 内文件）。
- 既有汇总（36 张里 33 张可用）又有点名对象时，两者都写，不要互相替代。

禁止把 conclusion 收成「已完成 / 已掌握框架 / 已交付某文件 / 为后续提供基础 / \
工具不可用 / 已完成计划拟定」而不带上述事实。过程状态可以写在事实后面，不能单独成篇。

## 计划节点

窗口含创建或修订计划（窗口事实中列出「计划步骤」）时，conclusion 必须写出计划的\
具体内容，禁止只写「已完成计划拟定」：

- 先用一句话写计划目标与全局口径（时间窗、指标、数据源、交付形式——窗口有的都写）；
- 再用有序列表按顺序列出每一步：步骤名 + 该步关键口径（指标 / 维度 / 产出文件）\
+ 依赖或跳过条件；
- 修订计划时写清新增、修改、删除了哪些步骤及原因；
- 计划之外的结果（如提问失败、等待用户确认）写在列表之后。

## 单位

- 贡献度、占比、渗透率、环比/WoW 等比率类数字，按数据文件原样加 `%`。
- 人数、金额及其变动**不加**百分号。
- `pt` 只用于「率值从 A% 变到 B%」的差值；不要把 `%` 改写成 `pt`。
- 禁用模糊词（如「显著增长」「大幅下降」「略有波动」「基本稳定」），必须给出\
具体数值；禁止未经证实的推测。

## 数字颜色（禁止 HTML）

对结论与行为中的关键数字使用下列记号，不要写 `<span>` 或任何 HTML：

- 关键数值：`{blue:28.71}`
- 下降/负面：`{red:-12.3%}`
- 上升/正面：`{green:+4}`

文件名仍用行内代码，不要包进记号。

## Markdown 约束

input / behavior / conclusion 使用 GFM 子集：标题、列表、加粗、行内代码、表格；\
禁止裸 HTML。列表用换行书写，如 `1. …\\n2. …` 或 `- …`。

## 开头的上下文节点

窗口若以 `user`、`hint`（系统注入的用户引导）开头，这些节点只提供动机与上下文，\
不属于本段覆盖的工作：据它们理解这一段要做什么、input 是什么，但 behavior 与 \
conclusion 只概括其后真正被覆盖的业务处理与结论。已作答的 `ask_user_question` \
不是上下文节点，它是本段真正覆盖的工作。
"""

EXTRACTOR_ROLE_EN = """\
You summarize a delimited sub-trajectory into structured metadata. Describe \
only what actually happened; title, behavior and conclusion must be non-empty.
"""

EXTRACTOR_FIELD_RULES_EN = """\
## Field rules

- title: name the **local business goal**.
- input: what this segment **consumed** — user criteria, business/ODPS tables, \
or **upstream** products from earlier segments. Non-file inputs (criteria, \
table names) are fine when there is no filename to give.
- behavior: answer how data / metrics were processed.
- conclusion: answer what was concluded — a data finding, verification result, \
or decision. If this segment is waiting on the user, state what was confirmed \
and the blocked next step.
- artifact: a user-facing final dataset / report / dashboard, or a query script \
that is itself the deliverable.
"""

EXTRACTOR_SHARED_TAIL_EN = """\
## Rules for every field

- title: ~10 words. Never copy the global query; never use orchestration \
phrasing ("routed", "delegated", "called a tool").
- input: **markdown**, prefer a bullet list. An answered `ask_user_question` \
must go in (option labels and any custom text). Name workspace files as "short \
gloss + filename" (e.g. "June GAAP detail `june_gaap.csv`") — filenames only, \
**never** full paths, skill paths, or tool names. **Never** put this segment's \
outputs in input; a file it just wrote is not an input. Null only when there \
genuinely is no upstream input.
- behavior: **markdown**, prefer numbered steps. The subject is the business \
process, not the agent. Never name tools, sub-agents, or skill paths.
- conclusion: **markdown**, a short paragraph, or an ordered list when the \
window contains a plan — see Plan nodes below. Must not repeat behavior; key \
numbers follow the Units and Number colors rules below. Thresholds, baselines, \
check counts, composition and comparison numbers the window already stated must \
go in — see Window facts below.
- artifact: only **key products** this window wrote. Omit probe files, \
generator scripts, intermediate results, and supporting files; null if none.

## Artifact format

- **one file per entry**; name must be an exact filename from the candidate \
list — never invent names outside it, never join files.
- description: one short clause, **15 words at most**, never a restatement of \
the filename.
- ODPS temp tables without a workspace path, bare download URLs, task graphs, \
plans, and conclusions are not files — leave them out.

## Window facts (do not collapse the conclusion into a status update)

behavior and conclusion compress the window; they are not a separate status \
report. Facts the window already stated must reach the conclusion — a process \
sentence is not a substitute:

- thresholds and baselines (support ≥ 30, lift ≥ 1.5, a business baseline of \
299 accounts, a guidance amount).
- comparisons (before → after, extracted rows vs baseline, no-JOIN vs JOIN \
totals).
- verification or self-checks (how many items passed, which failed).
- composition (five sections, how many tables, report module names, files \
inside a zip).
- when both a summary (33 of 36 usable) and named objects exist, write both; \
neither replaces the other.

Never let the conclusion shrink to "done" / "framework understood" / \
"delivered some file" / "unblocks the next step" / "tool unavailable" / \
"plan drafted" without those facts. Process status may follow the facts; it \
may not stand alone.

## Plan nodes

When the window creates or revises a plan (the window facts list "plan \
steps"), the conclusion must spell out the plan. Never stop at "plan drafted":

- open with one sentence on the plan's goal and global definition (time \
window, metrics, data source, deliverable — whichever the window states);
- then an ordered list, one item per step in order: the step name, its key \
definition (metrics / dimensions / output file), and its dependencies or \
skip conditions;
- when revising, name which steps were added, changed or removed, and why;
- results outside the plan (a failed question, waiting on the user) come \
after the list.

## Units

- Ratio metrics (contribution, share, penetration, WoW/MoM, etc.) keep `%` as in \
the data file.
- Headcounts, amounts, and their deltas do **not** take a percent sign.
- Use `pt` only for the difference when a rate moves from A% to B%; never rewrite \
`%` as `pt`.
- No vague wording ("grew significantly", "dropped sharply", "mostly stable"); \
always give concrete numbers. Never speculate beyond the sub-trajectory.

## Number colors (no HTML)

Mark key numbers in behavior and conclusion with these tokens. Do not write \
`<span>` or any other HTML:

- key value: `{blue:28.71}`
- down / negative: `{red:-12.3%}`
- up / positive: `{green:+4}`

Filenames stay in inline code, not tokens.

## Markdown constraints

Write input / behavior / conclusion in a GFM subset: headings, lists, bold, \
inline code, tables; no raw HTML. Express list steps as newlines, e.g. \
`1. …\\n2. …` or `- …`.

## Leading context nodes

When the window opens with a `user` node or a `hint` (system-injected guidance), \
those nodes supply motivation and context, not work this segment covers. Read \
the local goal and the input from them, but let behavior and conclusion \
describe only the business processing and findings afterwards. An answered \
`ask_user_question` is not a context node — it is work this segment covers.
"""

_KIND_FIELD_RULES_ZH: dict[str, str] = {
    "probe": """\
## 字段写作规范（探查）

窗口在确认表能不能用、结构是什么、分区到哪天，尚未稳定出数。

- title：探查哪张或哪批表。
- input：目标表全名、用户指定的项目空间、用户补充的 code/口径。读了表或业务文件\
不得 null。
- behavior：试过哪些项目/表名（如 `ds_` vs `ads_`、`sales_cdm` vs \
`analytics_data`）；维表读到了哪些有用字段。
- conclusion：写清**哪张表 / 哪个项目空间 / 哪个查询能力**能用或不能用（如\
「`analytics_data.ads_gaap_revenue_di` 可查，分区到 20260531」或「项目 `sales_cdm` 无 \
List 权限」）。禁止只写「可用」「不可用」而不点名对象。采用了哪张表就写表全名。\
**错误类型必须分开写**：无表 / 无项目权限 / 查询工具不可用；下一步改查什么。禁止\
混成「缺少项目访问权限」。有汇总（可用/不可用各几张）时先写汇总再点名失败对象，\
两者都要。工具不可用时，窗口已确认的结构、分区、月份缺口仍须写入。
- artifact：探查通常没有关键产物，schema 摘录也不算，多数情况填 null。
""",
    "fetch_transform": """\
## 字段写作规范（取数与变换）

窗口在按已有口径过滤、关联、聚合并写出明细/汇总。这是数据流主干。

- title：取了什么数、按什么维度切。
- input：主表、维表、用户口径（时间、code、渠道、新老客）。读了表或文件不得 null。
- behavior：时间过滤用的**字段名和取值**（`thedate` 还是 `ds`）；JOIN 哪张表、\
什么键；按哪些维度聚合；采用哪个收入字段。看过但未采用的字段（如 `gaap_d_adj`）\
必须点名。
- conclusion：产出规模与结构锚点（总金额、分维度贡献、户数）；截断或权限缺口。\
解析报告时写入模块名、源表与分区、文件大小——窗口有的都要。禁止停在「为后续提供\
基础」。
- artifact：只留本段实际采用的最终数据。
""",
    "verify_fix": """\
## 字段写作规范（校验与纠错）

窗口在对用户口播数、上一版 CSV 或参考报告做对账，并改条件重跑。

- title：校验什么、相对什么基准。
- input：待验产物；用户口播数或上一版文件；源表。对照文件与基准数字必须写入，不得\
因规则已写在 behavior 而填 null。
- behavior：错在哪一条条件（字段名 + 取值）；修成了什么；重跑哪些月份。
- conclusion：修正前后数字；与用户基准是否仍有差；**哪些旧结论作废**。若窗口算了\
无 JOIN 合计 vs JOIN 后合计，必须写入。第二项发现（如名单户数 vs 业务基线）不得因\
已写了收入差额而省略。
- artifact：修正后的最终数据或报告（若本段写出）。
""",
    "metric_modeling": """\
## 字段写作规范（口径建模）

窗口主工作是定义或修正渠道、新老客、GC、产品 code 归属，而不是第一次出全量明细。

- title：建模的维度（渠道 / 新老客 / GC / 产品归属）。
- input：维表或事实表；用户给出的枚举或 code 归属。规则已写在 behavior 时，消费的\
表/口径仍须进 input，禁止 null。
- behavior：用哪一列、哪些值、边界（公海、其他、交叉购）；关联键；是否去掉某条\
过滤。**代理字段必须点名**（窗口若声明用 `gmt_time` 近似注册时间，不得省略）。
- conclusion：最终规则；未覆盖值如何归类；若窗口有收入分布，写下主口径与旧口径的差。\
方案要求的数量（几个 code、几类因子）与窗口里的分数量程，有则必须写。
- artifact：本段写出的口径定义或对照文件，仅当它是交付物时写入。
""",
    "delivery": """\
## 字段写作规范（交付 / 最终产物）

窗口不再取新口径，而是把已有汇总收成用户可拿走的东西。HTML 报告、看板、CSV、zip \
同属一类。

- title：交了什么产物、满足用户哪条要求。
- input：来源数据/文件；用户对产物的要求（版式、粒度、字段、筛选——窗口有哪条写\
哪条）。窗口有源文件时不得 null。
- behavior：「部分构成」按形态取值：报告/看板写章节或卡片名，数据包写文件清单\
（名称 + 行数/粒度）。各部分用了什么数据、相对上一版改了什么。
- conclusion：交了什么、由哪些部分组成、点过的数字/发现、口径（人群 / code / 时间 \
/ 分区）、自检（过了几项、哪项未过）、限制/未完成。禁止只写「已交付某文件」。用户\
提示若有「收尾回复」，必须锚定它：可用同窗口数字补密，不得丢掉回复已经说出口的内容；\
不抄全文、不写 NAS 路径。中间过程回复（「正在渲染」）不进 conclusion。
- artifact：最终文件。候选列表中的最终 html / zip / 看板，窗口里交了就必须写入；\
临时 hash 名不要写入。
""",
}

_KIND_FIELD_RULES_EN: dict[str, str] = {
    "probe": """\
## Field rules (probe)

The window is checking whether tables exist, their shape, and how far \
partitions go; no stable extract yet.

- title: which table or set of tables is being probed.
- input: full table names, the user-specified project space, any extra codes \
or definitions. Never null if a table or business file was read.
- behavior: which projects / table names were tried (`ds_` vs `ads_`, \
`sales_cdm` vs `analytics_data`); useful dimension-table columns.
- conclusion: name **which table / project space / query capability** is \
usable or not (e.g. "`analytics_data.ads_gaap_revenue_di` is queryable, partitions \
through 20260531" or "no List permission on project `sales_cdm`"). Never open \
with a bare "usable" / "unusable". If a table was adopted, write its full \
name. **Keep error types apart**: no table / no project permission / query \
tool unavailable; what to query next. Never collapse them into "missing \
project access". When a summary exists (how many usable / unusable), write it \
first, then name the failures — both. If the query tool is down, still write \
structures, partitions, or month gaps the window already confirmed.
- artifact: probe windows rarely have a key product; a schema dump is not one \
either. Usually null.
""",
    "fetch_transform": """\
## Field rules (fetch and transform)

The window filters, joins and aggregates under an existing definition, then \
writes detail or summary. This is the data-flow trunk.

- title: what was fetched and on which dimensions.
- input: fact and dimension tables, user criteria (time, code, channel, \
new-vs-old). Never null if a table or file was read.
- behavior: the **field name and value** of the time filter (`thedate` vs \
`ds`); which table was joined and on which key; aggregation dimensions; which \
revenue field was adopted. Fields inspected but rejected (e.g. `gaap_d_adj`) \
must be named.
- conclusion: output scale and structural anchors (total amount, \
per-dimension contribution, headcount); truncation or permission gaps. When \
parsing a report, keep module names, source table and partition, and file size \
— whatever the window has. Never stop at "unblocks the next step".
- artifact: only the adopted final dataset.
""",
    "verify_fix": """\
## Field rules (verify and fix)

The window reconciles against a user-stated number, a prior CSV or a \
reference report, then re-runs with a fix.

- title: what is being verified and against which baseline.
- input: the product under test; the user's number or the prior file; the \
source table. Comparison files and baseline numbers must go in — never leave \
it null just because the rule already appears in behavior.
- behavior: which condition was wrong (field + value); what it became; which \
months were re-run.
- conclusion: before/after numbers; remaining gap to the user's baseline; \
**which old conclusions are void**. If the window computed no-JOIN vs JOIN \
totals, write them. A second finding (extracted rows vs business baseline) \
must not be dropped because an amount delta is already written.
- artifact: the corrected final data or report this window wrote.
""",
    "metric_modeling": """\
## Field rules (metric modeling)

The window defines or revises channel / new-vs-old / GC / product-code \
ownership, rather than producing the first full extract.

- title: the dimension being modeled.
- input: the dimension or fact table; enumerations or code ownership the user \
gave. Never null just because the rule already appears in behavior.
- behavior: which column, which values, the boundaries (open sea / other / \
cross-buy); the join key; any filter that was dropped. **Proxy fields must be \
named** (if the window says `gmt_time` stands in for registration time, keep \
that).
- conclusion: the final rule; how uncovered values are classified; if the \
window has a revenue split, the gap between the new and old definitions. \
Counts the plan required (how many codes, how many factor families) and score \
ranges in the window must be written when present.
- artifact: a definition or comparison file only when it is the deliverable.
""",
    "delivery": """\
## Field rules (delivery)

The window no longer takes a new definition; it packs existing totals into \
something the user can take. HTML reports, dashboards, CSVs and zip files \
share this kind.

- title: what was delivered and which user requirement it meets.
- input: source data or files; the user's product requirements (layout, grain, \
fields, filters — write whichever the window states). Never null when a source \
file was read.
- behavior: "parts" take the shape of the product: report/dashboard → section \
or card names; data package → file list (name + rows/grain). Which data each \
part uses, what changed versus the previous version.
- conclusion: what was delivered, its parts, the numbers or findings named, \
the definition (population / code / time / partition), checks (how many items \
passed, which failed), and limits. Never stop at "delivered some file". If the \
user prompt has a closing reply, anchor to it: densify with same-window \
numbers but do not drop what the reply already said; no full-text copy, no NAS \
paths. Mid-process replies ("still rendering") stay out.
- artifact: the final file. A final html / zip / dashboard on the candidate \
list that this window delivered must be written; omit hash temp names.
""",
}

_BOUNDARY_NOTE_ZH = {
    "natural": "自然切段：本段边界由连续性判定确认，内容应当完整。",
    "TaskStateUpdate": "强制截断：本段在子任务状态切换处被切断，可能不完整，请如实\
概括已经发生的部分。",
    "PlanUpdate": "强制截断：本段在计划被修订处被切断，可能不完整，请如实概括已经\
发生的部分。",
    "user_message": "强制截断：本段在用户下达新指令处被切断，可能不完整，请如实概括\
已经发生的部分。",
    "ask_user_question": "强制截断：本段止于向用户提问，可能不完整，请如实概括提问\
之前已经发生的部分。",
    "max_span": "强制截断：本段因长度上限被切断，可能不完整，请如实概括已经发生的\
部分。",
    "session_end": "强制截断：本段在会话结束处收尾，可能不完整，请如实概括已经发生\
的部分。",
}

_BOUNDARY_NOTE_EN = {
    "natural": "Natural boundary: confirmed by continuity judgment; the segment "
    "should be complete.",
    "TaskStateUpdate": "Forced cut at a sub-task state switch; the segment may "
    "be incomplete — describe faithfully what did happen.",
    "PlanUpdate": "Forced cut where the plan was revised; the segment may be "
    "incomplete — describe faithfully what did happen.",
    "user_message": "Forced cut where the user sent a new instruction; the "
    "segment may be incomplete — describe faithfully what did happen.",
    "ask_user_question": "Forced cut where the agent asked the user a question; "
    "the segment may be incomplete — describe faithfully what happened before "
    "the question.",
    "max_span": "Forced cut at the length cap; the segment may be incomplete — "
    "describe faithfully what did happen.",
    "session_end": "Forced cut at session end; the segment may be incomplete — "
    "describe faithfully what did happen.",
}


def get_extractor_system_prompt(
    lang: PromptLang = DEFAULT_PROMPT_LANG, *, kind: str = "other"
) -> str:
    """System prompt for segment metadata extraction.

    Each known kind rewrites the field-writing rules with what that kind must
    name; ``other`` and unknown kinds fall back to the generic rules. The
    constraints that hold for every kind — per-field shape, artifact format,
    window-fact retention, units, colors and markdown — stay in the tail.
    """
    if lang == "en":
        role = EXTRACTOR_ROLE_EN
        fields = _KIND_FIELD_RULES_EN.get(kind) or EXTRACTOR_FIELD_RULES_EN
        tail = EXTRACTOR_SHARED_TAIL_EN
    else:
        role = EXTRACTOR_ROLE_ZH
        fields = _KIND_FIELD_RULES_ZH.get(kind) or EXTRACTOR_FIELD_RULES_ZH
        tail = EXTRACTOR_SHARED_TAIL_ZH
    return f"{role}\n{fields}\n{tail}"


def build_extractor_user_prompt(
    *,
    query: str,
    scope: str | None,
    boundary_reason: str,
    rendered_window: str,
    candidate_files: list[str] | None = None,
    facts: str | None = None,
    closing_reply: str | None = None,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the segment extractor's user prompt over a frozen window snapshot."""

    names = sorted(candidate_files or [])
    english = lang == "en"
    if english:
        scope_section = f"## Sub-task scope\n\n{scope}\n\n" if scope else ""
        note = _BOUNDARY_NOTE_EN.get(boundary_reason, "")
        if names:
            listed = "\n".join(f"- `{name}`" for name in names)
            candidates = (
                "## Files this window wrote\n\n"
                "artifact may use only these exact names. Omit supporting and "
                "intermediate files.\n\n"
                f"{listed}\n\n"
            )
        else:
            candidates = (
                "## Candidate workspace files\n\nNone — set artifact to null.\n\n"
            )
        facts_section = (
            f"## Window facts (must be named)\n\n{facts}\n\n" if facts else ""
        )
        closing = (
            "## Closing reply (conclusion must be anchored here)\n\n"
            f"{closing_reply}\n\n"
            if closing_reply
            else ""
        )
        return (
            f"## Global query (background only — do not copy into title)\n\n"
            f"{query}\n\n"
            f"{scope_section}"
            f"## Boundary\n\n{note}\n\n"
            f"{candidates}"
            f"{facts_section}"
            f"{closing}"
            f"## Sub-trajectory\n\n{rendered_window}\n"
        )
    scope_section = f"## 子任务作用域\n\n{scope}\n\n" if scope else ""
    note = _BOUNDARY_NOTE_ZH.get(boundary_reason, "")
    if names:
        listed = "\n".join(f"- `{name}`" for name in names)
        candidates = (
            "## 本段写入的文件\n\n"
            "artifact 只能用这些确切文件名。supporting / 中间文件不要写入。\n\n"
            f"{listed}\n\n"
        )
    else:
        candidates = "## 候选工作区文件\n\n无 — artifact 填 null。\n\n"
    facts_section = f"## 窗口事实（必须点名）\n\n{facts}\n\n" if facts else ""
    closing = (
        f"## 收尾回复（conclusion 必须锚定）\n\n{closing_reply}\n\n"
        if closing_reply
        else ""
    )
    return (
        f"## 全局 query（仅作背景，勿抄入 title）\n\n{query}\n\n"
        f"{scope_section}"
        f"## 定界原因\n\n{note}\n\n"
        f"{candidates}"
        f"{facts_section}"
        f"{closing}"
        f"## 子轨迹\n\n{rendered_window}\n"
    )


__all__ = [
    "DEFAULT_PROMPT_LANG",
    "PresentationTask",
    "PromptLang",
    "build_classifier_user_prompt",
    "build_continuity_user_prompt",
    "build_extractor_user_prompt",
    "build_family_call_prompt",
    "build_family_result_prompt",
    "build_presentation_summary_prompt",
    "build_tool_output_prompt",
    "build_tool_running_prompt",
    "get_classifier_system_prompt",
    "get_continuity_system_prompt",
    "get_extractor_system_prompt",
    "get_file_write_call_system_prompt",
    "get_file_write_result_system_prompt",
    "get_presentation_system_prompt",
    "get_semantic_call_system_prompt",
    "get_semantic_result_system_prompt",
    "get_shell_call_system_prompt",
    "get_shell_result_system_prompt",
    "get_sql_call_system_prompt",
    "get_sql_result_system_prompt",
    "get_thinking_presentation_system_prompt",
    "get_tool_output_presentation_system_prompt",
    "get_tool_presentation_system_prompt",
]
