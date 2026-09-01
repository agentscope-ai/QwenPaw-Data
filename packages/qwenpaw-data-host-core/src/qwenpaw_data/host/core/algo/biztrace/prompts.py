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
用户的简短说明，供前端卡片直接展示。

写作要求：

- 用简洁的自然语言陈述，不要使用列表符号、标题或代码块；
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
- describe only information actually present; never speculate;
- no bullet lists, headings, or code blocks.
"""

TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_ZH = """\
你是一名业务轨迹卡片撰写者，只负责概括工具调用的出参或失败原因。

写作要求：

- 只填写 output_summary：一句或一段话概括结果（成功）或错误原因（失败），不超过 200 字；
- 调用信息、卡片标题、目的、入参已在卡片其他位置展示，禁止在 output_summary 中重复，\
也禁止写出「卡片小标题」「目的」「输入摘要」「输出摘要」等字段名；
- 只描述出参 / 错误中确实存在的信息，不臆测；
- 不使用列表符号、标题或代码块，使用中文。
"""

TOOL_OUTPUT_PRESENTATION_SYSTEM_PROMPT_EN = """\
You write the result section of a business-facing tool card.

Requirements:

- Fill only output_summary: one short paragraph on the result (success) or the \
error (failure), at most 200 characters;
- Caption, purpose, and input are already shown elsewhere on the card — do not \
repeat them, and do not write field labels such as "caption", "purpose", \
"input_summary", or "output_summary";
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


