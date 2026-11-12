# Section 生命周期

基线 Section 状态依次为：

```text
pending → running → validating → approved
             ↓           ↓
        needs_input ←─────┘
             ↓
           running
```

任意执行态可以进入 `failed`，`failed` 可由后续会话恢复到 `running`。

- `pending`：尚未开始，且依赖可能未完成。
- `running`：正在反推问题、取数、分析或修订。
- `needs_input`：存在会改变统计结果的用户决策。
- `validating`：草稿（含可选的顶部「复现差异与数据缺口说明」块）已生成，等待确定性验收。
- `approved`：当前 Section 已满足验收条件（数值/结构比对通过，**且**证据门禁判为
  `重算`；门禁未过时只能补证据重算，不能人工放行进入该状态）。
- `failed`：工具或数据源失败；错误必须记录在 manifest。

状态迁移全部由 `report_cli.py` 执行：`rr update-section` 把 Section 推到
`validating` 或 `needs_input`，`rr compare-section` **只写 `baseline/comparison.json`
不改状态**，只有 `rr apply-comparison` 才把状态落定为 `approved`（passed）或
`failed`。两者都先过**证据门禁**：`queries/*.sql`（含 `SELECT`）或 `baseline/data/*`
缺失、证据是 `source.md`/`draft.md` 拷贝、或草稿声明 `【无法复现】` 时判
`provenance=原报告`、`passed` 强制为 `false`，此时回到 `running` 补真实取数证据、
重写草稿后重跑（**不能人工放行**，`--approved-by-user` 会被拒绝）；只有门禁通过才
进入数值验收与 `ask_user_question` 放行逻辑（见 SKILL.md「证据门禁」）。Agent 不得
手工编辑 manifest 状态。

Plan 状态只用于用户可见进度，不能替代 manifest。

首次报告解析 chat 只允许完成澄清与 Plan，不进入任何基线 Section 状态。
之后每个基线 chat 只能绑定一个 `section_id`；该 Section 通过后停止并等待用户
确认下一项。该约束由脚本根据 `--chat-id`（取自环境说明中的当前 chat_id）落盘
`turns/{chat_id}.json` 强制：已绑定某个 Section 的 chat 去推进另一个 Section、或去
运行 `freeze-recipe` / `prepare-run` / `compile-report` / `schedule-descriptor`，均会
直接失败。

新周期 Run 使用独立的 `runs/{period}/run_manifest.json`：

```text
pending → running → checking → approved
             ↓          ↓
        needs_input ←────┘
```

- `checking` 只表示数据证据、冻结查询、口径和完整性检查；
- Run 不进入基线 `validating`，不与原报告 GT 数值比较；
- 基线 `approved` 不代表任意新周期已经完成；
- 每个 period 的状态相互隔离，新周期不得覆盖基线或其他 Run。
- Plan 最后的目标周期完整测试是基线单 Section 规则的例外：运行
  `rr prepare-run --period <period>` 后，同一 chat 可以依次推进该 period 的全部
  Section（`rr update-section --run-period` + `rr check-run`），全部 `approved` 后
  直接编译完整 HTML。该 chat 从此绑定这个 period，换一个周期必须换一个 chat。
