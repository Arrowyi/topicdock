# TopicDock (`td`)

*Your topics live in one dock — plug in Claude Code, Cursor, Codex, or OpenCode anytime to continue.*

[English](README.md)

**TopicDock 是一个零依赖的个人主题记忆底座：你的讨论、结论和进度按「主题」存成 Markdown 账本，换任何 AI Agent 客户端都能接着做。**

## 为什么需要 TopicDock

在 Claude Code 里聊了一半的方案，换到 Cursor 就得从头解释；在 Codex 里拍板的结论，OpenCode 并不知道。原因是进度被绑在各客户端自己的「会话（Session）」里，而会话换个窗口、换个客户端就断了。

TopicDock 把记忆的单位从客户端绑定的「会话」换成你自己拥有的「主题（Topic）」。主题以普通 Markdown 文件保存（`topics/<topic-id>.md` + `hops/hop-NNN.md`），由你的 Agent 通过 `td` 命令读写，不属于任何客户端。Claude Code / Cursor / Codex / OpenCode 都能随时挂载同一个主题，读到完整的结论、被排除的方案、待决问题和当前断点，然后接着干。

引擎只用 Python ≥ 3.9 标准库，没有第三方依赖；唯一的可执行文件是单文件 `scripts/topicdock.py`（别名 `td`）。

## 核心特性与概念

- **正交双轨账本（`[C/R/Q]` + `[A]`）**：认知与定论轨记录已拍板结论 `[C]`、已排除或被推翻的方案 `[R]`、待决问题 `[Q]`；行动与进展轨记录会话级阶段行动 `[A]`（`[ ]` 待办 / `[~]` 进行中断点 / `[x]` 已完成）。没有任何生命周期状态机，主题只要没删除，就永远平权、随时可续、随时可推翻。
- **双层 Markdown 账本 + 派生图库**：
  - `topics/<topic-id>.md` 是当前态的单页全景，行尾自动附带 ≤ 20 字符的演进短标，如 `(H001→H003)`。
  - `hops/hop-NNN.md` 保存每一跳（hop，一次保存产生的增量）的证据块与头部快照。
  - `topicdock.db`（SQLite WAL 预写日志模式）只是派生索引，提供毫秒级检索、DAG（有向无环图）引用计数和回滚栈，丢了可以从 Markdown 100% 冷重建。
- **向上冒泡就近寻址**：从当前工作目录逐级向父目录找 `.topicdock/`（或指针文件 `.agents/topicdock.json`），先找到哪一级就用哪一级；都没有就回退到全局默认库 `~/.topicdock/`；仍然没有则返回 `UNINITIALIZED_STORE`（退出码 3），引导建库。
- **锚点凭证行（`⚓`）**：每次 `td save`（或 `td merge`）保存后，主 Agent 在回复末尾回显 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。同一会话未压缩时不用重复查库；长会话发生上下文压缩（Compaction，旧对话被裁剪）后，凭这一行重调 `td use <id>` 即可恢复完整上下文。

## 三支柱工作流（Read / Trace / Write）

[`SKILL.md`](SKILL.md) 是工作流的权威规范，其中的 3 行常驻守则如下（`install.py` 会把它们注入工作区的 `CLAUDE.md` / `AGENTS.md`；`/topicdock` 命令模板同样指向这套三支柱工作流）：

