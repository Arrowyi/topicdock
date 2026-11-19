# TopicDock (`td`)

*Your topics live in one dock — plug in Claude Code, Cursor, Codex, or OpenCode anytime to continue.*

**TopicDock 是一个零依赖的个人主题记忆底座：你的讨论、结论和进度按「主题」存成 Markdown 账本，换任何 AI Agent 客户端都能接着做。**

在 Claude Code 里聊了一半的方案，换到 Cursor 就得从头解释；在 Codex 里拍板的结论，OpenCode 并不知道。原因是进度被绑在各客户端自己的「会话（Session）」里，而会话换个窗口、换个客户端就断了。

TopicDock 把记忆的单位从「会话」换成你自己拥有的「主题（Topic）」。主题以普通 Markdown 文件保存，由你的 Agent 通过 `td` 命令读写，不属于任何客户端。Claude Code / Cursor / Codex / OpenCode 都能随时挂载同一个主题，读到完整的结论、被排除的方案、待决问题和当前断点，然后接着干。引擎只用 Python ≥ 3.9 标准库，没有第三方依赖；唯一的可执行文件是单文件 `scripts/topicdock.py`（别名 `td`）。

## 核心概念

- **正交双轨账本（`[C/R/Q]` + `[A]`）**：认知与定论轨记录已拍板结论 `[C]`、已排除或被推翻的方案 `[R]`、待决问题 `[Q]`；行动与进展轨记录会话级阶段行动 `[A]`（`[ ]` 待办 / `[~]` 进行中断点 / `[x]` 已完成）。没有任何生命周期状态机，主题只要没删除，就永远平权、随时可续、随时可推翻。
- **双层 Markdown 账本 + 派生图库**：`topics/<topic-id>.md` 是当前态的单页全景（行尾自动附带 ≤ 20 字符的演进短标，如 `(H001→H003)`）；`hops/hop-NNN.md` 保存每一跳（hop，一次保存产生的增量）的证据块与快照；`topicdock.db`（SQLite WAL）只是派生索引，提供毫秒级检索、DAG 引用计数和回滚栈，丢了可以从 Markdown 100% 冷重建。
- **向上冒泡就近寻址**：从当前工作目录逐级向父目录找 `.topicdock/`（或 `.agents/topicdock.json`），先找到哪一级就用哪一级；都没有就回退到全局默认库 `~/.topicdock/`；仍然没有则返回 `UNINITIALIZED_STORE`，引导建库。
- **锚点凭证行**：每次写入后，主 Agent 在回复末尾回显 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。同一会话未压缩时不用重复查库；长会话发生上下文压缩（Compaction，旧对话被裁剪）后，凭这一行重调 `td use <id>` 即可恢复完整上下文。

## 三支柱工作流（Read / Trace / Write）

`SKILL.md` 里的 3 行常驻守则（`install.py` 会把它们注入客户端的 `CLAUDE.md` / `AGENTS.md`；`/topicdock` 命令模板同样指向这套三支柱工作流）：

- **TopicDock 调取（Read）**：仅在会话开启首个实质话题、用户点名（`继续 <id|序号>` / `/topicdock`）或中途切入**未挂载的新话题**时，加载 `topicdock` Skill 调用 `td use` 或拆解关键词调用 `td match`（`td match` 含 `--recent` 定位到目标 `topic_id` 后，必须同轮紧接着调一次 `td use <topic_id>` 获取完整 `base_sha` 完成正式挂载）；已挂载主题在同会话同角色后续轮次直接对话，**0 次重复查库与读取**（若发生上下文压缩 Compaction 丢失正文，或同会话切换了 Agent 角色，凭上一条消息末尾的 `⚓ TopicDock: <id> (<head_hop>@<sha6>)` 重调一次 `td use <id>` 刷新完整凭证）。
- **TopicDock 追根（Trace）**：凡 ① 用户质疑、推翻或深挖某条已挂载结论 `[C]`、排除项 `[R]`、待决 `[Q]` 或行动 `[A]`（含已折叠/已推翻/已合流项）；② 跨会话/跨角色接手未决 `[Q]` 或断点 `[~][A]` 需读取前序跳的多行 `推导与权衡` 与 `核心证据`；③ 基于既有 `[C]/[R]` 继续深化设计或编码而顶层单行摘要不足以确认边界细节时，必须先调用 `td trace <topic_id> <item_id>` 全量拉取底层 `hop-NNN.md` 历次证据块，严禁仅凭顶层一句话摘要臆测。
- **TopicDock 沉淀（Write）**：仅由**顶层主 Agent**（严禁 Subagent 调用，且禁止用户手改 Markdown 文件）在触达 4 类自然收敛节点——① 达成/推翻 `[C]/[R]` 定论；② 浮现需用户确认或需下一会话/下一阶段/另一角色拍板的未决问题 `[Q]`；③ `[A]` 阶段完成 `[x]` 或停在自然交接断点 `[~]`；④ 话题切换或收尾——时**当轮回复前立即调用 `td save`**（严禁先口头询问“是否交接/保存”再等下一轮；同会话同 `actor` 后续增量默认传 `--amend` 原地更新当前跳，跨会话或切换 `actor` 不传 `--amend` 自动新建下一跳）；并在回复末尾回显一行 `⚓ TopicDock: <topic-id> (<head_hop>@<base_sha前6位>)`。

