---
name: topicdock
description: 零依赖跨客户端主题记忆 Skill——以「主题」替代客户端割裂的「会话」，td use/match 挂载、td trace 追根、td save 沉淀，跨 Claude Code / Cursor / Codex / OpenCode 即插即续。
---

# TopicDock（`td`）— 跨客户端主题记忆

零依赖（Python ≥ 3.9，仅标准库）。唯一可执行形态：单文件 `scripts/topicdock.py`（别名 `td`）。
语法规范见 `references/markdown-spec.md`；11 个子命令与 JSON 载荷/输出契约见 `references/cli-contract.md`。

## 常驻唤醒守则（3 行，会话全程有效，压缩后仍须遵守）

- **TopicDock 调取（Read）**：仅在会话开启首个实质话题、用户点名（`继续 <id|序号>` / `/topicdock`）或中途切入**未挂载的新话题**时，加载 `topicdock` Skill 调用 `td use` 或拆解关键词调用 `td match`（`td match` 含 `--recent` 定位到目标 `topic_id` 后，必须同轮紧接着调一次 `td use <topic_id>` 获取完整 `base_sha` 完成正式挂载）；已挂载主题在同会话同角色后续轮次直接对话，**0 次重复查库与读取**（若发生上下文压缩 Compaction 丢失正文，或同会话切换了 Agent 角色，凭上一条消息末尾的 `⚓ TopicDock: <id> (<head_hop>@<sha6>)` 重调一次 `td use <id>` 刷新完整凭证）。
- **TopicDock 追根（Trace）**：凡 ① 用户质疑、推翻或深挖某条已挂载结论 `[C]`、排除项 `[R]`、待决 `[Q]` 或行动 `[A]`（含已折叠/已推翻/已合流项）；② 跨会话/跨角色接手未决 `[Q]` 或断点 `[~][A]` 需读取前序跳的多行 `推导与权衡` 与 `核心证据`；③ 基于既有 `[C]/[R]` 继续深化设计或编码而顶层单行摘要不足以确认边界细节时，必须先调用 `td trace <topic_id> <item_id>` 全量拉取底层 `hop-NNN.md` 历次证据块，严禁仅凭顶层一句话摘要臆测。
- **TopicDock 沉淀（Write）**：仅由**顶层主 Agent**（严禁 Subagent 调用，且禁止用户手改 Markdown 文件）在触达 4 类自然收敛节点——① 达成/推翻 `[C]/[R]` 定论；② 浮现需用户确认或需下一会话/下一阶段/另一角色拍板的未决问题 `[Q]`；③ `[A]` 阶段完成 `[x]` 或停在自然交接断点 `[~]`；④ 话题切换或收尾——时**当轮回复前立即调用 `td save`**（严禁先口头询问“是否交接/保存”再等下一轮；同会话同 `actor` 后续增量默认传 `--amend` 原地更新当前跳，跨会话或切换 `actor` 不传 `--amend` 自动新建下一跳）；并在回复末尾回显一行 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。

## 工作流契约

### 挂载时机（Read）
- 会话开启首个实质话题、用户点名（`继续 <id|序号>` / `/topicdock`）、或中途切入**未挂载的新话题**时才调 `td use` / `td match`；已挂载主题同会话同角色后续轮次直接对话，0 次重复查库与读取。
- `td match` 定位到目标 `topic_id` 后，必须**同轮紧接着调一次 `td use <topic_id>`** 获取完整 `base_sha` 完成正式挂载，并按需对未决 `[Q]` / 断点 `[~][A]` 调 `td trace` 拉取前序富文本块再进入讨论；纯时间指代（如「继续刚才讨论的」）单传 `td match --recent` 免 `--topic` 直取最近 ≤5 个主题，排序与 `list` / `use` 同。
- 上下文压缩（Compaction）导致已挂载 `topics/<id>.md` 正文丢失，或同会话切换 Agent 角色需获取完整 64 位 `base_sha` 时，凭上一条消息末尾的 ⚓ 行重调一次 `td use <id>` 刷新完整账本与凭证。
- `td use` 无参：库内 0 个主题 → 引导建档；仅 1 个 → 直通挂载；≥2 个 → 展示第 1 页列表（列表在上含序号|标题|id|更新时间，底部自然语言操作提示：回复序号挂载 / `删 <序号>` 触发 dry-run 预览并经显式 `topic_id` 确认执行 / 下一页或全部 / 关键词过滤；合并不做列表一级操作，不暴露 CLI 语法）。

### 追根时机（Trace）
- 三类触发任一命中即先追根：① 用户质疑、推翻或深挖某条已挂载条目（`[C]`/`[R]`/`[Q]`/`[A]`，含已折叠/已推翻/已合流项）的原始证据与推导过程；② 跨会话/跨角色接手未决 `[Q]` 或断点 `[~][A]`，需读取前序跳的多行 `推导与权衡` 与 `核心证据`；③ 基于既有 `[C]/[R]` 继续深化设计或编码而顶层单行摘要不足以确认边界细节时。
- 命中后必须先调 `td trace <topic_id> <item_id>` 全量拉取底层 `hop-NNN.md` 历次证据块再作答；严禁凭顶层行尾一句话摘要臆测历史。

