# TopicDock 双 Markdown 语法规范（`topics/<id>.md` 与 `hops/hop-NNN.md`）

本文件是 design §4.2 的成文版：两个模板的逐字段语法、防注入转义规则、行尾短标降级链与编号水位线。**当前 `SUPPORTED_SCHEMA_VERSION = 1`**；读入 `schema_version > 1` 或 hop 头 `v > 1` 时拒绝并返回 `UNSUPPORTED_SCHEMA_VERSION`（退出码 6），任何写盘/删盘之前先做版本守卫。

总不变量：`parse(render(doc)) == doc`（双向恒等）；顶层 md 与 hop 分片均**仅允许经 `td` 命令写入**，手动修改一律视为文件破坏（自愈详见 `references/cli-contract.md` doctor 节）。

---

## 1. `topics/<topic-id>.md` 顶层主题当前态账本

### 1.1 frontmatter（7 字段）

```markdown
---
schema_version: 1
topic_id: auth-refactor
title: "認證模塊無狀態 JWT 改造與灰度遷移 / Auth Stateless JWT Refactor"
head_hop: H004
updated_at: 2026-11-16T10:30:00Z
last_actor: architect
next_seq: {"C":4,"R":2,"Q":2,"A":4}
---
```

| 字段 | 语义 |
| --- | --- |
| `schema_version` | 恒 `1`；>1 触发版本守卫 |
| `topic_id` | 创建后不可变，`^[a-z0-9][a-z0-9-]{0,63}$`；必须与文件名一致（不一致 = 篡改） |
| `title` | 显示标题，支持任意语言，任意一次 `td save` 可改（载荷传 `title` 非空即更新） |
| `head_hop` | 当前头跳号（`H004`） |
| `updated_at` | 唯一来源 = head 跳头 `created_at`（amend 刷新之，冷重建同口径） |
| `last_actor` | head 跳 `actor` |
| `next_seq` | 各轨道**已用最大编号**（存储语义，见 §4 水位线），如 `{"C":4,"R":2,"Q":2,"A":4}` |

### 1.2 四个分节与条目行语法

```markdown
## 1. 认知与定论 (Knowledge & Conclusions)

### 核心背景与已拍板结论 (Confirmed `[C]`)
- [auth-refactor:C2] 外部网关统一签发 EdDSA 短效 JWT（15m），内部服务零数据库验签 — 依据(verified): 压测 P99 延迟从 18ms 降至 1.2ms (H003)

### 已排除方案与被推翻结论 (Rejected & Overturned `[R]`)
- [auth-refactor:R1] 放弃全链路 Session 共享库方案 — 原因: 跨区只读副本同步延迟导致偶发 401 (原立H001→推翻H003)

### 待继续讨论与未决问题 (Open Questions `[Q]`)
- [auth-refactor:Q1] 第三方 Webhook 回调鉴权是否走同一网关验签中间件？ (H004)

## 2. 行动与进展 (Session Actions `[A]`)
- [x] [auth-refactor:A1] 完成网关鉴权原型压测与算法选型 (H001→H002)
- [~] [auth-refactor:A2] 编写 `design.md` 接口契约与错误码定义 — 断点: 待敲定 Q1 回调签名头格式 (H004)
- [ ] [auth-refactor:A3] 交由 coder 实施中间件与集成测试 (H004)

## 3. 关联文件链接 (Artifact Links)
- 网关架构设计记录: /abs/path/to/design.md — 定义 JWT 载荷字段与公钥轮换接口

## 4. 最新一跳 (Latest Hop)
- **H004** (`2026-11-16T10:30:00Z` | actor: `architect` | parents: `H003` | sha: `9f8e7d6c5b4a`)
- 本跳摘要: 敲定 Refresh Token 旋转机制 `[C3]`，推进设计记录至 `[A2]` 断点。
- 待续焦点 (Next Focus): 拍板 `[Q1]` Webhook 鉴权边界并完成 `design.md` 落盘。
```

- **条目行四段语法**（C/R/Q 压平单行）：`- [<topic_id>:<item_id>] <正文> — 依据(<verified|inferred>): <一句话依据>`；R 轨尾段为 ` — 原因: <一句话原因>`；A 轨带三态前缀 `- [x|~| ] [<topic_id>:<A n>] <正文> — 断点: <断点>`。内部换行压平为空格。
- `## 4.` 最新一跳行内 `sha` = **hops.md_sha256 前 12 位**（hop 分片文件哈希，非 CAS 的 base_sha）；`parents` 仅示首父（多父合流详见 hop 头）。

### 1.3 单行分隔符消歧与转义

- 正文内字面量 ` — 依据(`、` — 原因:`、` — 断点:` 或行尾形如 `(H001)` 的括号，渲染时自动将破折号转义为 `&#8212;`、左括号转义为 `&#40;`；解析时**从右侧匹配最后一个标准分隔符**并还原转义，保证 `parse(render(doc)) == doc`。

---