```bash
td match --topic jwt --topic 刷新令牌      # 按关键词检索候选主题（0~100 打分，默认阈值 35）
td use auth-refactor                        # 挂载：返回账本全文 + head_hop + 完整 base_sha
td trace auth-refactor C2                   # 追根：C2 的全部历史证据块与去向
td save --stdin --json --amend < payload.json   # 同会话增量：原地更新当前跳
```

## `/topicdock` 单命令入口

安装后（见下文），在客户端直接输入 `/topicdock` 即可挂载/切换主题（单一命令，不用命令族）：

- **带参直通**：`/topicdock <id|序号>` → 直接调 `td use <id|序号>` 挂载目标主题，返回账本正文后按三支柱工作流继续。
- **无参引导**：`/topicdock` → 调 `td use` 无参引导流：库内 0 个主题时引导你说明课题并新建建档；恰好 1 个时直通挂载；≥ 2 个时展示第 1 页列表——列表行在上，格式 `序号 | 标题 | id | 更新时间`，底部是自然语言操作提示（回复序号挂载 / 回复 `删 <序号>` 先出删除预览、经你显式回复该主题的 topic_id 确认后才真正删除 / 有后续页时可回复「下一页」或「全部」/ 直接给关键词按关键词过滤），提示按列表实际状态收敛。列表只用于选择与删除。
- 命令模板以 `$ARGUMENTS` 传参：claude / opencode 会把命令后的参数替换进模板；cursor 追加原文，模板以自然语言判断参数，三端均可降级执行。

命令模板安装位置（单一事实源 `scripts/templates/topicdock-command.md`，模板正文 frontmatter 后第一行是固定哨兵 `<!-- topicdock:command-managed -->`）：

| 客户端 | 命令文件 |
|---|---|
| claude | `~/.claude/commands/topicdock.md` |
| opencode | `~/.config/opencode/commands/topicdock.md` |
| cursor | `~/.cursor/commands/topicdock.md` |

codex 不安装命令模板：官方 custom prompts（自定义 slash 命令）已废弃（"Deprecated. Use skills for reusable prompts"），可复用提示改由 Agent Skills 承载，故 codex 只需技能目录 + AGENTS.md 守则块。

同名文件含该哨兵 → 属 TopicDock 管理（重装覆盖升级、卸载删除）；不含 → 外来文件，安装时提示跳过、**绝不静默覆盖**，卸载也不动。

## 快速上手

**环境要求**：Python ≥ 3.9，仅标准库，无需 `pip install` 任何东西。

**安装**（终端直接执行，或在 AI Agent 聊天里粘贴发送）：

```bash
curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | bash
```

- 想先审查脚本再执行：`curl -fsSL <同上 URL> | less`（纯文本，无压缩无混淆）。
- 在 AI Agent（Claude Code / Cursor / OpenCode / Codex）聊天里，把上面这条命令原样粘贴发送即可，Agent 会执行它完成安装。
- 可选参数：`... | bash -s -- --client cursor`（只装/升级指定客户端；不带参数时自动升级本机已装客户端，一个都没装过时会打印四客户端示例命令并以退出码 2 结束，请带 `--client` 重跑）。
- 安装会按你的本机环境，自动把常驻唤醒守则块注入**当前项目根**的 `AGENTS.md`（claude 为 `CLAUDE.md`）——注入内容含你本机的引擎调用路径，幂等可重入；卸载时按其余已装客户端探测式保留或移除。