- **TopicDock 调取（Read）**：仅在会话开启首个实质话题、用户点名（`继续 <id|序号>` / `/topicdock`）或中途切入**未挂载的新话题**时，加载 `topicdock` Skill 调用 `td use` 或拆解关键词调用 `td match`（`td match` 含 `--recent` 前 ≤ 5 个 `full: true` 命中项直接返回 `head_hop`、完整 64-hex `base_sha`、账本全文与 `artifacts`，单次 match 即完成正式挂载，免第 2 轮 `td use`；目标排第 6+ 才需补调一次 `td use <id>`）；已挂载主题在同会话同角色后续轮次直接对话，**0 次重复查库与读取**（若发生上下文压缩 Compaction 丢失正文，或同会话切换了 Agent 角色，凭上一条消息末尾的 `⚓ TopicDock: <id> (<head_hop>@<sha6>)` 重调一次 `td use <id>` 刷新完整凭证）。
- **TopicDock 追根（Trace）**：凡 ① 用户质疑、推翻或深挖某条已挂载结论 `[C]`、排除项 `[R]`、待决 `[Q]` 或行动 `[A]`（含已折叠/已推翻/已合流项）；② 跨会话/跨角色接手未决 `[Q]` 或断点 `[~][A]` 需读取前序跳的多行 `推导与权衡` 与 `核心证据`；③ 基于既有 `[C]/[R]` 继续深化设计或编码而顶层单行摘要不足以确认边界细节时，必须先调用 `td trace <topic_id> <item_id>`（多条目可单次调用：`td trace <topic> C1,C2,Q3`）全量拉取底层 `hop-NNN.md` 历次证据块，严禁仅凭顶层一句话摘要臆测。
- **TopicDock 沉淀（Write）**：仅由**顶层主 Agent**（严禁 Subagent 调用，且禁止用户手改 Markdown 文件）在触达 4 类自然收敛节点——① 达成/推翻 `[C]/[R]` 定论；② 浮现需用户确认或需下一会话/下一阶段/另一角色拍板的未决问题 `[Q]`；③ `[A]` 阶段完成 `[x]` 或停在自然交接断点 `[~]`；④ 话题切换或收尾——时**当轮回复前立即调用 `td save`**（严禁先口头询问“是否交接/保存”再等下一轮；同会话同 `actor` 后续增量默认传 `--amend` 原地更新当前跳，跨会话或切换 `actor` 不传 `--amend` 自动新建下一跳）；并在回复末尾回显一行 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。

```bash
td match --topic jwt --topic 刷新令牌      # 按关键词检索候选主题（0~100 打分，默认阈值 35）
td use auth-refactor                        # 挂载：返回账本全文 + head_hop + 完整 base_sha
td trace auth-refactor C2                   # 追根：C2 的全部历史证据块与去向
td save --stdin --json --amend < payload.json   # 同会话增量：原地更新当前跳
```

## /topicdock 单命令入口

安装后（见下文），在客户端直接输入 `/topicdock` 即可挂载/切换主题（单一命令，不用命令族）：

- **带参直通**：`/topicdock <id|序号>` → 直接调 `td use <id|序号>` 挂载目标主题，返回账本正文后按三支柱工作流继续。
- **无参引导**：`/topicdock` → 调 `td use` 无参引导流，按库内主题数分三种情形：
  - **0 个主题**：引导你说明课题并新建建档。
  - **恰好 1 个**：直通挂载。
  - **≥ 2 个**：展示第 1 页列表，列表行在上，格式 `序号 | 标题 | id | 更新时间`，底部是自然语言操作提示，按列表实际状态收敛：
    - 回复序号挂载；
    - 回复 `删 <序号>` 先出删除预览，经你显式回复该主题的 topic_id 确认后才真正删除；
    - 有后续页时可回复「下一页」或「全部」；
    - 直接给关键词则按关键词过滤。

  列表只用于选择与删除。
- **参数传递**：命令模板通过 `$ARGUMENTS` 接收命令后的附加参数。若参数形如 `<id|序号>`，Agent 直接调 `td use <id|序号>`；否则回退到上面的无参引导流。

命令模板的单一事实源是 `scripts/templates/topicdock-command.md`，模板正文 YAML frontmatter（文件顶部的元数据头）后第一行是固定哨兵 `<!-- topicdock:command-managed -->`。安装位置：

