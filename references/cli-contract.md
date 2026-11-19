# TopicDock CLI 契约速查（11 子命令 + 载荷 Schema + 输出契约 + 退出码）

命令形态：`td <子命令> …`（`td` = 单文件引擎 `scripts/topicdock.py`；Python ≥ 3.9，仅标准库）。
全局参数 `--json` / `--store-dir <dir>` / `--workspace <dir>` 可置于子命令前或后：`--json` 输出缩进 JSON（人类可读模式输出键值行）；`--store-dir` 显式指定库目录；`--workspace` 覆盖寻址起点（默认 cwd，向上冒泡至全局 `~/.topicdock/`，见 `SKILL.md` bootstrap 节）。

语法权威 `references/markdown-spec.md`（本文件不重复双模板语法）。

---

## 1. 11 个子命令总表

| 子命令 | 参数签名 | 核心行为与自愈保障 |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | 初始化 `<store_dir>`（`.gitignore` + `.topicdock-meta.json`，DB 惰性创建）；有 `--path` 时额外写指针配置（global → `~/.agents/topicdock/config.json`，local → `<cwd>/.agents/topicdock.json`，内容 `{"store_dir":"<abs>"}`）；幂等成功返回现状 |
| `td use` | `[<topic_id\|index>]` | 显式 selector（纯数字按 list 同序 1-based 序号，否则 topic_id）→ 返回 md 全文 + `(head_hop, base_sha 完整 64 hex)` + 关联文件三态探测；无参：0 主题引导 / 1 主题直通挂载 / ≥2 首页列表 + hint |
| `td match` | `[--topic <kw> [--topic <kw2> …]] [--threshold 35] [--recent]` | 主 LLM 拆解关键词后调用；0~100 确定性打分，≥τ 全部召回（同分按 `updated_at` 倒序、`topic_id` 升序）；前 ≤5 附全文其余摘要；`--recent` 免 `--topic` 绕过打分直取最近 ≤5（排序同 `list/use`） |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | 唯一权威写入入口：`(base_hop, base_sha)` 双因子 CAS + 排他锁 + 四步落盘（hop 分片 → md 提交点 → DB 事务 + undo 压栈）；校验可选 `model` 类型；`--amend` 同 `session_id` 且 `live_refs(head_hop) == 1` 时原地合并；但双侧归一化 `actor` 均非空且剥离 `@model` 后主体不同时自动降级新建下一跳（输出 `degraded` 标注）；原地合并时空 head `actor` 遇非空载荷 `actor` 时回填该跳 `actor` |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id] [--actor] [--model] [--next-focus]` | src 有效条目按 dst `next_seq` 续编并入（`op=inherit, origin=src:item@hop`，trace 不断链）；生成 `[H_dst, H_src]` 双父合流跳（`--actor` 与 `--model` 默认 `""`，不继承双亲 `model`）；删 `src.md` 顶层文件（底层分片因被合流跳引用而保留）；undo 压栈 |
| `td trace` | `<topic_id> <item_id>` | `item_hops` 正序 + `item_links` 跨主题递归上溯（环保护）+ 推翻去向块 + 后续去向尾注；已推翻/已解决/已折叠/已合流条目仍 100% 可查；缺失跳标注 `missing` |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | `updated_at` 倒序每页 10 条，跨页全局连续编号（第 2 页 11~20）；`--filter` topic_id/title 大小写不敏感子串 |
| `td view` | `<topic_id\|index> [--history]` | 纯只读；`--history` 从 hops + hop_edges 逆拓扑（head 最前）输出跳摘要链（跳头含非空 `str` 型 `model` 时附 `model` 字段）；断链输出可达部分 + `missing`/链中断标注 |
| `td delete` | `<expr> [--dry-run] [--yes]` | 预览（默认 / `--dry-run`）：序号表达式 `2,4~6`、`keep 1,3` 解析为 topic_id 清单 + 独占跳数 + 缺失跳数；执行 `--yes` **仅接受显式 topic_id 列表**（免疫并发重排）；仅物理删除 `live_refs == 0` 的独占分片 |
| `td undo` | `[--force]` | 弹出 `undo_stack` 栈顶（LIMIT 3）：post_hashes 校验（`--force` 仅放宽此层）+ 「无新增存活子跳引用」恒强制 → journal 原子写 → 幂等绝对还原（恢复 pre_files / 删新建 / per-topic 高水位 bump / rebuild 全库重算）→ 删 journal |
| `td doctor` | `[--rebuild]` | 无参：七步自愈巡检 + 修复报告；`--rebuild`：强制从合法存活 Markdown 100% 冷重建 SQLite 图库与行尾短标 |

所有命令启动均在排他锁下自动执行七步自愈（能修则修、不能修或 ID 被改则级联删除，绝不阻塞好主题）。

---

## 2. `td save` 载荷 Schema（`--stdin` / `--json-file`，design §4.3(1) 权威）

```json
{
  "topic_id": "auth-refactor",
  "title": "认证模块无状态 JWT 改造与灰度迁移",
  "base_hop": "H003",
  "base_sha": "a1b2c3d4e5f6",
  "session_id": "ses-01",
  "actor": "opencode/architect",
  "model": "opencode-go/gemini-4-argon",
  "summary": "敲定 Refresh Token 旋转机制 [C3]，推进设计记录至 [A2] 断点",
  "next_focus": "拍板 [Q1] Webhook 鉴权边界并完成 design.md 落盘",
  "parent_hops": ["H003"],
  "mutations": [
    {"op": "create", "track": "C", "statement": "...", "evidence_status": "inferred", "basis": "...", "rationale": "...", "evidence_detail": "...", "review_cond": "..."},
    {"op": "update", "item_id": "A2", "state": "~", "statement": "... — 断点: ...", "rationale": "..."},
    {"op": "overturn", "from_item": "C1", "statement": "...", "basis": "...", "rationale": "...", "evidence_detail": "...", "review_cond": "..."},
    {"op": "resolve", "from_item": "Q1", "into_track": "C", "statement": "...", "evidence_status": "verified", "basis": "..."},
    {"op": "fold", "from_items": ["A1", "A2", "A3"], "statement": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}
  ],
  "artifacts": [
    {"name": "网关架构设计记录", "path": "/abs/path/to/design.md", "note": "定义 JWT 载荷字段与公钥轮换接口"}
  ]
}
```

- `topic_id`：`^[a-z0-9][a-z0-9-]{0,63}$`，创建后不可变；`title`：任意语言，可随时改（空/缺省 = 保持/继承）。
- `actor` 归一化：`null`/空白串 → `""`（不落盘为 `"None"`）；`model`（可选）：`null`/缺省/`strip()` 后空白 → 缺省不写键，非 `None` 且非 `str` → `INVALID_INPUT`（退出码 7），落盘为 `strip()` 后值；新建跳不继承父跳 `model`，`--amend` 非空串更新、缺省/空白保留原值。
- `base_hop: null` = 新建主题（场景 1）；`base_sha` 规则（D-P26）：完整 64-hex 相等，或 ≥12 位且为当前 `topics.md_sha256` 前缀。
- **`--fork-as <new_id>`**：载荷 `topic_id` = 源主题 id，`--fork-as` = 新 id（正则合法且不存在）；源 = base_hop 处的状态（祖先分叉免 sha 校验）；新首跳 `parents=[base_hop]`、全部条目 `op=inherit, origin=src:item@<初始创建跳>` 拷入 + 载荷 mutations 同跳叠加；与 `--amend`/`--force-merge` 组合一律 `INVALID_INPUT`。
- `artifacts` 流转：省略/`null` → 继承 base_hop 快照；`[]` → 清空；新列表 → 更新并剔除死链（D-P33）。
- `fold` 前置校验：`from_items` 全部存在且 `[x]`，否则 `INVALID_INPUT`。
- `retract` 形态：`{"op": "retract", "item_id": "<id>"}`（移除误建条目，不转入 `[R]`）。

### ops 状态转移矩阵（7 op × md 变更 × 分片变更 × amend 合并规则）

| 操作 `op` | 适用轨道 | 对顶层 `topics/<id>.md` 的变更 | 对 `hops/hop-NNN.md` 与 `item_hops / item_links` 的变更 | 同跳 `--amend` 合并规则 |
|---|---|---|---|---|
| `create` | `C / R / Q / A` | 分配新编号 `next_seq[T]++` 并追加至对应分节 | 写入 `### [<new_id>] (op=create, ...)`，`item_hops` 记 `op='create'` | 保持 `op=create` |
| `update` | `C / R / Q / A` | 原地更新指定 `item_id` 的正文、依据或行动状态 `[x/~]` | 写入 `### [<id>] (op=update, ...)`，`item_hops` 记 `op='update'` | 若该条目系本跳刚 `create`，合并后仍保持 `op=create`（更新正文）；否则保持 `op=update` |
| `overturn` | `C -> R` | 从 `[C]` 移除 `from_item`，分配新 `R<n>` 追加至 `[R]` 并标 `(原立H_orig→推翻H_cur)` | 写入 `### [R<n>] (op=overturn, from=C<k>, origin=...)`，记录 `item_links` | 若被推翻的 `C<k>` 系本跳刚建，直接替换为 `R<n> (op=create)` |
| `resolve` | `Q -> C / R / None` | 从 `[Q]` 移除 `from_item`；若 `into_track='C'` 或 `'R'` 则同时新建对应条目 | 写入 `### [Q<k>] (op=resolve)`（及新建的 `### [C<n>]`），记录 `item_links` | 若 `Q<k>` 系本跳刚建且当跳即解决，直接收敛为结果条目 |
| `fold` | `A -> A` | 从 `[A]` 移除指定的多个已完成 `- [x] A_i`，分配新 `- [x] A_new` 汇总行替换之 | 写入 `### [A_new] (op=fold, from=A1+A2+A3)`，记录 `item_links` | 合并 `from` 集合 |
| `inherit` | 分叉/合流时的 `C/R/Q/A` | 分叉或 `td merge` 时将源主题条目按目标 `next_seq` 拷入目标顶层文件 | 写入 `### [<id>] (op=inherit, origin=<src_topic>:<src_id>@<hop>)`，串联跨主题 `trace` | 保持 `op=inherit` |
| `retract` | `C / R / Q / A` | 移除误建或废弃的无意义条目（不转入 `[R]`） | 写入 `### [<id>] (op=retract)` | 若系本跳刚 `create`，直接从本跳分片中抵消删除 |