## 2. 行尾短标（`<auto_trace_tag>`，全形态 ≤20 字符 / UTF-8 ≤32 B）

### 2.1 基本形态

对 `[C<n>]` 与 `[A<n>]`：从 `item_hops` 按 `seq_order` 正序取全部关联跳号 `[h1..hm]`：

- `m = 1` → `(H001)`
- `2 ≤ m ≤ 3` → `(H001→H003→H005)`
- `m > 3` → 折叠中间项 `(H001→..→H012, 6跳)`

对 `[R<n>]`：直接新建排除项同上；由原结论 `[C<k>]` 推翻移入时标 `(原立<origin_hop>→推翻<overturn_hop>)`（`origin_hop` 固定为该原结论首次创建的跳号，即 `seq_order=1`）。

### 2.2 全形态降级链（D-P30/D4）

降级链适用于**全部形态**（m=1/2/3、m>3、R 轨）；超 20 字符或 32 字节时依序降级：

- 普通与折叠形态：`(H1000→..→H1012, 6跳)` → `(H1000..+6跳)` → `(H1000)` → 确定性截断兜底（取前 18 字符 + `)`），硬上限恒成立。
- R 轨形态：`(原立H1001→推翻H1003)` → `(推翻H1003)` → 确定性截断兜底。

跳号 ≥3 位补零、自然增长（`H999→H1000`）、比较按数值。

---

## 3. `hops/hop-NNN.md` 历史增量分片

### 3.1 第 1 行安全 JSON 注释头（canonical）

第 1 行固定：`<!-- topicdock-hop: <canonical-json> -->`——`json.dumps(header, ensure_ascii=False, sort_keys=True)` 紧凑单行；转义顺序**先** `--` → `-\u002d`，**再** `<` → `\u003c`、`>` → `\u003e`（防注释截断与 HTML 注入；摘要含 `-->` 时不会截断注释头）。

**必填字段集（11 个，含 `title` 与 `artifacts` 快照，保障重放修复与冷重建 100% 无损）**：

```json
{
  "v": 1,
  "hop_id": "H003",
  "topic_origin": "auth-refactor",
  "title": "認證模塊無狀態 JWT 改造與灰度遷移 / Auth Stateless JWT Refactor",
  "parents": ["H002"],
  "session_id": "ses-01",
  "created_at": "2026-11-16T09:15:00Z",
  "actor": "consultant",
  "model": "opencode-go/gemini-4-argon",
  "summary": "基于跨区压测推翻 H001 的共享 Session 方案(C1->R1)，确立 EdDSA 短效 JWT(C2)",
  "next_focus": "设计 Refresh Token 吊销机制",
  "artifacts": [{"name": "网关架构设计记录", "path": "/abs/path/design.md", "note": "定义 JWT 载荷字段与公钥轮换接口"}]
}
```

- **可选溯源字段 `model`**（`v: 1` 前向兼容可选键）：记录本跳所用模型标识，`actor` 仅记录操作主体、不再拼接 `@<model>` 后缀。
  - 仅当 `td save` 载荷 `"model"`（或 `td merge --model`）为非空字符串（`strip()` 后非空）时写入跳头；未传、`null` 或空白串视为缺省，**不写入 `"model"` 键**（老跳与无 `model` 保存的字节与 `sha256` 不变）。传入非 `None` 且非 `str` 类型 → `INVALID_INPUT`（退出码 7）。
  - 新建跳（首次建档、常规新跳、降级新跳、`--fork-as`、`--force-merge`、`td merge`）不继承父跳 `model`；同跳 `--amend` 传入非空串时更新，缺省/空白保留原值。
  - `model` 仅存于本行 JSON 注释头：不修改 `topics/<id>.md` 的 `## 4.` 行，也不新增 SQLite 列。
- 解析**保留任意额外字段**、渲染**原样透传**（前向兼容）。
- 头部第 2 行起为 `# Hop H003 — auth-refactor` 标题行。

### 3.2 `title` / `artifacts` 快照流转（D-P33）

- ① `td save` 载荷省略 `artifacts`（或 `null`）→ 继承 `base_hop` 头快照；显式 `[]` → 清空；传入新列表 → 更新并**自动剔除已销毁死链**。
- ② `td merge <src> <dst>` 合流跳：`artifacts` 取 dst 与 src 最新跳快照的并集（**按 `path` 保序去重**）；`title` 取 dst。
- ③ 篡改修复、冷重建（`doctor --rebuild`）及从历史祖先跳 `--fork-as` 分叉：直接从目标跳头 `title` 与 `artifacts` 100% 还原主题标题与 `## 3. 关联文件链接`。

### 3.3 条目块标题行属性语法

```
### [<item_id>] (op=<op>[, status=verified|inferred][, state=x|~|space][, from=<id1>+<id2>][, origin=<topic>:<item>@<hop>])
```