| 客户端 | 命令文件 |
|---|---|
| `claude` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/commands/topicdock.md` |

同名文件含该哨兵 → 属 TopicDock 管理（重装覆盖升级、卸载删除）；不含 → 外来文件，安装时提示跳过、**绝不静默覆盖**，卸载也不动。

`codex` 不安装命令模板，因为 Codex 已弃用 custom prompts（自定义 slash 命令），改由 Agent Skills 承载可复用提示。因此 `codex` 使用技能目录加 `<cwd>/AGENTS.md` 中的守则块。

## 安装指南

**环境要求**：Python ≥ 3.9，仅标准库，无需 `pip install` 任何东西。

**一键安装**（终端直接执行，或在 AI Agent 聊天里粘贴发送）：

```bash
curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | bash
```

- 想先审查脚本再执行：`curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | less`（纯文本，无压缩无混淆）。
- 在 AI Agent（Claude Code / Cursor / OpenCode / Codex）聊天里，把上面这条命令原样粘贴发送即可，Agent 会执行它完成安装。
- 可选参数：`... | bash -s -- --client cursor`（只装/升级指定客户端；不带参数时自动升级本机已装客户端，一个都没装过时会打印四客户端示例命令并以退出码 2 结束，请带 `--client` 重跑）。
- 安装会把常驻唤醒守则块注入**当前工作目录**下的 `<cwd>/AGENTS.md`（`claude` 为 `<cwd>/CLAUDE.md`）。请在项目根目录运行安装命令，这样守则才会落在你项目根的 `AGENTS.md` / `CLAUDE.md` 里。注入内容含你本机的引擎调用路径，幂等可重入；卸载时按其余已装客户端探测式保留或移除。

**从源码安装**，克隆仓库后运行：

```bash
python3 scripts/install.py --client claude|opencode|cursor|codex|all [--dest <dir>] [--uninstall]
```

`install.py` 会依次完成：

1. 把 `SKILL.md`、`references/` 和 `scripts/topicdock.py` 复制到对应客户端的技能目录。
2. 向 `<cwd>/CLAUDE.md`（`claude`）或 `<cwd>/AGENTS.md`（`opencode` / `cursor` / `codex`）幂等注入常驻唤醒守则块。
3. 把 `/topicdock` 命令模板安装到下表位置。`--dest <dir>` 表示装到默认客户端位置之外的目录，同时把命令模板根重定向到 `<dest>/<client>/commands/`，并跳过全局库的创建。
4. 在 `~/.local/bin/td` 建立指向已安装引擎的符号链接（该目录需在 `PATH` 中；不可写时会打印 `alias` 提示）。
5. 未指定 `--dest` 时，末尾自动建立/确认全局库 `~/.topicdock/`（幂等）。

`--uninstall` 反向清理：技能目录、命令模板（仅删含哨兵的文件）、守则块、符号链接；记忆文件里守则块以外的内容保留。卸载绝不删除你的主题数据库。

各客户端技能与命令模板落位（`--client` 取值同第一列）：

| 客户端 | 技能目录 | 命令模板 |
|---|---|---|
| `claude` | `~/.claude/skills/topicdock` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/skills/topicdock` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/skills/topicdock` | `~/.cursor/commands/topicdock.md` |
| `codex` | `<cwd>/.agents/skills/topicdock` | 无（Codex 已弃用 custom prompts） |

说明：

- `codex` 技能目录是唯一的项目级共享根 `<cwd>/.agents/skills/topicdock`；`<cwd>/AGENTS.md` 守则块由 `opencode` / `cursor` / `codex` 共注，卸载任一方时按其余客户端技能目录**探测式**保留或移除；`<cwd>/CLAUDE.md` 的块仅属 `claude`。
- `td` 符号链接运行的是 `PATH` 中找到的 `python3`，它必须 ≥ 3.9。如果默认的 `python3` 低于 3.9，或 `~/.local/bin` 不在 `PATH` 中，请用指定的 Python ≥ 3.9 解释器直接调用引擎，例如 `python3.11 <技能目录>/scripts/topicdock.py <子命令>`。
- TopicDock v1 面向 macOS / Linux（一键安装脚本是 bash）；Windows 用户请经 WSL（Windows 的 Linux 子系统）使用一键安装，或走上方「从源码安装」。

## 快速上手

最小闭环：建库 → 保存首个主题 → 挂载与追根。

```bash
# 1) 初始化存储库（global = ~/.topicdock/，local = <cwd>/.topicdock/），可重复执行
td init --scope global