### 沉淀时机（Write）
- 触达 4 类自然收敛节点时**当轮回复前立即调 `td save`**，严禁先口头询问「是否交接/保存」再等下一轮：① 达成/推翻 `[C]/[R]` 定论；② 浮现需用户确认或需下一会话/下一阶段/另一角色拍板的未决问题 `[Q]`；③ `[A]` 阶段完成 `[x]` 或停在自然交接断点 `[~]`；④ 话题切换或收尾。
- 同会话同 `actor` 后续增量默认传 `--amend` 原地更新当前跳；跨会话或切换 `actor` 不传 `--amend`，自动新建下一跳（已封板或 session 失配时亦自动降级新建，无需干预）。
- 保存成功后在回复末尾回显 ⚓ 行：`⚓ TopicDock: <id> (<head_hop>@<base_sha前6位>)`（JSON 输出的 `echo` 字段为该行权威来源；`base_sha` 完整 64 hex 须从 JSON `base_sha` 字段抄取，禁止截断自拼）。
- `td save` 是唯一权威写入入口（`--stdin | --json-file <path>`，可选 `--amend / --fork-as <new_id> / --force-merge`）；`td merge` 是唯一例外入口（跨主题合流）。
- **`next_focus` 通用格式**：写「事」不绑「人」，固定为一句话自包含的下一跳首步任务（如 `"围绕 [Q1] 确认 hop model 字段在 v1 下的兼容策略，随后推进 [A2] 编写实现计划"`）；仅多角色宿主且存在明确分工建议时，句尾自然附注建议角色。
- **`actor` 与 `model` 溯源填法**：`actor` 填 `<客户端>`（如 `claude-code`、`cursor`、`codex`）或 `<客户端>/<角色>`（如 `opencode/architect`），不拼 `@<model>` 后缀；当前模型标识可获知时填入顶层可选字段 `"model"`（如 `"opencode-go/gemini-4-argon"`），不可获知时省略 `"model"` 键。

### 蒸馏标准：五必收 / 四不收
**五必收（保障跨窗口/跨 Agent 切换 0 有效上下文丢失）**：
1. **已拍板结论及具体规格子项 → `[C]`**：`statement + basis` 必须高密度自包含（写出具体规格要点，禁止空洞标题）；多子项细节与权衡写入 hop 块 `rationale`（推导与权衡）。
2. **已查证的客观事实与代码/约束基线 → `[C] (verified)` 或 `evidence_detail`**：会话中查清的关键源码路径/行号、既有接口契约、实验/压测数据与硬约束，必须作为 `verified` 事实或对应条目 `evidence_detail`（核心证据）落盘，省去下一会话重新翻代码的开销。
3. **已否决/被推翻的备选方案及死因 → `[R]`**：写明放弃原因 `basis` 与 hop 块 `review_cond`（重审条件：除非何种前提改变否则不再重开），防止下一会话重复提议已否决方案。
4. **待决问题的完整决策上下文 → `[Q]`**：`statement` 写全待决问题与需拍板的子维度清单，hop 块 `rationale / evidence_detail` 写清已掌握的候选方向利弊与风险点。
5. **宏观阶段断点与外部落地工件 → `[A]` + `artifacts` + `next_focus`**：`[C]/[R]/[Q]/[A]` 任何轨道涉及外部落地文档（设计记录、计划、核心规范等），当次 `save` 必须如实附入 `artifacts`（`name + path + note`，0~n 个）并在条目正文/依据中点名文档名；未完成阶段标 `[~][A]` 并写明断点。

**四不收（保障高信噪比，严禁噪音入库）**：
1. 不收无果的中间探索与试错流水账（找错文件、临时报错重跑的过程不入库，只收最终查实结论）；
2. 不收同会话内已作废的过渡口风（同跳内通过 `td save --amend` 原地收敛为最终定论）；
3. 不收会话内微观执行步骤（`todowrite` 细项、单测/lint 修复循环、Subagent 内部过程严禁入 `[A]`）；
4. 不收外部长文档的正文副本（已落盘至 `design.md / plan.md` 的长文仅通过 `artifacts` 挂路径引用）。