def build_tool_running_prompt(
    *,
    tool_name: str,
    tool_input: str,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the user prompt for a call card's caption, purpose and input."""
    if lang == "en":
        return f"## Tool\n\n{tool_name}\n\n## Arguments\n\n{tool_input}\n"
    return f"## 工具\n\n{tool_name}\n\n## 入参\n\n{tool_input}\n"


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

    if lang == "en":
        state = (
            "The call FAILED; summarize only the error from the Result."
            if failed
            else "The call succeeded; summarize only the Result."
        )
        return (
            f"## Instruction\n\nWrite output_summary only. Do not restate the "
            f"tool name, purpose, or The call section.\n\n"
            f"## Tool\n\n{tool_name} — {purpose}\n\n"
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
        f"## 任务\n\n只填写 output_summary。"
        f"不要重复工具名、目的或「调用信息」中的内容，也不要输出字段名。\n\n"
        f"## 工具\n\n{tool_name} — {purpose}\n\n"
        f"## 调用信息（仅供参考，勿重复）\n\n{call_context or '-'}\n\n"
        f"## 结果状态\n\n{state}\n\n"
        f"## 出参\n\n{tool_output}\n"
    )


# --------------------------------------------------------------------------- #
# Trace2Segment step 1: ContinuityJudge
# --------------------------------------------------------------------------- #

CONTINUITY_SYSTEM_PROMPT_ZH = """\
你是一名轨迹分段判定官。给定一个候选窗口和一个前瞻节点，只判断前瞻节点是否与窗口\
属于同一个子任务。你不需要写摘要。

## 子任务的定义

子任务是最小的、语义完整的工作单元，其目标应能脱离上下文被独立理解。

一个子任务不应只由一句收尾汇报、交付确认或状态确认构成；这类内容归入它所总结的\
那段工作。

## 节点粒度

前瞻节点默认对应一整条 AgentScope 消息气泡（同一次回复内的思考、文本、工具调用与\
结果已按时间序展开在同一个节点里）；若该气泡内出现了硬边界，节点则是它的一个片段。

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
You are a trajectory segmentation judge. Given a candidate window and one peek \
node, decide only whether the peek belongs to the same sub-task as the window. \
You do not write summaries.

## What a sub-task is

A sub-task is the smallest semantically complete unit of work; its goal should be \
understandable on its own, without surrounding context. A sub-task must never \
consist only of a closing report, delivery confirmation, or status check; such \
content belongs to the work it wraps up.

## Node granularity

The peek node is by default one whole AgentScope message bubble: the thinking, \
text, tool calls and tool results of a single reply, expanded in time order \
inside that node. When a hard boundary occurred inside that bubble, the node is \
one fragment of it instead.

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
# Trace2Segment step 2: SegmentExtractor
# --------------------------------------------------------------------------- #

EXTRACTOR_SYSTEM_PROMPT_ZH = """\
你是一名子轨迹归纳者。给定一段已经定界的子轨迹，抽取它的元数据摘要。你不做任何\
边界判断——边界已经确定。只描述子轨迹中确实发生的事，使用中文；title / behavior /\
 conclusion 必须非空。

## 字段写作规范

- title：约 10 个词，描述这一小段的**局部业务目标**。**禁止**照抄全局 query，\
禁止「路由 / 委托 / 调用工具」等编排口吻。
- input：该段**消费**的关键输入的 **markdown**（推荐无序列表）——用户口径、业务/\
ODPS 表、**前序段落已有**的上游产物。有工作区文件时写成「简述 + 文件名」\
（如「6 月 GAAP 明细 `june_gaap.csv`」），只写文件名，**禁止**完整路径；没有\
文件名可写时用口径/表名简述亦可。确实没有输入则填 null。不要写 skill 路径或\
工具名。
  - 已作答的 `ask_user_question`：用户选择（选项标签、补充文本）必须写入，不得\
因没有表或文件而填 null。
  - **禁止**把本段产出写进 input：候选列表里的文件、本段将写入或刚写出的结果 \
CSV / SQL / 报告 / 看板一律只进 artifact；本段若只有取数/生成、没有真正的上游\
文件输入，input 就填 null，不要用结果文件「凑」一项。
- behavior：回答「业务上数据/指标如何被处理」的 **markdown**（推荐有序步骤）。\
主语是业务过程，不是 Agent。**禁止**出现工具名、子 Agent 名、skill 路径。
- conclusion：回答「得出了什么」——数据发现、校验结果或决策——的 **markdown**\
（短段落）。关键数字按下方「单位」与「数字颜色」规范书写。不得与 behavior 重复。\
若本段在等待用户补齐信息或材料，须写明已确认事项与当前阻塞的下一步。
- artifact：对本段**候选文件列表**打标（候选由系统给出，见用户提示）。候选即本段\
写入的文件，与 input 互斥。
  - **一项只写一个文件**；name 必须是候选列表中的**确切文件名**，不得发明候选\
之外的名字，不得并列多个文件。
  - description：一句话说清这个文件是什么，**不超过 25 个字**，不要复述文件名。
  - kind：`query_script`（SQL/查询脚本）/ `dataset`（CSV 等数据）/ `report` /
    `dashboard` / `other`（探测文件、生成脚本等）。
  - role：`final`（面向用户的最终产物）/ `intermediate`（中间结果）/
    `supporting`（支撑/探测）。
  - 无 workspace 路径的 ODPS 临时表、仅下载 URL、任务图、计划、口径结论不是文件，\
不要写入 artifact。
  - artifact 是真正的 JSON 数组，不要写成一个装着 JSON 的字符串。

## 单位

- 贡献度、占比、渗透率、环比/WoW 等比率类数字，按数据文件原样加 `%`。
- 人数、金额及其变动**不加**百分号。
- `pt` 只用于「率值从 A% 变到 B%」的差值；不要把 `%` 改写成 `pt`。
- 禁用模糊词（如「显著增长」「大幅下降」「略有波动」「基本稳定」），必须给出\
具体数值；禁止未经证实的推测。

## 数字颜色（仅允许下列 span）

对结论与行为中的关键数字使用 HTML `<span>` 着色（class 必须逐字一致）：

- 关键数值：`<span class="text-blue-600 font-bold">…</span>`
- 下降/负面：`<span class="text-red-500 font-bold">…</span>`
- 上升/正面：`<span class="text-green-600 font-bold">…</span>`

禁止其他 HTML 标签或 class。文件名仍用行内代码，不要包进 span。

## Markdown 约束

input / behavior / conclusion 各写成**一个字符串**，列表用换行书写（如 \
`1. …\\n2. …` 或 `- …`），**禁止**做成 JSON 数组。使用 GFM 子集：标题、列表、\
加粗、行内代码、表格；除上文允许的数字着色 `<span>` 外，禁止其他裸 HTML。

## 开头的上下文节点

窗口若以 `user`、`hint`（系统注入的用户引导）开头，这些节点只提供动机与上下文，\
不属于本段覆盖的工作：据它们理解这一段要做什么、input 是什么，但 behavior 与 \
conclusion 只概括其后真正被覆盖的业务处理与结论。已作答的 `ask_user_question` \
是本段输入，不按上下文节点忽略。
"""

EXTRACTOR_SYSTEM_PROMPT_EN = """\
You summarize a delimited sub-trajectory into structured metadata. You make no \
boundary decisions — the boundary is already fixed. Describe only what actually \
happened; title, behavior and conclusion must be non-empty.

## Field rules

- title: ~10 words naming the **local business goal**. Never copy the global \
query; never use orchestration phrasing ("routed", "delegated", "called a tool").
- input: **markdown** (prefer a bullet list) for what this segment **consumed** — \
user criteria, business/ODPS tables, or **upstream** products from earlier \
segments. When naming a workspace file, write "short gloss + filename" \
(e.g. "June GAAP detail `june_gaap.csv`"); filenames only — **never** full \
paths. Non-file inputs (criteria, table names) are fine. Null when there \
genuinely are none. No skill paths or tool names.
  - An answered `ask_user_question` must go into input (option labels and any \
custom text); do not set null for lack of tables or files.
  - **Never** put this segment's outputs in input: candidate files and anything \
this window writes (result CSVs, SQL, reports, dashboards) belong only in \
artifact. If the segment only fetches or generates and has no real upstream file \
input, set input to null — do not pad it with the result filename.
- behavior: **markdown** (prefer a numbered list) answering how data / metrics \
were processed. The subject is the business process, not the agent. Never name \
tools, sub-agents, or skill paths.
- conclusion: **markdown** answering what was concluded — a data finding, \
verification result, or decision (short paragraph). Key numbers follow the \
Units and Number colors rules below. Must not repeat behavior. If this \
segment is waiting on the user, state what was confirmed and the blocked next step.
- artifact: label the **candidate files** supplied in the user prompt. Candidates \
are files this segment wrote; they are mutually exclusive with input.
  - **one file per entry**; name must be an exact filename from the candidate \
list — never invent names outside it, never join files.
  - description: one short clause, **15 words at most**, never a restatement of \
the filename.
  - kind: `query_script` (SQL or query script) / `dataset` (CSV and the like) / \
`report` / `dashboard` / `other` (probe files, generator scripts).
  - role: `final` (user-facing product) / `intermediate` / `supporting`.
  - ODPS temp tables without a workspace path, bare download URLs, task graphs, \
plans, and conclusions are not files — leave them out.
  - emit artifact as a real JSON array, never as a string containing JSON.

## Units

- Ratio metrics (contribution, share, penetration, WoW/MoM, etc.) keep `%` as in \
the data file.
- Headcounts, amounts, and their deltas do **not** take a percent sign.
- Use `pt` only for the difference when a rate moves from A% to B%; never rewrite \
`%` as `pt`.
- No vague wording ("grew significantly", "dropped sharply", "mostly stable"); \
always give concrete numbers. Never speculate beyond the sub-trajectory.

## Number colors (only these spans)

Wrap key numbers in HTML `<span>` tags (class strings must match exactly):

- key value: `<span class="text-blue-600 font-bold">…</span>`
- down / negative: `<span class="text-red-500 font-bold">…</span>`
- up / positive: `<span class="text-green-600 font-bold">…</span>`

No other HTML tags or classes. Filenames stay in inline code, not spans.

## Markdown constraints

Write input / behavior / conclusion as **one string** each, with list steps as \
newlines (e.g. `1. …\\n2. …` or `- …`); never emit a JSON array of steps. Use a \
GFM subset: headings, lists, bold, inline code, tables; aside from the \
number-color `<span>` tags above, no raw HTML.

## Leading context nodes

When the window opens with a `user` node or a `hint` (system-injected guidance), \
those nodes supply motivation and context, not work this segment covers. Read \
the local goal and the input from them, but let behavior and conclusion \
describe only the business processing and findings afterwards. An answered \
`ask_user_question` is this segment's input — do not ignore it as context.
"""

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


def get_extractor_system_prompt(lang: PromptLang = DEFAULT_PROMPT_LANG) -> str:
    """System prompt for segment metadata extraction."""
    if lang == "en":
        return EXTRACTOR_SYSTEM_PROMPT_EN
    return EXTRACTOR_SYSTEM_PROMPT_ZH


def build_extractor_user_prompt(
    *,
    query: str,
    scope: str | None,
    boundary_reason: str,
    rendered_window: str,
    candidate_files: list[str] | None = None,
    lang: PromptLang = DEFAULT_PROMPT_LANG,
) -> str:
    """Build the segment extractor's user prompt over a frozen window snapshot.

    Args:
        query: Global user query, background only.
        scope: Sub-task scope description, when a plan is active.
        boundary_reason: Why the window was cut.
        rendered_window: Rendered snapshot of the delimited window.
        candidate_files: Filenames the segment actually saved; the model may
            only label names from this list.
        lang: Prompt language.
    """

    names = sorted(candidate_files or [])
    if lang == "en":
        scope_section = f"## Sub-task scope\n\n{scope}\n\n" if scope else ""
        note = _BOUNDARY_NOTE_EN.get(boundary_reason, "")
        if names:
            listed = "\n".join(f"- `{name}`" for name in names)
            candidates = (
                "## Candidate workspace files (label only these exact names)\n\n"
                f"{listed}\n\n"
            )
        else:
            candidates = (
                "## Candidate workspace files\n\nNone — set artifact to null.\n\n"
            )
        return (
            f"## Global query (background only — do not copy into title)\n\n"
            f"{query}\n\n"
            f"{scope_section}"
            f"## Boundary\n\n{note}\n\n"
            f"{candidates}"
            f"## Sub-trajectory\n\n{rendered_window}\n"
        )
    scope_section = f"## 子任务作用域\n\n{scope}\n\n" if scope else ""
    note = _BOUNDARY_NOTE_ZH.get(boundary_reason, "")
    if names:
        listed = "\n".join(f"- `{name}`" for name in names)
        candidates = f"## 候选工作区文件（只能标注这些确切文件名）\n\n{listed}\n\n"
    else:
        candidates = "## 候选工作区文件\n\n无 — artifact 填 null。\n\n"
    return (
        f"## 全局 query（仅作背景，勿抄入 title）\n\n{query}\n\n"
        f"{scope_section}"
        f"## 定界原因\n\n{note}\n\n"
        f"{candidates}"
        f"## 子轨迹\n\n{rendered_window}\n"
    )


__all__ = [
    "DEFAULT_PROMPT_LANG",
    "PresentationTask",
    "PromptLang",
    "build_continuity_user_prompt",
    "build_extractor_user_prompt",
    "build_presentation_summary_prompt",
    "build_tool_output_prompt",
    "build_tool_running_prompt",
    "get_continuity_system_prompt",
    "get_extractor_system_prompt",
    "get_presentation_system_prompt",
    "get_thinking_presentation_system_prompt",
    "get_tool_output_presentation_system_prompt",
    "get_tool_presentation_system_prompt",
]