# 2) 首次保存建档：base_hop 为 null 即新建主题，本例创建 C1 与 A1
td save --stdin --json <<'EOF'
{
  "topic_id": "auth-refactor",
  "title": "认证模块无状态 JWT 改造",
  "base_hop": null,
  "session_id": "ses-01",
  "actor": "opencode/architect",
  "summary": "敲定无状态 JWT 方案，开始实现刷新端点",
  "next_focus": "完成 Refresh Token 旋转逻辑",
  "mutations": [
    {"op": "create", "track": "C", "statement": "采用无状态 JWT + Refresh Token 旋转",
     "evidence_status": "inferred", "basis": "网关无共享会话存储"},
    {"op": "create", "track": "A", "state": "~", "statement": "实现 /refresh 端点",
     "basis": "路由已完成，待写旋转逻辑"}
  ]
}
EOF
# 返回 head_hop 为 H001 的 JSON，其 echo 字段即回显行：
# ⚓ TopicDock: auth-refactor (H001@<sha6> 新建 | +1[C] +1[A])

# 3) 之后在任意客户端、任意会话：挂载并追根
td use auth-refactor
td trace auth-refactor C1
```

库不存在时，除 `td init` 外的所有命令都会以退出码 3（`UNINITIALIZED_STORE`）结束。用一键安装脚本安装时全局库已自动建好，`td init` 本身也是幂等的。

之后在该主题上再次保存时，把 `base_hop` 设为 `H001`，`base_sha` 设为 `td use` 或 `td save` 返回的完整 `base_sha`。

### `⚓` 回显行

`td save` 与 `td merge` 返回的 `echo` 字段是锚点凭证行的权威来源，共有三种格式：

- **新建主题：** `⚓ TopicDock: <id> (<head_hop>@<sha6> 新建 | +1[C] +1[R] +1[A])`。其中 `+n[轨道]` 各段统计该轨道新建的条目数。
- **封板降级：** `⚓ TopicDock: <id> (<old> 已封板 → 自动新建 <head_hop>@<sha6> | +1[C] +1[A])`。上一个 head 跳已封板时，保存会自动新建下一跳并输出这一行。
- **常规 / amend / merge：** `⚓ TopicDock: <id> (<head_hop>@<sha6> | …)`。`--amend` 保存时带 ` amend` 段；没有新增条目时输出 `无新增条目`。

*注：prompt 与文档面已英文化（双语触发词保留）；引擎运行时 `status`/`echo`/`hint` 文案与 Markdown 协议字面量有意维持 v1 中文原样，不改。*

## 11 个子命令与退出码速查

| 命令 | 参数签名 | 说明 |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | 初始化存储库，幂等 |
| `td use` | `[<topic_id\|index>] [--trace <auto\|items>]` | 挂载主题，返回账本全文 + `(head_hop, base_sha)`；无参时按库内主题数引导；可选 `--trace auto` 同轮内联全部未决 `[Q]` 与断点 `[~][A]` 的完整历史块 |
| `td match` | `[--topic <kw> [--topic <kw2> …]] [--threshold 35] [--recent] [--trace <auto\|items>]` | 关键词打分召回（同分按 `updated_at` 倒序、`topic_id` 升序）；`--recent` 免 `--topic` 直接取最近 ≤ 5 个；前 5 命中（`full: true`）直接附挂载凭证 `head_hop`/完整 `base_sha`/`artifacts`，单次 match 即完成挂载，第 6 项起不附 |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | 唯一权威写入入口，带乐观并发校验；载荷可选 `model` 字符串（新建跳缺省不落键，`--amend` 缺省保留原值） |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id] [--actor] [--model] [--next-focus]` | 跨主题合流：src 条目并入 dst，trace 不断链（`--actor`/`--model` 默认 `""`） |
| `td trace` | `<topic_id> <item_id> [<item2> …]` | 拉取条目的全部历史证据块，已推翻/已折叠/已合流项同样可查；支持单次多条目（空格/逗号分隔如 `C1,C2,Q3`，单条目输出与旧版完全一致） |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | 分页列出主题，按 `updated_at` 倒序 |
| `td view` | `<topic_id\|index> [--history]` | 只读查看；`--history` 输出跳摘要链（含 `model` 时一并输出） |
| `td delete` | `<expr> [--dry-run] [--yes]` | 默认只预览；`--yes` 只接受显式 topic_id 列表 |
| `td undo` | `[--force]` | 回滚最近 3 步回滚栈中的栈顶一条 |
| `td doctor` | `[--rebuild]` | 七步自愈巡检；`--rebuild` 强制从 Markdown 冷重建图库 |