**从源码安装**（克隆仓库后运行；`scripts/topicdock.py` 是构建产物，缺失时 `install.py` 会先自动调用 `scripts/build.py` 生成）：

```bash
python3 scripts/install.py --client claude|opencode|cursor|codex|all [--dest <dir>] [--uninstall]
```

它会把 `SKILL.md`、`references/` 和 `scripts/topicdock.py` 复制到对应客户端的技能目录，向 `<cwd>/CLAUDE.md`（claude）或 `<cwd>/AGENTS.md`（opencode / cursor / codex）幂等注入常驻唤醒守则块，并把 `/topicdock` 命令模板安装到上表位置（`--dest <dir>` 同时把命令模板根重定向到 `<dest>/<client>/commands/`），再在 `~/.local/bin/td` 建立指向已安装引擎的符号链接（该目录需在 `PATH` 中；不可写时会打印 `alias` 提示）。`--uninstall` 反向清理：技能目录、命令模板（仅删含哨兵的文件）、守则块、符号链接，记忆文件里守则块以外的内容保留。

`td` 链接依赖 `PATH` 中的 `python3` ≥ 3.9；不满足时请以完整命令直接调用引擎：`python3 <技能目录>/scripts/topicdock.py <子命令>`。旧版 cursor 曾把技能装到项目级 `<cwd>/.agents/skills/topicdock`、命令装到 `<cwd>/.cursor/commands/`；这些旧产物不迁移、不自动删除，不再使用 codex 的用户可手动删除该目录。

TopicDock 一期面向 macOS / Linux（bash 脚本）；Windows 用户请经 WSL 使用一键安装，或走上方「从源码安装」。

各客户端技能落位（`--client` 取值同名）：

| 客户端 | 技能目录 | 命令模板 |
|---|---|---|
| claude | `~/.claude/skills/topicdock` | `~/.claude/commands/topicdock.md` |
| opencode | `~/.config/opencode/skills/topicdock` | `~/.config/opencode/commands/topicdock.md` |
| cursor | `~/.cursor/skills/topicdock` | `~/.cursor/commands/topicdock.md` |
| codex | `<cwd>/.agents/skills/topicdock` | 无（官方已弃用 custom prompts） |

无 `--dest` 时，安装末尾会自动建立/确认全局库 `~/.topicdock/`（幂等）；卸载绝不删除数据库。codex 技能目录是唯一的项目级共享根 `<cwd>/.agents/skills/topicdock`；`<cwd>/AGENTS.md` 守则块由 opencode / cursor / codex 共注，卸载任一方时按其余客户端技能目录**探测式**保留或移除（`<cwd>/CLAUDE.md` 的块仅属 claude）。

**最小闭环：建库 → 保存 → 挂载**

```bash
# 1) 初始化存储库（global = ~/.topicdock/，local = <cwd>/.topicdock/）
td init --scope global

# 2) 首次保存建档：base_hop 为 null 即新建主题，本例创建 C1 与 A1
td save --stdin --json <<'EOF'
{
  "topic_id": "auth-refactor",
  "title": "认证模块无状态 JWT 改造",
  "base_hop": null,
  "session_id": "ses-01",
  "actor": "architect",
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

## 11 个子命令速查

| 命令 | 参数签名 | 说明 |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | 初始化存储库，幂等 |
| `td use` | `[<topic_id\|index>]` | 挂载主题，返回账本全文 + `(head_hop, base_sha)`；无参时按库内主题数引导 |
| `td match` | `[--topic <kw> [--topic <kw2> …]] [--threshold 35] [--recent]` | 关键词打分召回（同分按 `updated_at` 倒序、`topic_id` 升序）；`--recent` 免 `--topic` 直接取最近 ≤ 5 个 |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | 唯一权威写入入口，带乐观并发校验；载荷可选 `model`（缺省不落键） |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id] [--actor] [--model] [--next-focus]` | 跨主题合流：src 条目并入 dst，trace 不断链（`--actor`/`--model` 默认 `""`） |
| `td trace` | `<topic_id> <item_id>` | 拉取条目的全部历史证据块，已推翻/已折叠/已合流项同样可查 |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | 分页列出主题，按 `updated_at` 倒序 |
| `td view` | `<topic_id\|index> [--history]` | 只读查看；`--history` 输出跳摘要链（含 `model` 时一并输出） |
| `td delete` | `<expr> [--dry-run] [--yes]` | 默认只预览；`--yes` 只接受显式 topic_id 列表 |
| `td undo` | `[--force]` | 回滚最近 3 次写操作之一 |
| `td doctor` | `[--rebuild]` | 七步自愈巡检；`--rebuild` 强制从 Markdown 冷重建图库 |