- `op ∈ {create, update, overturn, resolve, fold, inherit, retract}`（7 种）；多来源 ID 用 `+` 连接（如 `from=A1+A2+A3`）；`origin` 承载推翻/解决/继承的来源条目初始创建跳。
- **块字段 = 操作后完整条目快照**（`update` 含全量 post-state 字段，非增量）：`结论正文` / `一句话依据`（R 轨为 `一句话原因`、A 轨为 `断点`）/ `推导与权衡` / `核心证据 (verified|inferred)` / `重审条件`。
- 块内多行字段**续行统一缩进 2 个空格**；字段内容某行以 `### [` 开头时渲染转义为 `\### [`（防冷重建误切分为新条目块）。

完整 hop 示例（与 design §4.2(3) 一致）：

```markdown
<!-- topicdock-hop: {"actor":"consultant","artifacts":[{"name":"网关架构设计记录","note":"定义 JWT 载荷字段与公钥轮换接口","path":"/abs/path/to/design.md"}],"created_at":"2026-11-16T09:15:00Z","hop_id":"H003","model":"opencode/claude-sonnet-5-5","next_focus":"设计 Refresh Token 吊销机制","parents":["H002"],"session_id":"ses-01","summary":"基于跨区压测推翻 H001 的共享 Session 方案(C1->R1)，确立 EdDSA 短效 JWT(C2)","title":"認證模塊無狀態 JWT 改造與灰度遷移 / Auth Stateless JWT Refactor","topic_origin":"auth-refactor","v":1} -->

# Hop H003 — auth-refactor

### [R1] (op=overturn, from=C1, origin=auth-refactor:C1@H001)
- 结论正文: 放弃全链路 Session 共享库方案
- 一句话原因: 跨区只读副本同步延迟导致偶发 401

### [C2] (op=create, status=verified)
- 结论正文: 外部网关统一签发 EdDSA 短效 JWT（15m），内部服务零数据库验签
- 一句话依据: 压测 P99 延迟从 18ms 降至 1.2ms
```

---

## 4. 编号水位线（单调防重用不变量）

### 4.1 全局跳号水位线（D-P30 四源 max）

```
next_hop_seq = max(db.meta.next_hop_seq, .topicdock-meta.max_hop_seq + 1,
                   max(hop 文件编号) + 1, max(topics.head_hop 编号) + 1)
```

每次分配新跳时同步更新 `.topicdock-meta.json`；分配号一律走引擎内 `allocate_hop_id`，禁止旁路。

### 4.2 主题内各轨道编号水位线（D-P30 三源 max）

```
next_seq[T] = max(frontmatter.next_seq[T], max(topic.md(T)) + 1, max(item_hops(T)) + 1)
```

**存储语义**：md frontmatter 与 DB `topics.next_seq_json` 存的是**「已用最大号」**（非 +1 语义的下一号）；分配时由引擎按上式取 max 后再 +1，分配结果严禁写回 md 当水位（否则崩溃纳管的 `md == render` 门禁会拒收合法窗口 md）。

### 4.3 undo 后 md 高水位语义

`td undo` 绝对还原恢复旧 md 时，仅对**本次实际恢复的 md** 将 frontmatter `next_seq` 强制覆写为 `max(还原值, 撤前已用最大 + 1)`——被撤回条目的编号永不复用；旁观主题不受影响（不传染）。merge 续编与 save 重放基座同样套 md frontmatter 水位 floor（撤号不复用、md 水位不回退）。

---

## 5. 已知边界（设计已确认的不改代码残余）

- **F8（undo 后 next_seq 极端场景）**：undo 后的高水位仅存于 md frontmatter 与 DB topics 行——若 DB **同时**损坏且从 hops 链重放（冷重建），已撤回条目的编号可能被复用；属可接受极端场景，不追加防御。
- **F13 残余**：① `MISSING` hop 还原后 sha ≠ `MISSING` 会按失配进入下一轮判定（先按损坏路径处置，收敛正确但多一轮）；② 「提交后、删文件前崩溃」由 ③ 崩溃纳管 + ⑤ DB 闭包同轮再级联闭环收敛（无独立 undo 条目为已接受损失）；③ 版本守卫每命令全量扫 md/hop 文件有额外读盘开销；④ heal 主体中途 `DatabaseError` 无重建重试兜底；⑤ `quick_check` 失败分支无专测。
- **M7（F15）残余**：① undo journal 的启动 heal 接线未实现——入口拒绝（journal 残留时 undo 报 `INVALID_INPUT` 退出码 7，需手动删 `.topicdock-undo-pending.json`）已把无消费者窗口收窄到进程内崩溃；② 无参 doctor 对健康库仅报 `meta_json: ok` 一项（非全维 ok 项展开）；③ undo 棘轮使被撤轨道编号产生 +1 空洞（永不复用语义的代价，可接受）；④ delete undo payload 对乱码文件预像用 bytes/base64 编码落库，`json.dumps(ensure_ascii=False)` 文本列在极端场景可能不可读（F13 同源记录）。