载荷 mutations 先规范化为 HopBlock 序列再逐块过唯一重放内核 `apply_hop_block`（正反两路同一函数）；载荷与主题状态不相容（如 `[retract X, update X]` 顺序非法、fold 自引用）→ `INVALID_INPUT` **零落盘**。

---

## 3. 输出 JSON 契约（`--json`；`status` 与退出码一一对应）

| 命令 | 成功载荷字段 |
|---|---|
| `init` | `status/store_dir/scope/created(bool)` |
| `save` | `status/topic_id/head_hop/base_sha(完整 64 hex)/amend/degraded/created{track:n}/echo(⚓行)/conflicts[]/overwritten[]`；fork 成功时 `topic_id` = 新 id |
| `use` | `status/topic_id/text(md 全文)/head_hop/base_sha(完整)/artifacts[{name,path,note,status: ok\|missing\|moved,new_path?}]`；无 selector 0 主题 → `{status, topic_id: null, note}`；≥2 → list 首页字段 + `hint` |
| `list` | `status/page/page_size/total/items[{index,topic_id,title,updated_at}]` |
| `match` | `status/results[{topic_id,score,title,full(bool),text?(≤5 附全文)}]` |
| `trace` | `status/topic_id/item_id/blocks[{hop_id,op,text?,missing?,from_topic?}]/links[]/successor[]/successor_notes[]` |
| `view` | `status/topic_id/text` 或（`--history`）`history[{hop_id,created_at,actor,summary,model?,missing?,note?}]`（`model` 仅跳头含非空 `str` 时输出） |
| `delete` | 预览：`status/topics[{topic_id,title}]/exclusive_hops/missing_hops`；执行：`status/deleted[]/exclusive_hops/missing_hops` |
| `merge` | `status/topic_id/dst_topic_id/head_hop/base_sha/merged_items{track:n}/merged_from/echo` |
| `undo` | 空栈：`{status,note:"回滚栈为空…"}`（退出 0）；成功：`status/restored[]/op_type/summary` |
| `doctor` | `status/checks[{name,status: ok\|repaired\|deleted,detail?}]`（结构见 §7） |