**全局参数**可放在子命令前或后：

- `--json`：输出缩进 JSON。
- `--store-dir <dir>`：显式指定库目录。
- `--workspace <dir>`：覆盖寻址起点（默认当前目录）。

**退出码 `0–7`：**

| 码 | status | 语义 |
|---|---|---|
| 0 | `OK` | 成功（含 undo 空栈提示、doctor 全 ok） |
| 1 | `ERROR` | 未预期异常，以 `{status, error}` 返回，不输出 traceback |
| 2 | —（无 JSON） | argparse 用法错误（缺必选参数、未知子命令等） |
| 3 | `UNINITIALIZED_STORE` | 向上冒泡与全局位置均未找到库 |
| 4 | `STALE_BASE` | 并发校验失步：另一窗口已推进该主题；绝不静默应用 |
| 5 | `TOPIC_NOT_FOUND` / `ITEM_NOT_FOUND` | 主题/条目不存在（含 selector 解析失败） |
| 6 | `UNSUPPORTED_SCHEMA_VERSION` | 数据的 `schema_version` 高于当前引擎支持的版本（任何写盘/删盘之前守卫） |
| 7 | `INVALID_INPUT` | 输入类错误：载荷非 JSON/校验失败、`--fork-as` 与 `--amend`/`--force-merge` 组合、delete 表达式非法、回滚受阻（`UNDO_BLOCKED`）等。 |

完整的 JSON 输入/输出契约和状态转移矩阵见 [`references/cli-contract.md`](references/cli-contract.md)。跨库 `export / import` 属 v2 规划，v1 不提供。

## 可靠性与防呆保障

- **`(base_hop, base_sha)` 双因子乐观并发校验（CAS，先比对再写入）**：多窗口并发推进或原地 `--amend` 时不会静默覆盖。基准失步时 `td save` 返回 `STALE_BASE`（退出码 4），载荷里带当前 head、变更条目和 `auto_absorbable`：
  - `auto_absorbable: true`：你的载荷与新增内容没有交集，把 `base_hop` 换成 `current_head`、`base_sha` 换成 `current_sha` 重发，即自动吸收。
  - `auto_absorbable: false`：存在冲突，由你二选一。`--fork-as <new_id>` 从你的基准状态分叉出新主题，没有覆盖风险；`--force-merge` 以当前 head 为基重放，最后写入胜，响应会列出被覆盖的条目。
- **七步启动自愈（`td doctor`）**：每条命令启动时，都会在跨平台文件锁下自动巡检。账本只许通过 `td` 修改，手改一律视为文件破坏：
  - 能用 `topicdock.db + hops/` 重放还原的（含标题与关联文件快照）自动覆写修复；
  - 无法修复或改了 `topic_id` 的损坏主题直接级联清理（连同依赖它的跳文件一并删除），不会拖累其他主题。