全局参数 `--json`（输出缩进 JSON）、`--store-dir <dir>`（显式指定库目录）、`--workspace <dir>`（覆盖寻址起点）可放在子命令前或后。退出码 `0~7`：`0` OK、`1` 未预期错误、`2` 用法错误、`3` `UNINITIALIZED_STORE`、`4` `STALE_BASE`、`5` 主题/条目不存在、`6` `UNSUPPORTED_SCHEMA_VERSION`、`7` `INVALID_INPUT`。完整的 JSON 输入/输出契约和状态转移矩阵见 [`references/cli-contract.md`](references/cli-contract.md)。跨库 `export / import` 属二期规划，一期不提供。

## 可靠性与防呆保障

- **`(base_hop, base_sha)` 双因子乐观并发校验（CAS，先比对再写入）**：多窗口并发推进或原地 `--amend` 时不会静默覆盖。基准失步时 `td save` 返回 `STALE_BASE`（退出码 4）：与新增内容无冲突的可换新 base 重发自动吸收；有冲突的由用户二选一，`--fork-as` 分叉成新主题，或 `--force-merge` 以当前 head 为基覆盖（响应会列出被覆盖条目）。
- **七步启动自愈（`td doctor`）**：每条命令启动时，都会在跨平台文件锁下自动巡检。账本只许通过 `td` 修改，手改一律视为文件破坏：能用 `topicdock.db + hops/` 重放还原的（含标题与关联文件快照）自动覆写修复；无法修复或改了 `topic_id` 的损坏主题直接级联清理，不会拖累其他主题。
- **灾备与 100% 冷重建**：`topicdock.db` 丢失或损坏时自动重建（也可手动 `td doctor --rebuild`），结果与增量维护的结果一致。`hop-NNN.md` 被误删时断链跳过、不会崩溃；外部关联文件被移动或缺失时，先按同名探测，找不到则追加告警，并在下次保存或巡检时摘除死链。删除主题时实时校验 DAG 的可达引用计数，只物理删除 `live_refs == 0` 的独占分片。
- **定长 3 步防呆回滚（`td undo`）**：保留最近 3 次 `save / amend / merge / delete` 的快照。回滚前校验文件哈希，并确认没有新增的存活子跳引用，再原子还原。条目编号的水位线只增不减，被撤回的编号不会复用，不会串号。

## 更多文档与仓库结构

- [`SKILL.md`](SKILL.md)：常驻 3 行唤醒守则与主 Agent 完整工作流契约
- [`references/markdown-spec.md`](references/markdown-spec.md)：`topics/<id>.md` 与 `hops/hop-NNN.md` 的精确语法、转义规则与冷重建不变量
- [`references/cli-contract.md`](references/cli-contract.md)：11 个子命令参数、JSON 输入/输出 Schema、退出码（`0–7`）与 `STALE_BASE` 吸收协议
- [`AGENTS.md`](AGENTS.md)：仓库六层分片构建规范与改动→验证测试映射表

```text
TopicDock/
├── SKILL.md                    # Skill 入口：常驻守则 + 工作流契约
├── AGENTS.md                   # 开发规范与改动→验证映射表
├── references/
│   ├── markdown-spec.md        # 账本 Markdown 语法规范
│   └── cli-contract.md         # CLI 与 JSON 契约
├── scripts/
│   ├── topicdock.py            # 单文件引擎（构建产物，不手改）
│   ├── layers/                 # 源码分片 L1→L6，按文件名序拼接
│   ├── templates/
│   │   └── topicdock-command.md  # /topicdock 命令模板（单一事实源）
│   ├── build.py                # 分片 → topicdock.py 纯拼接
│   └── install.py              # 跨客户端 Skill + 命令模板安装/卸载与守则注入
└── tests/                      # 标准库 unittest
```

改代码请改 `scripts/layers/` 下的分片，再构建并跑全量测试（当前 272 个测试全绿）：

```bash
/opt/homebrew/bin/python3.13 scripts/build.py
/opt/homebrew/bin/python3.13 -m unittest discover -s tests -v   # 必须在仓库根目录运行
```

以上解释器路径是维护者本机的写法，其他环境换成任意 Python ≥ 3.9 即可。