⚓ 回显行（`save`/`merge` 的 `echo` 字段）三种格式：

- 新建主题：`⚓ TopicDock: <id> (<head_hop>@<sha6> 新建 | +1[C] +1[R] +1[A])`
- 封板降级：`⚓ TopicDock: <id> (<old> 已封板 → 自动新建 <head_hop>@<sha6> | +1[C] +1[A])`
- 常规 / amend / merge：`⚓ TopicDock: <id> (<head_hop>@<sha6> | …)`（amend 带 ` amend` 段；无新增条目计 `无新增条目`）

sha 三源口径（D-P26）：CAS `base_sha` = `topics.md_sha256`；md `## 4.` 行 sha = `hops.md_sha256` 前 12 位；⚓ = `base_sha` 前 6 位；`use/save` JSON 恒返回完整 base_sha。

---

## 4. 退出码表（0–7 全量）

| 码 | status | 语义 |
|---|---|---|
| 0 | `OK` | 成功（含 undo 空栈提示、doctor 全 ok） |
| 1 | `ERROR` | 未预期异常兜底（`{status, error:"<类型>: <信息>"}`，不 traceback） |
| 2 | —（无 JSON） | argparse 用法错误（缺必选参数/未知子命令等） |
| 3 | `UNINITIALIZED_STORE` | 冒泡与全局均未找到库：`{status, default_global:"~/.topicdock", workspace_local:"<cwd>/.topicdock"}` → bootstrap 引导 |
| 4 | `STALE_BASE` | CAS 失步（载荷见 §5），绝不静默应用 |
| 5 | `TOPIC_NOT_FOUND` / `ITEM_NOT_FOUND` | 主题/条目不存在（含 use/view/trace/merge/delete 的 selector 解析失败、`--yes` 引用不存在 id） |
| 6 | `UNSUPPORTED_SCHEMA_VERSION` | `schema_version > 1` 或 hop 头 `v > 1`（任何写盘/删盘之前守卫） |
| 7 | `INVALID_INPUT` | 输入类错误：载荷非 JSON/校验失败、`base_hop` 不在首父血缘链上、载荷引用已消失 item_id（附 `vanished_items`）、`--fork-as` 与 `--amend/--force-merge` 组合、fold 前置不满足、delete 表达式非法/越界、`UNDO_BLOCKED`（见 §6）、undo journal 残留入口拒绝 |

