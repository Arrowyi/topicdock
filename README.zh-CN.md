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
- **双层 Markdown 账本 + 派生本地索引**：
  - `topics/<topic-id>.md` 是当前态的单页全景，行尾自动附带演进短标，如 `(H001→H003)`。
  - `hops/hop-NNN.md` 保存每一跳（hop，一次保存产生的增量）的证据块与头部快照。
  - `topicdock.db` 是派生的本地 SQLite 索引，用于快速检索与回滚；丢了可以从 Markdown 文件 100% 冷重建。
- **向上冒泡就近寻址**：从当前工作目录逐级向父目录找 `.topicdock/`（或指针文件 `.agents/topicdock.json`），先找到哪一级就用哪一级；都没有就回退到全局库：若存在全局指针 `~/.agents/topicdock/config.json` 则用它指定的路径，否则用默认的 `~/.topicdock/`；仍然没有则返回 `UNINITIALIZED_STORE`（退出码 3），引导建库。
- **锚点凭证行（`⚓`）**：每次 `td save`（或 `td merge`）保存后，主 Agent 在回复末尾回显 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。同一会话未压缩时不用重复查库；长会话发生上下文压缩（Compaction，旧对话被裁剪）后，凭这一行重调 `td use <id>` 即可恢复完整上下文。
- **乐观并发校验（`STALE_BASE`）**：多个 Agent 窗口并发保存时，谁也不会静默覆盖谁。当一次保存的 `(base_hop, base_sha)` 已过期，`td save` 返回 `STALE_BASE`（退出码 4）：若你的改动与新内容没有重叠（`auto_absorbable: true`），换成 `current_head` 与 `current_sha` 重发即自动吸收；若存在冲突（`auto_absorbable: false`），由你选择分叉成新主题（`--fork-as <new_id>`），或在当前 head 之上重放（`--force-merge`）。
- **启动自愈与 3 步防呆回滚（`td doctor` / `td undo`）**：除 `td init` 外的每条命令启动时都会检查账本完整性，能由 `topicdock.db` 与 `hops/` 重放还原的损坏会自动修复（也可用 `td doctor --rebuild` 强制 100% 冷重建）；无法修复或被改动了 `topic_id` 的文件会被清除，健康主题不会被拖累。`td undo` 回滚 3 步回滚栈（`save`、`amend`、`merge`、`delete`）中最近的一条，前提是没有更新的存活跳依赖它；条目编号与跳编号不会复用被撤回的编号。

## 三支柱工作流（Read / Trace / Write）

Agent 遵循三条习惯，安装器会把它们作为简短的常驻守则注入。完整的工作流契约见 [`SKILL.md`](SKILL.md)。

- **Read（挂载主题）**：Agent 在会话开启首个话题、你点名某个主题或你切换主题时，通过 `td use`、`td match` 或 `/topicdock` 挂载主题。同一会话同一角色的后续轮次直接对话，0 次重复查库；上下文压缩后，凭上一条消息中的 `⚓` 行恢复主题。
- **Trace（追根溯源）**：回答关于已有 `[C]`、`[R]`、`[Q]` 或 `[A]` 条目的问题之前，Agent 会先运行 `td trace <topic_id> <item_id>`，从 `hops/hop-NNN.md` 拉取完整的历史证据，而不是凭一句话摘要臆测。
- **Write（在自然收敛点沉淀）**：只有顶层主 Agent 才运行 `td save`。它在 4 个自然收敛点（达成或推翻定论、浮现待决问题、行动完成或停在断点、话题切换或收尾）当轮回复前保存：同会话同 `actor` 用 `--amend`，跨会话或切换 `actor` 则新建一跳；回复末尾回显 `⚓` 行。切勿手改 Markdown 文件。

```bash
td match --topic jwt --topic 刷新令牌      # 按关键词检索候选主题
td use auth-refactor                        # 挂载：返回账本全文 + head_hop + 完整 base_sha
td trace auth-refactor C2                   # 追根：C2 的全部历史证据块与去向
td save --stdin --json --amend < payload.json   # 同会话增量：原地更新当前跳
```

**全局参数**可放在子命令前或后：

