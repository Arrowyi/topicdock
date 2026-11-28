# TopicDock Dual Markdown Syntax Specification (`topics/<id>.md` and `hops/hop-NNN.md`)

This specification defines the per-field syntax of both templates, anti-injection escaping rules, the trailing short-tag downgrade chain, and numbering watermarks. **Current `SUPPORTED_SCHEMA_VERSION = 1`**; reading `schema_version > 1` or a hop header `v > 1` is rejected with `UNSUPPORTED_SCHEMA_VERSION` (exit code 6) before any write/delete.

Global invariant: `parse(render(doc)) == doc` (bidirectional identity). Both the top-level md and hop shards are **writable only via `td` commands**; any manual modification counts as file corruption (self-healing: see the `doctor` section in `references/cli-contract.md`).

> **Protocol-literal note**: the Chinese section titles, field labels, separators, and short-tag forms shown in the code samples below (` — 依据(verified): `, ` — 原因: `, ` — 断点: `, `结论正文`, `一句话依据`, `一句话原因`, `推导与权衡`, `核心证据 (verified)`, `核心证据 (inferred)`, `重审条件`, `断点`, `(原立H001→推翻H003)`, `跳`, the four `##` section titles, etc.) are **exact L2 codec protocol literals**. They are part of the on-disk format and MUST be reproduced verbatim when generating or parsing — never translated in ledger content. English text around them is explanation only.

---

## 1. `topics/<topic-id>.md` top-level current-state ledger

### 1.1 Frontmatter (7 fields)

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

| Field | Semantics |
| --- | --- |
| `schema_version` | Always `1`; `>1` triggers the version guard |
| `topic_id` | Immutable after creation, `^[a-z0-9][a-z0-9-]{0,63}$`; must match the file name (a mismatch = tampering) |
| `title` | Display title, any language, changeable in any `td save` (a non-empty `title` in the payload updates it) |
| `head_hop` | Current head hop id (`H004`) |
| `updated_at` | Sole source = head hop header `created_at` (refreshed by `--amend`, same rule on cold rebuild) |
| `last_actor` | Head hop `actor` |
| `next_seq` | **Largest number already used** per track (storage semantics, see §4 watermarks), e.g. `{"C":4,"R":2,"Q":2,"A":4}` |

### 1.2 The four sections and item-line syntax

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

## 3. 关联文件链接
- 网关架构设计记录: /abs/path/to/design.md — 定义 JWT 载荷字段与公钥轮换接口

## 4. 最新一跳 (Latest Hop)
- **H004** (`2026-11-16T10:30:00Z` | actor: `architect` | parents: `H003` | sha: `9f8e7d6c5b4a`)
- 本跳摘要: 敲定 Refresh Token 旋转机制 `[C3]`，推进设计记录至 `[A2]` 断点。
- 待续焦点 (Next Focus): 拍板 `[Q1]` Webhook 鉴权边界并完成 `design.md` 落盘。
```

- **Item-line four-segment syntax** (C/R/Q flattened to one line): `- [<topic_id>:<item_id>] <body> — 依据(<verified|inferred>): <one-line basis>`; the R-track tail segment is ` — 原因: <one-line reason>`; the A track carries a three-state prefix `- [x|~| ] [<topic_id>:<A n>] <body> — 断点: <breakpoint>`. Internal newlines are flattened to spaces.
- The `## 4.` latest-hop line's inline `sha` = **first 12 hex of `hops.md_sha256`** (the hop shard file hash — not the CAS `base_sha`); `parents` shows only the first parent (multi-parent consolidation details live in the hop header).

### 1.3 Single-line separator disambiguation and escaping

Separator disambiguation is guaranteed by two layers:

1. **Save-entry guard (write time).** `td save` rejects — through `_normalize_mutations`, covering the new-topic / amend / fork / force-merge payload paths — any mutation whose `statement` or `basis` embeds a complete separator pattern (` — 依据(verified): `, ` — 依据(inferred): `, ` — 原因: `, ` — 断点: `), or whose `basis` begins with a separator without a leading space (the structural separator's trailing space would complete a higher-position fake separator at the splice boundary). The result is `INVALID_INPUT` (exit code 7) with the item context in the message. Basis/reason/breakpoint text belongs in the `basis` field; the renderer adds the structural separator. Hop rich fields (`rationale` / `evidence_detail` / `review_cond`, multi-line shard format) are outside the guard.
2. **Render-time synthesized-body semantic minimal escaping (read/replay time).** Rendering first synthesizes the plaintext item body for the item's render form (`statement` flattened + structural separator + `basis` flattened), then splits it with the same logic as `_split_item_body` and compares the recovered pair against the render pair **by raw separator positions** (never by comparing unescaped output). Only when the plaintext form would mis-split does it escape the em dash of the offending complete-pattern occurrences as `&#8212;`:
   - Structural separator present and the rightmost separator sits exactly at the real structural boundary → zero escaping (patterns inside `statement` are harmless: `rfind` prefers the real boundary).
   - Structural separator present but a pattern occurs beyond the boundary (inside `basis`, or a splice-boundary fake separator formed with the structural separator's trailing space) → escape those occurrences only.
   - No structural separator (Q track, C without `evidence_status`, or empty `basis`) → escape **all** complete-pattern occurrences in the body (escaping only the last would let an earlier plaintext pattern win `rfind`).

The four item render forms are: C-with-`evidence_status` renders `statement — 依据(<status>): basis`; R renders `statement — 原因: basis`; A renders `statement — 断点: basis`; **Q and C-without-`evidence_status` keep `basis` in the item state but do not render it**, so their synthesized body carries no structural separator. Because the decision depends only on separator positions in the synthesized body, `replay == md` always holds and rendering is a deterministic pure function; payload text containing a literal `&#8212;` does not trigger over-escaping.

- **Trailing parenthetical group** (collision with `<auto_trace_tag>`): a trailing `(...)` group is still escaped as `&#40;...)` under the unchanged current rule. `&#40;` is not part of the semantic separator escaping and remains a legal on-disk entity.
- **Semantic-required entity rows**: a row that would mis-split in its render form keeps its `&#8212;` deterministically until that item's text is updated (the subsequent update is guided by the save-entry guard into a legal shape). `doctor` restricts its `legacy_entity_cleanup` sweep to chain-replayable topics whose item rows still contain `&#8212;` and whose bytes differ from the new semantic render (see `references/cli-contract.md` §7).
- **Degraded parse path** (`_state_from_md`, used when the head shard is missing): its parsed state is guaranteed by "rendering is always parse-faithful". The only exceptions are payloads whose literal text contains `&#8212;` / `&#40;`: the read-side restore turns them into `—` / `(` — pre-existing behavior, declared as-is.

---

## 2. Trailing short-tag (`<auto_trace_tag>`, all forms <=20 chars / UTF-8 <=32 B)

### 2.1 Basic forms

For `[C<n>]` and `[A<n>]`: take all associated hop ids `[h1..hm]` from `item_hops` in `seq_order` order:

- `m = 1` → `(H001)`
- `2 <= m <= 3` → `(H001→H003→H005)`
- `m > 3` → middle hops folded `(H001→..→H012, 6跳)`

For `[R<n>]`: a directly created rejected item follows the C/A forms above; one moved in by overturning an original conclusion `[C<k>]` is tagged `(原立<origin_hop>→推翻<overturn_hop>)` (where `origin_hop` is fixed to the hop id where the original conclusion was first created, i.e. `seq_order=1`).

### 2.2 Full-form downgrade chain

The downgrade chain applies to **all forms** (m=1/2/3, m>3, R track); beyond 20 chars or 32 bytes it degrades in this order:

- Plain and folded forms: `(H1000→..→H1012, 6跳)` → `(H1000..+6跳)` → `(H1000)` → deterministic truncation fallback (first 18 chars + `)`), hard cap always holds.
- R-track form: `(原立H1001→推翻H1003)` → `(推翻H1003)` → deterministic truncation fallback.

Hop ids are zero-padded to >=3 digits and grow naturally (`H999→H1000`); comparisons are numeric.

---

## 3. `hops/hop-NNN.md` historical incremental hop shard

### 3.1 First-line safe JSON comment header (canonical)

The first line is always: `<!-- topicdock-hop: <canonical-json> -->` — `json.dumps(header, ensure_ascii=False, sort_keys=True)` on one compact line; escaping order is **first** `--` → `-\u002d`, **then** `<` → `\u003c`, `>` → `\u003e` (prevents comment truncation and HTML injection; a summary containing `-->` never truncates the header).

**Required base field set (11 fields, including the `title` and `artifacts` snapshots, guaranteeing 100% lossless replay repair and cold rebuild)**:

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

- **Optional provenance field `model`** (a `v: 1` forward-compatible optional key): records the model identifier used for this hop; `actor` records only the acting principal (never an `@<model>` suffix).
  - Written only when the `td save` payload `"model"` (or `td merge --model`) is a non-empty string (non-empty after `strip()`); missing / `null` / whitespace-only counts as absent and **the `"model"` key is not written** (hops and saves without `model` keep identical bytes and `sha256`). A non-`None`, non-`str` value → `INVALID_INPUT` (exit code 7).
  - Newly created hops (first topic creation, regular new hop, degraded new hop, `--fork-as`, `--force-merge`, `td merge`) do not inherit the parent hop's `model`; on same-hop `--amend`, a non-empty string updates it, while absent/whitespace keeps the existing value.
  - `model` lives only in this JSON comment header line: it never modifies the `topics/<id>.md` `## 4.` line and adds no SQLite column.
- Parsing **preserves any extra fields** and rendering **passes them through verbatim** (forward compatibility).
- From the second line the header is followed by the `# Hop H003 — auth-refactor` title line.

#### 3.1.1 The 4 position-independent Merkle identity fields (`v: 1` forward-compatible optional extension)

Hop header fields are strictly decoupled into "store-local routing slots" (`hop_id`, `parents`, `topic_origin`, and the per-block `origin` attribute — rewritable on cross-store import collisions) and "global position-independent Merkle identity" (100% invariant across cross-store renumbering and collision renames). All 6 hop-writing entry points (`_save_new_topic`, `_save_amend`, `_save_fork`, regular `_save_locked`, `_save_force_merge`, `_merge_payload`) route through the single finalizer `_finalize_hop_merkle_header(store, conn, topic_id, header, blocks, chain, rep)`, which writes:

1. **`topic_uid`** (`str`, 32-hex topic lineage id): `uuid.uuid4().hex` on first topic creation (`base_hop: null`) or branch fork (`--fork-as <new_id>`); `td merge <src> <dst>` takes the destination `<dst>`'s `topic_uid`. Regular new hops, `--amend`, and `--force-merge` inherit the most recent non-empty `str` `topic_uid` found along the reachable own chain (walking from `base_hop` toward genesis; on a head break, surviving shards with `topic_origin == topic_id` are scanned in descending hop-number order). Only when no hop on the reachable own chain carries `topic_uid` is it deterministically derived from the reachable earliest hop `H_earliest` as `sha256(b"topicdock-topic-uid:v1\0" + compute_hop_oid(H_earliest).encode("utf-8")).hexdigest()[:32]` (on `--amend` of a head without `topic_uid`, extraction/derivation happens **before** `created_at / summary / blocks` are modified).
2. **`parent_oids`** (`list[str]`, aligned 1:1 by index with `parents`): for each parent hop `P_i`, always the **live-recomputed** `compute_hop_oid(parent_loaded.hop)` (never a possibly stale cached value); when the parent file is missing on disk the slot is `""` to preserve index alignment; a genesis hop is `[]`.
3. **`origin_oids`** (`dict[str, str]`, optional — the key is written only when this hop's `blocks` contain a non-empty `block.origin`; omitted entirely when empty): `{ "<block.item_id>": "<src_topic_uid>:<src_item_id>@<origin_hop_oid>" }`, resolved via `_parse_origin(block.origin)`; a source store without a persisted `topic_uid` gets a `""` slot (foreign topics are never filled with a derived value); a missing source hop gets `""` in the `origin_hop_oid` slot.
4. **`hop_oid`** (`str`, format `"v1:<64-hex>"` — position-independent pure-function content hash): computed last, over the finalized header + blocks.

**`compute_hop_oid(hop: HopDoc) -> str` (L2 pure function, one non-recursive implementation shared by all hops)**:
- First normalize via `norm_hop = parse_hop_md(render_hop_md(hop))`, then build a **fixed 13-key dictionary** (all top-level keys always present; missing/abnormal values become safe empties `"" / [] / {}`): `v` (int, default 1), `topic_uid`, `title`, `parent_oids`, `session_id`, `created_at`, `actor`, `model` (only non-empty strings, stripped), `summary`, `next_focus`, `artifacts` (list of dicts with `name`/`path`/`note` all stringified), `origin_oids` (`sorted` then `{str: str}`), and `blocks` (in `norm_hop.blocks` order; each block a fixed 7-key dict `item_id / op / status / a_state / from_items / origin_item / fields`, with `origin_item` = `_parse_origin(b.origin)[1]` or `""`, and `fields` stringified in `_HOP_FIELD_ORDER` intersection order).
- Then `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")`, prefixed with the domain separator `b"topicdock-hop-oid:v1\0"`, hashed with SHA-256, returned as `"v1:" + hex`.
- **Position-independence guarantee**: the store-local slots `hop_id`, `parents`, `topic_origin`, the full local `origin` string on blocks, and `hop_oid` itself are excluded from the 13 keys (while the block-level source item id `origin_item` and the top-level `origin_oids` are included), so `compute_hop_oid(hop)` is 100% invariant across cross-store `HNNN` renumbering or collision renames.

**`_load_hop` non-destructive tolerance**: a hop file with any of the 4 fields missing, type-abnormal (e.g. externally written `null`), or with `header["hop_oid"] != compute_hop_oid(hop)` (e.g. an external edit or in-place amend that did not refresh `hop_oid`) is **never rejected**; the read side safely normalizes to `"" / [] / {}`. When `file_sha256 == db.hops.md_sha256` (i.e. the file passed tamper verification) but the header `hop_oid` disagrees, the live-recomputed `compute_hop_oid(hop)` is the single authority; `td doctor` reports it non-destructively as `hop_oid_drift`. Hops without the 4 Merkle fields are never rewritten in the background (zero disk churn).

### 3.2 `title` / `artifacts` snapshot flow

- (1) A `td save` payload that omits `artifacts` (or passes `null`) inherits the `base_hop` header snapshot; an explicit `[]` clears it; a new list updates it and **automatically prunes destroyed dead links**.
- (2) A `td merge <src> <dst>` consolidation hop: `artifacts` is the union of the dst and src latest-hop snapshots (**deduplicated by `path`, order-preserving**); `title` is taken from dst.
- (3) Tamper repair, cold rebuild (`doctor --rebuild`), and forking from a historical ancestor hop via `--fork-as`: the topic title and `## 3. 关联文件链接` are restored 100% from the target hop header's `title` and `artifacts`.

### 3.3 Item block title attribute syntax

```
### [<item_id>] (op=<op>[, status=verified|inferred][, state=x|~|space][, from=<id1>+<id2>][, origin=<topic>:<item>@<hop>])
```

- `op ∈ {create, update, overturn, resolve, fold, inherit, retract}` (7 kinds); multiple source ids are joined with `+` (e.g. `from=A1+A2+A3`); `origin` carries the initial creation hop of the source item for overturn/resolve/inherit.
- **Block fields**: `inherit` blocks record the full item snapshot (`_item_fields_snapshot`), while `create`, `update`, `overturn`, `resolve`, and `fold` blocks record the explicitly supplied payload fields (`_save_field_map`); `retract` blocks carry no fields. During replay, `update` modifies only the fields present in the block (`_apply_fields_to_item`). The field labels are exact protocol literals: `结论正文` / `一句话依据` (R track: `一句话原因`; A track: `断点`) / `推导与权衡` / `核心证据 (verified)` / `核心证据 (inferred)` / `重审条件` — 8 Chinese field names in total.
- Continuation lines of a multi-line field are **uniformly indented by 2 spaces**; a line starting with `### [` is escaped to `\### [` when rendered (prevents cold-rebuild mis-splitting into a new item block).

Full hop example:

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

## 4. Numbering watermarks (monotonic no-reuse invariants)

### 4.1 Global hop-number watermark (four-source max)

```
next_hop_seq = max(db.meta.next_hop_seq, .topicdock-meta.max_hop_seq + 1,
                   max(hop file numbers) + 1, max(topics.head_hop numbers) + 1)
```

`.topicdock-meta.json` is updated whenever a new hop id is allocated; allocation always goes through the engine's `allocate_hop_id` — bypassing it is forbidden.

### 4.2 Per-track numbering watermark inside a topic (three-source max)

```
next_seq[T] = max(frontmatter.next_seq[T], max(topic.md(T)) + 1, max(item_hops(T)) + 1)
```

**Storage and allocation semantics**: in normal operation, the md frontmatter `next_seq[T]` and DB `topics.next_seq_json` store the **largest item number already used** on track `T` (updated by `_note_seq` during replay). Before allocating, `next_seq_for_topic` computes the three-source maximum `max(frontmatter.next_seq[T], max(topic.md(T)) + 1, max(item_hops(T)) + 1)`, and `_make_allocator` allocates that value directly (defaulting to `1` when the maximum is `0`) and increments its in-memory counter by `1` for subsequent allocations in the same save. Normal saves never write an unallocated `used + 1` value into `doc.next_seq` (so `render_topic_md(rep.doc)` matches the on-disk md during crash adoption).

### 4.3 Post-undo md high-watermark semantics

When `td undo` restores an old md, only the **actually restored md** has its frontmatter `next_seq` forcibly overwritten to `max(restored value, largest number used before the undo + 1)` — the number of a retracted item is never reused; bystander topics are unaffected (no contagion). Merge renumbering and the save replay base apply the same md frontmatter watermark floor (no number reuse, md watermark never regresses).

---

## 5. Store housekeeping files

- `.topicdock-meta.json` — persisted watermark `{"schema_version": 1, "max_hop_seq": N}`.
- `.topicdock.lock` — cross-platform exclusive/shared file lock (POSIX `flock` / Windows `msvcrt`).
- `.topicdock-undo-pending.json` / `.topicdock-amend-pending.json` — crash-window journals (removed after their transaction commits).
- **`.topicdock-published.json` — publish seal ledger**: records hop oids that have been exported/synced, as `{"v": 1, "published_oids": ["v1:<64-hex>", ...]}`. Publishing seals a hop without rewriting any `hop-NNN.md` byte (0 cross-window CAS false conflicts). Reading is fail-closed: a missing file is `(set(), False)`; any parse/shape failure is `(set(), True)`. A sealed head's `--amend` auto-degrades to a new next hop, and `td undo` on a sealed hop — or while the ledger is corrupt — always reports `UNDO_BLOCKED` (exit code 7), which `--force` cannot bypass. The ledger is listed in the store `.gitignore` as machine-local state and is preserved by `doctor --rebuild`.
- `.gitignore` — written by `td init` with exactly these lines: `topicdock.db*`, `.topicdock.lock`, `*.tmp`, `.topicdock-undo-pending.json`, `.topicdock-amend-pending.json`, `.topicdock-published.json`.

---

## 6. Known boundaries

- **Post-undo cold-rebuild watermark boundary**: the post-undo high watermark lives only in the md frontmatter and the DB topics row — if the DB is **also** corrupted and replayed from the hop chain (cold rebuild), a retracted item's number may be reused.
- **Self-heal and crash-window boundaries**: (1) after a `MISSING` hop is restored, `sha != MISSING` enters the next judging round (handled via the corruption path — converges correctly, one extra round); (2) a crash between "commit and file deletion" is closed by crash adoption (3) plus DB closure (5) in the same round (no dedicated undo entry); (3) the version guard scans all md/hop files on every command (extra I/O); (4) the heal main path has no rebuild retry when it hits `DatabaseError` mid-way; (5) the `quick_check` failure branch has no dedicated test.
- **Undo journal and doctor boundaries**: (1) startup heal wiring for the undo journal is not implemented — entry rejection (undo reports `INVALID_INPUT` exit 7 while `.topicdock-undo-pending.json` remains; delete the file manually) narrows the un-consumed window to an in-process crash; (2) a no-arg `doctor` on a healthy store reports only `meta_json: ok` (plus `published_ledger`, `hop_oid_drift`, and `match_index`, all `ok`); (3) the undo ratchet leaves a +1 hole in retracted track numbers; (4) the delete-undo payload encodes garbage-file pre-images as base64 bytes, so `json.dumps(ensure_ascii=False)` into a text column may be unreadable in extreme cases.