---

## 5. `STALE_BASE` 载荷与 5C 吸收流程（D-P24）

失配载荷字段：

```json
{
  "status": "STALE_BASE",
  "topic_id": "auth-refactor",
  "current_head": "H006",
  "current_sha": "<完整 64 hex>",
  "hops_after_base": ["H005", "H006"],
  "changed_items": ["C2", "A1"],
  "conflicts": ["C2"],
  "auto_absorbable": false
}
```

- `hops_after_base`：base 之后的新跳；`changed_items`：这些跳变更过的条目；**冲突判据** = 载荷 mutations 触及的 item_id（update/overturn/resolve/fold/retract 目标）与 `changed_items` 的交集（`create` 不冲突），外加头部字段冲突（载荷带 `title`/`artifacts` 且 base 后 head 头同名字段变过 → `conflicts` 含 `"title"`/`"artifacts"`）。
- `auto_absorbable = (conflicts 为空)`。
- **吸收流程**：`auto_absorbable: true` → 换新 base 重发即吸收（必要时先 `td use` 刷新正文；`base_hop: current_head`、`base_sha: current_sha`）。
- `auto_absorbable: false` → 提示用户二选一：`--fork-as <new_id>` 分叉（无覆盖风险；fork 遇同 head sha 失配仍可吸收，不改源主题）或 `--force-merge` 覆盖（以当前 head 为基重放，最后写入胜；成功响应附 `overwritten[]`，元素固定 `{item_id, by_hop}`；载荷引用已消失 item_id → `INVALID_INPUT` + `vanished_items`，退出 7）。
- 特例：同 head 但 sha 失配（head 被另一窗口 amend）→ `{status:"STALE_BASE", reason:"BASE_SHA_MISMATCH_HEAD", changed_items:[], conflicts:[], auto_absorbable:false}`；`--force-merge` 同样放行。
- 前置条件：`base_hop` 必须在当前 head 的首父血缘链上，否则 `INVALID_INPUT`（退出 7）。