- `--json`：输出缩进 JSON。
- `--store-dir <dir>`：显式指定库目录。
- `--workspace <dir>`：覆盖寻址起点（默认当前目录）。

完整的 Agent 工作流契约见 [`SKILL.md`](SKILL.md)；全部 11 个子命令、JSON 载荷/输出 Schema 与退出码 `0–7` 见 [`references/cli-contract.md`](references/cli-contract.md)。

## /topicdock 单命令入口

安装后（见下文），在客户端直接输入 `/topicdock` 即可挂载/切换主题（单一命令，不用命令族）。`/topicdock` 会作为命令文件安装到 `claude`（`~/.claude/commands/topicdock.md`）和 `opencode`（`~/.config/opencode/commands/topicdock.md`）；在 `cursor`（2.4+）和 `codex` 上，由 skill 自身承载入口。

- **带参直通**：`/topicdock <id|序号>` → 直接调 `td use <id|序号> --trace auto` 挂载目标主题（同时载入所有未决 `[Q]` 与 `[~]` 断点的完整历史），返回账本正文后按三支柱工作流继续。
- **无参引导**：`/topicdock` → 调 `td use` 无参引导流，按库内主题数分三种情形：
  - **0 个主题**：引导你说明课题并新建建档。
  - **恰好 1 个**：直通挂载。
  - **≥ 2 个**：展示第 1 页列表，列表行在上，格式 `序号 | 标题 | id | 更新时间`，底部是自然语言操作提示，按列表实际状态收敛：
    - 回复序号挂载；
    - 回复 `删 <序号>` 先出删除预览，经你显式回复该主题的 topic_id 确认后才真正删除；
    - 有后续页时可回复「下一页」或「全部」；
    - 直接给关键词则按关键词过滤。

  列表只用于选择与删除。
- **参数传递**：命令后的附加文字会传给 Agent。若形如 `<id|序号>`，Agent 直接调 `td use <id|序号> --trace auto`；否则回退到上面的无参引导流。

重装会升级 TopicDock 管理的命令文件，卸载会将其删除；你自己创建的同名自定义文件不会被改动：安装时会提示并跳过，**绝不静默覆盖**。

## 安装指南

**环境要求**：Python ≥ 3.9，仅标准库，无需 `pip install` 任何东西。

**一键安装**（终端直接执行，或在 AI Agent 聊天里粘贴发送）：

```bash
curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | bash
```

- 想先审查脚本再执行：`curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | less`（纯文本，无压缩无混淆）。
- 在 AI Agent（Claude Code / Cursor / OpenCode / Codex）聊天里，把上面这条命令原样粘贴发送即可，Agent 会执行它完成安装。
- 可选参数：`... | bash -s -- --client cursor`（只装/升级指定客户端；不带参数时自动升级本机已装客户端，一个都没装过时会打印四客户端示例命令并以退出码 2 结束，请带 `--client` 重跑）。
- 安装会把常驻唤醒守则块注入**当前工作目录**下的 `<cwd>/AGENTS.md`（`claude` 为 `<cwd>/CLAUDE.md`）。请在项目根目录运行安装命令，这样守则才会落在你项目根的 `AGENTS.md` / `CLAUDE.md` 里。注入内容含你本机的引擎调用路径，重复运行只会原地更新守则块（幂等），可以放心重跑；卸载时按其余已装客户端探测式保留或移除。

**从源码安装**，克隆仓库后运行：

```bash
python3 scripts/install.py --client claude|opencode|cursor|codex|all [--dest <dir>] [--uninstall]
```

`install.py` 会依次完成：

1. 把 `SKILL.md`、`references/` 和 `scripts/topicdock.py` 复制到对应客户端的技能目录。
2. 向 `<cwd>/CLAUDE.md`（`claude`）或 `<cwd>/AGENTS.md`（`opencode` / `cursor` / `codex`）幂等注入常驻唤醒守则块。
3. 为 `claude` 与 `opencode` 安装 `/topicdock` 命令文件（见下表）。`--dest <dir>` 表示装到默认客户端位置之外的目录，同时把命令文件根重定向到 `<dest>/<client>/commands/`，并跳过全局库的创建。
4. 在 `~/.local/bin/td` 建立指向已安装引擎的符号链接（该目录需在 `PATH` 中；不可写时会打印 `alias` 提示）。
5. 未指定 `--dest` 时，末尾自动建立/确认全局库 `~/.topicdock/`（幂等）。