- **灾备与 100% 冷重建**：
  - `topicdock.db` 丢失或损坏时自动重建，也可手动 `td doctor --rebuild`。
  - `hop-NNN.md` 被误删时断链跳过，不会崩溃。
  - 外部关联文件被移动或缺失时，先按同名探测，找不到则追加告警，并在下次保存或巡检时摘除死链。
  - 删除主题时实时校验 DAG 的可达引用计数，只物理删除 `live_refs == 0` 的独占分片。
- **定长 3 步防呆回滚（`td undo`）**：保留最近 3 次 `save` / `amend` / `merge` / `delete` 的快照（`LIMIT 3`）。回滚前校验文件哈希，`--force` 只放宽磁盘哈希不一致这一层；同时恒定检查是否仍有存活子跳引用待移除的跳，有则以 `INVALID_INPUT`（退出码 7）拒绝并给出 `UNDO_BLOCKED` 详情。条目编号与跳编号的水位线只增不减，被撤回的编号不会复用。若上次回滚中断，残留的 `.topicdock-undo-pending.json` 会让下一次 undo 以 `INVALID_INPUT` 失败，需先手动删除该文件。一个已记录的极端情形：undo 之后若数据库又丢失并经冷重建，被撤回的编号可能被复用（见 [`references/markdown-spec.md`](references/markdown-spec.md) 的已知边界）。

## 常见问题（FAQ）

**我可以手动编辑这些 Markdown 文件吗？**
不可以。`topics/<id>.md` 与 `hops/hop-NNN.md` 只允许经 `td` 写入，任何手动修改都视为文件破坏。TopicDock 会尝试用 `topicdock.db` 与 `hops/` 重放还原该主题；做不到，或者你改了 `topic_id`，该主题会被清除。如果已经改过，请立即运行 `td doctor`。

**两个 Agent 窗口同时保存同一个主题会怎样？**
谁也不会覆盖谁。基准过期的那个窗口会收到 `STALE_BASE`（退出码 4）：如果它的改动与较新的改动没有重叠，换新基准重发即可吸收；有重叠则由你选择分叉成新主题（`--fork-as`）还是覆盖（`--force-merge`）。详见[可靠性与防呆保障](#可靠性与防呆保障)。

**卸载会删除我的主题吗？**
不会。卸载只移除技能目录、含哨兵的命令模板、注入的守则块和 `td` 符号链接，绝不删除你的主题数据库。

**Windows 上怎么用 TopicDock？**
TopicDock v1 面向 macOS / Linux。Windows 用户请在 WSL 中运行一键安装，或用 `python3 scripts/install.py`（Python ≥ 3.9）从源码安装。

**可以对 `.topicdock/` 做版本控制或备份吗？**
库由普通文件组成，任何备份或同步工具都能用。`topicdock.db` 是派生物，可用 `td doctor --rebuild` 从 Markdown 重建；`topics/` 与 `hops/` 才是事实来源，所以请备份整个库目录。关联文件（`artifacts` 条目）只是指向外部文档（如设计或计划文档）的路径引用，库里并不保存这些文档的正文副本，所以库备份里只有引用，没有这些文档本身。请记住规则：只有 `td` 能写入这个库，因此把两份副本里的文件混合合并，在 TopicDock 看来就是一次手改。跨库 `export / import` 属 v2 规划。

## 延伸文档

- [`SKILL.md`](SKILL.md)：常驻 3 行唤醒守则与主 Agent 完整工作流契约
- [`references/markdown-spec.md`](references/markdown-spec.md)：`topics/<id>.md` 与 `hops/hop-NNN.md` 的精确语法、转义规则与冷重建不变量
- [`references/cli-contract.md`](references/cli-contract.md)：11 个子命令参数、JSON 输入/输出 Schema、退出码（`0–7`）与 `STALE_BASE` 吸收协议
- [`README.md`](README.md)：本页的英文版