### 话题粒度：三元分合平衡准则（防碎片化 & 防无关话题粘连）
1. **同题内聚演进（默认主路径：`td save [--amend]`）**：凡共享同一最终交付目标或同一系统变更闭环的子问题、边界修补、实现细节或顺手牵出的关联增量，一律留在当前主题内（通过新增/解决 `[Q]`、追加 `[C]/[A]` 演进）。**防碎片铁律**：严禁为单个子问题、单次 Bug 修复或局部小增量新建或分叉主题。
2. **同源支线裂变（罕见重操作：`td save --fork-as <new_id>`）**：双硬门槛须同时满足——① 强背景依赖（新支线必须建立在当前主题已拍板的 `[C]/[R]` 基座之上）；② 独立生命周期（该支线拆为二期独立专项或长期平行探索方向，不再与主线同步验收，继续留主线会阻塞主线收口或使 `[Q]/[A]` 失焦）。`--fork-as` 会复制源 `base_hop` 四轨条目（`op=inherit`）并封板源跳，代表单向裂变独立演进（分叉同跳可在 `mutations` 中 `retract` 不属于支线的原主线未决 `[Q]/[A]`；因 `td merge` 不按 `origin` 对共享祖先去重，分叉出的支线不走 `td merge` 回并原题）。
3. **异源硬切独立新题（防大杂烩红线：`td match` → `td use` 或 `base_hop: null` 新建）**：当用户在同会话中途切入的新话题，与当前已挂载主题既无共同交付目标、也无需继承任何 `[C]/[R]` 背景时，严禁混写进当前主题。必须先将原主题未落盘增量（若有）`td save` 收口，再对新话题调 `td match` 查重：命中既有主题则 `td use` 切换挂载；未命中则以 `base_hop: null` 新建独立主题，并在末尾 `⚓ TopicDock:` 行显式标出新 `topic_id`。
4. **事后纠偏兜底**：若不同窗口针对同一课题误建了两个独立主题，调 `td merge <src_id> <dst_id>` 合一；若最近 1~3 步分叉/合并/保存有误，调 `td undo` 一键回滚。

### Subagent 禁令
- **严禁 Subagent 调用 `td match / save`**；沉淀与检索仅由顶层主 Agent 执行。
- 严禁将会话内部的 `todowrite`、`plan` 模式、superpowers 或 Subagent 微观步骤写入行动轨 `[A]`——TopicDock 不接管微观执行循环。

### 用户手改 Markdown 禁令
- `topics/<id>.md` 与 `hops/hop-NNN.md` 仅允许经 `td` 命令修改；任何手动修改一律视为**文件破坏**——能从 `db + hops` 重放还原的自动覆写修复（含从最新跳头部快照 100% 还原标题与关联文件链接），无法修复或改了 `topic_id` 的直接级联删除。手改后请立即跑 `td doctor` 自愈。

### 5C 失步吸收协议（`STALE_BASE`，退出码 4）
- 另一窗口已推进（新跳或 amend）导致 `(base_hop, base_sha)` 失步时，`td save` 返回 `STALE_BASE` 载荷，**绝不静默应用**。
- `auto_absorbable: true`（`conflicts: []`，即本载荷与 base 之后各跳的变更条目无交集且头部字段无冲突）→ **换新 base 重发即吸收**：凭载荷中 `current_head` / `current_sha` 重新挂载（必要时 `td use` 刷新正文），以 `base_hop: <current_head>`、`base_sha: <current_sha>` 重发同一载荷。
- `auto_absorbable: false`（`conflicts` 非空，含特例 `reason: "BASE_SHA_MISMATCH_HEAD"`——同 head 被另一窗口 amend）→ 提示用户二选一：
  - **分叉**：`--fork-as <new_id>` 从 base_hop 处的状态分叉新主题（无覆盖风险）；
  - **覆盖**：`--force-merge` 以当前 head 为基重放（最后写入胜；响应附 `overwritten[]` 被覆盖条目清单；载荷引用已被推翻/撤回而消失的 item_id → `INVALID_INPUT` + `vanished_items` 清单，退出码 7）。
- `create` 不构成冲突；冲突判据详见 `references/cli-contract.md` STALE_BASE 节。

### Bootstrap 引导（`UNINITIALIZED_STORE`，退出码 3）
- 向上冒泡与全局目录均未找到库时（除 `td init` 外的命令），主 Agent **询问用户一次**：建立全局统一库 `~/.topicdock/`（默认），或当前目录本地库 `<cwd>/.topicdock/`，或 `--path <dir>` 自定义路径；随后调 `td init --scope global|local [--path <dir>]` 初始化。

### 自愈（heal）自动处置简述
- 每条命令启动均在排他锁下自动执行七步自愈（DB 缺失→冷重建 → meta 修复 → 崩溃纳管 → 野生文件清理 → 顶层 md 修复 → 引用中 hop 分类处置 → 孤儿 hop 清理）：能修则修、不能修或 ID 被改则级联删除，损坏主题绝不阻塞好主题。
- `hop-NNN.md` 误删 = 断链跳过不级联（trace / view --history / delete 标注缺失跳号）；关联外部文件缺失 → 同名探测或 `[⚠️ 外部文件不存在，跳过读取]` 告警，save/doctor 自动摘除死链。
- 巡检详情与强制冷重建：`td doctor [--rebuild]`。