`--uninstall` 反向清理：技能目录、TopicDock 管理的命令文件、守则块、符号链接；记忆文件里守则块以外的内容保留。卸载绝不删除你的主题数据库。

各客户端技能与命令文件落位（`--client` 取值同第一列）：

| 客户端 | 技能目录 | 命令文件 |
|---|---|---|
| `claude` | `~/.claude/skills/topicdock` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/skills/topicdock` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/skills/topicdock` | 无（由 skill 承载入口） |
| `codex` | `<cwd>/.agents/skills/topicdock` | 无（由 skill 承载入口） |

说明：

- 安装/卸载 `cursor`（或 `all`）时，会自动清理旧版 TopicDock 管理的 `~/.cursor/commands/topicdock.md`；你自己创建的同名文件不会被改动，`--dest` 模式下该清理仅作用 `<dest>/cursor/commands`。
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

*注：引擎运行时 `status`/`echo`/`hint` 文案与 Markdown 账本协议字面量使用固定中文标识。*

## 本地数据与隐私契约

所有主题账本（`topics/<topic-id>.md`、`hops/hop-NNN.md`）和派生的 SQLite 索引（`topicdock.db`）100% 存放在你的本地磁盘上：项目库在 `<cwd>/.topicdock/`，全局库在 `~/.topicdock/`，也可以是通过 `td init --path` 配置的自定义本地路径。`td` 引擎只使用 Python 标准库，不发起任何网络请求，也不上传任何遥测数据。

## 常见问题（FAQ）

**我可以手动编辑这些 Markdown 文件吗？**
不可以。`topics/<id>.md` 与 `hops/hop-NNN.md` 只允许经 `td` 写入，任何手动修改都视为文件破坏。TopicDock 会尝试用 `topicdock.db` 与 `hops/` 重放还原该主题；做不到，或者你改了 `topic_id`，该主题会被清除。如果已经改过，请立即运行 `td doctor`。

**两个 Agent 窗口同时保存同一个主题会怎样？**
谁也不会覆盖谁。基准过期的那个窗口会收到 `STALE_BASE`（退出码 4）：如果它的改动与较新的改动没有重叠，换新基准重发即可吸收；有重叠则由你选择分叉成新主题（`--fork-as`）还是覆盖（`--force-merge`）。详见[核心特性与概念](#核心特性与概念)。

**卸载会删除我的主题吗？**
不会。卸载只移除技能目录、TopicDock 管理的命令文件、注入的守则块和 `td` 符号链接，绝不删除你的主题数据库。

**Windows 上怎么用 TopicDock？**
TopicDock v1 面向 macOS / Linux。Windows 用户请在 WSL 中运行一键安装，或用 `python3 scripts/install.py`（Python ≥ 3.9）从源码安装。

**可以对 `.topicdock/` 做版本控制或备份吗？**
库由普通文件组成，任何备份或同步工具都能用。`topicdock.db` 是派生物，可用 `td doctor --rebuild` 从 Markdown 重建；`topics/` 与 `hops/` 才是事实来源，所以请备份整个库目录。关联文件（`artifacts` 条目）只是指向外部文档（如设计或计划文档）的路径引用，库里并不保存这些文档的正文副本，所以库备份里只有引用，没有这些文档本身。各存储库彼此独立，且只有 td 能写入存储库，因此请勿手动把两份库副本里的文件混合合并。

## 延伸文档

- [`SKILL.md`](SKILL.md)：常驻 3 行唤醒守则与主 Agent 完整工作流契约
- [`references/markdown-spec.md`](references/markdown-spec.md)：`topics/<id>.md` 与 `hops/hop-NNN.md` 的精确语法、转义规则与冷重建不变量
- [`references/cli-contract.md`](references/cli-contract.md)：11 个子命令参数、JSON 输入/输出 Schema、退出码（`0–7`）与 `STALE_BASE` 吸收协议
- [`README.md`](README.md)：本页的英文版