---

## 6. D-P20 / D-P35 窄窗口 roll-forward 与 `UNDO_BLOCKED`

### 6.1 引用中 hop 的 sha 失配分类（D-P20 收紧 + D-P35 加固）

- **缺失** = 断链跳过不级联：`hops.md_sha256` 置 `MISSING`（不动 `topics.md_sha256`，保 CAS 稳定）；trace/view/delete 标注缺失跳号；仅当该主题 md 亦坏且重放需要该跳时才级联。
- **roll-forward 唯一窗口**：sha 失配仅当**同时满足**「该 hop 是某存活主题的 head」+ `live_refs == 1` + （md `## 4.` sha 前缀 == 新文件哈希，**或** md 与 DB 同处旧态 = amend 2→3 / 3→4 崩溃窗口）+ 文件哈希与 `.topicdock-amend-pending.json` 记录（`{"hop_id", "expected_sha"}`，amend 原地合并前原子写、DB 提交后删除）匹配 → **以文件为准 roll-forward**（经重放内核重算该主题全部派生行与 md）。
- **其余一切 sha 失配**（封板祖先、非 head、`live_refs > 1`、含语法合法的人为篡改）与无 pending 记录或失配、不可解析、头失配 → 一律视为**损坏** → 依赖主题按「能修则修、否则级联删除」处置，损坏 hop 文件随级联物理清理。
- **merge ②③ 窄窗口 roll-forward**：merge 在「dst md 已写、DB 未提交」间崩溃 → 下次启动 ③ 崩溃纳管发现 dst head 是合流跳且 parents 含存活 src 的 head → 前滚补完合流（补删 src 行 + src.md）；此窗口 undo 无条目为已接受损失。
- pending 记录删除一律延后到事务 commit 之后；无归属/已持久化的残留 pending 由自愈 sweep 清理（`pending_sweep` 报告项）。

### 6.2 `UNDO_BLOCKED`

不设独立 status：一律 `{"status":"INVALID_INPUT", ...}` + 退出码 7，两种触发：

1. **子跳引用检查（恒强制，`--force` 不放宽）**：待移除/还原的跳存在后续存活子跳引用 → `{status:"INVALID_INPUT", detail:"UNDO_BLOCKED：跳存在后续存活子跳引用", blocked_hop, child_hop}`。
2. **post_hashes 校验（`--force` 可放宽）**：磁盘文件哈希与快照不一致 → `{status:"INVALID_INPUT", detail:"UNDO_BLOCKED：磁盘文件哈希与快照不一致（--force 可放宽）", path, expected, actual}`。

另有 undo 入口拒绝：`.topicdock-undo-pending.json` journal 残留（上次回滚中断）→ `INVALID_INPUT` 退出 7，先手动删除该文件（heal 未接线，见 `references/markdown-spec.md` 已知边界）。

---

## 7. `td doctor` 报告结构

无参 → 七步自愈巡检（顺序 ⑦ DB 缺失/损坏冷重建 → ⑥ meta 三源 max 修复 → ③ 崩溃纳管 → ① 野生文件清理 → ② 顶层 md 修复 → ⑤ 引用中 hop 分类处置 → ④ 孤儿 hop 清理），返回：

```json
{"status": "OK", "checks": [{"name": "meta_json", "status": "ok"}, {"name": "md_repair", "status": "repaired", "detail": "auth-refactor（重放还原）"}]}
```

- `status ∈ {ok, repaired, deleted}`；健康库通常仅 `meta_json: ok`（已知边界见 markdown-spec §5）。
- 已定义的 `name`：`rebuild_db` / `meta_json` / `adopt_crashed` / `tmp_sweep` / `wild_cleanup` / `md_repair` / `hop_missing` / `amend_rollforward` / `pending_sweep` / `orphan_cleanup` / `cascade_delete`。
- `--rebuild` → 强制冷重建（`topicdock.db.tmp` 全表重放 → `os.replace` 原子替换 + 删旧 `-wal/-shm`），返回 `{status, checks:[{name:"rebuild_db", status:"repaired", detail}…]}`；仅纳入语法合法、文件名↔`topic_id` 一致、可达跳链完整的主题，损坏主题跳过并清理。
