# TopicDock CLI Contract Quick Reference (11 subcommands + payload schema + output contract + exit codes)

Command form: `td <subcommand> ...` (`td` = the single-file engine `scripts/topicdock.py`; Python >= 3.9, standard library only).
Global options `--json` / `--store-dir <dir>` / `--workspace <dir>` may appear before or after the subcommand: `--json` prints indented JSON (human-readable mode prints multi-line key-value text — see the "Human-readable rendering" contract in §3); `--store-dir` sets the store directory explicitly; `--workspace` overrides the address-resolution start point (default cwd, bubbling up to the global `~/.topicdock/` — see the bootstrap section in `SKILL.md`).

Syntax authority: `references/markdown-spec.md` (this file does not repeat the dual-template syntax).

---

## 1. The 11 subcommands

| Subcommand | Signature | Core behavior & self-heal guarantees |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | Initializes `<store_dir>` (`.gitignore` incl. the `.topicdock-published.json` line + `.topicdock-meta.json`; the DB is created lazily); with `--path` it additionally writes the pointer config (global → `~/.agents/topicdock/config.json`, local → `<cwd>/.agents/topicdock.json`, content `{"store_dir":"<abs>"}`); idempotent, returns the current state |
| `td use` | `[<topic_id\|index>] [--trace <auto\|items>]` | Explicit selector (pure digits = 1-based index in the same order as `list`, otherwise a topic_id) → returns the md full text + `(head_hop, base_sha full 64 hex)` + three-state artifact probing; optional `--trace` (takes a required value when present and may be omitted entirely — a single-value option with no `nargs="?"`, eliminating argparse ambiguity with the optional positional `topic`): `auto` inlines the full historical evidence blocks of all currently unresolved `[Q]` and `[~]`-state `[A]` items in the same call (`"traces": {"Q1": {...}, ...}`); a comma-separated id list (e.g. `C3,C4,Q3`) inlines the specified items' history blocks (a missing explicit item sets `"traces"[id]` to `{"missing_item": true}` without interrupting the mount); the no-selector list/empty-store branches ignore `--trace` |
| `td match` | `[--topic <kw> [--topic <kw2> ...]] [--tier all\|partial\|any] [--recent] [--trace <auto\|items>]` | Call after the primary LLM decomposes keywords (one keyword per concept — multiple keywords AND-narrow: a topic matching every query token lands in the `all` tier, matching ≥ ⌈|T|/2⌉ tokens in `partial`, matching any token in `any`); tiered recall with an in-tier relative bm25 score of 0-100 (`round(1 + 99*(worst-bm25)/(worst-best))`, degenerate buckets all 100; ties by `updated_at` DESC, then `topic_id` ASC); `--tier` is a cumulative recall floor — `all` = the all tier only (**default**), `partial` = all+partial with all first, `any` = all three layers; the `recall` summary is computed over all three layers (`any_term` is the union count of all tiers, not the any-tier count). The top <=5 hits (`full: true`) return, besides `text`, **`head_hop`, full 64-hex `base_sha`, and `artifacts` directly** — match **locates only**; the formal mount is always completed by `td use <id>` (never use match's `text` field as mount evidence; from the 6th item on, `full: false` without these fields); with `--trace <auto\|items>` the top <=5 `full: true` items additionally carry `"traces"`; `--recent` skips scoring and takes the <=5 most recent topics without `--topic` (same ordering as `list`/`use`; entries carry `score: null` with no `tier`/`matched_terms`, and the payload has no `recall`/`hint`). `--threshold` is removed: old callers fail closed with an argparse usage error, exit code 2. Empty-token queries, >500 query tokens, and `--recent` together with `--tier` → `INVALID_INPUT` (exit code 7); FTS5 unavailable → `ERROR` (exit code 1; the message contains「td match 需要 SQLite 启用 FTS5；当前平台 <sys.platform> / SQLite <version>」— the emitted line carries a `MatchIndexUnavailable: ` prefix, and the message is optionally suffixed with「（<detail>）」). **Human-readable mode renders each hit as its own block** (never one collapsed JSON line): separator line → metadata key/value lines in the order `topic_id`/`title`/`tier`/`matched_terms`/`score`/`head_hop`/`full`/`updated_at` → the full 64-hex `base_sha` on its own line → `artifacts` line by line → the decoded `text` md line by line → end line; `full: false` hits emit the metadata block only (no `text`). See §3 for the shared folding rule |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | The only authoritative write entry: `(base_hop, base_sha)` two-factor CAS + exclusive lock + four-step commit (hop shard → md commit point → DB transaction + undo push); validates the optional `model` type; all 6 hop-writing paths route through `_finalize_hop_merkle_header` to write `topic_uid / parent_oids / origin_oids / hop_oid`; when the head's authoritative `compute_hop_oid` is absent from the `.topicdock-published.json` seal ledger (and the ledger is not corrupt), `--amend` merges in place when a non-empty `session_id` matches, `live_refs(head_hop) == 1`, the head chain has no missing ancestor, and the normalized `actor` subject has not changed (an empty `actor` never counts as a change); otherwise it auto-degrades to a new next hop (`degraded` flagged in the output) — including a sealed head or a corrupt ledger; when the in-place head's `actor` is empty and the payload `actor` is non-empty, the head's `actor` is backfilled |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id <id>] [--actor <name>] [--model <id>] [--next-focus "..."]` | Merges src's live items into dst, renumbered per dst's `next_seq` (`op=inherit, origin=src:item@hop` — trace stays unbroken); generates a dual-parent consolidation hop `[H_dst, H_src]` (inherits dst's `topic_uid`; both parents' `parent_oids`; `--actor` and `--model` default to `""` and do not inherit either parent's `model`); deletes `src.md` (its shards survive via the consolidation hop's references); undo push |
| `td trace` | `<topic_id> <item_id> [<extra_items> ...]` | Single- and multi-item lineage: positional `nargs="+"`, each element also accepts comma splitting (e.g. `C3,C4,Q3`), order-preserving dedupe, one exclusive lock and one in-memory `hop_cache`; **1 parsed item → the single-item structure**; >=2 items → any missing item reports `ITEM_NOT_FOUND` (exit code 5), all present → `{"status": "OK", "topic_id": ..., "item_ids": [...], "items": {...}}` (top-level `blocks` are not duplicated); internally walks `item_hops` in order + `item_links` cross-topic recursion (cycle-guarded) + overturn destination blocks + trailing destination notes; overturned/resolved/folded/consolidated items remain 100% traceable; missing hops are annotated `missing` |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | `updated_at` DESC, 10 per page, continuous global numbering across pages (page 2 = 11-20); `--filter` is a case-insensitive substring match on topic_id/title |
| `td view` | `<topic_id\|index> [--history]` | Purely read-only; `--history` outputs the hop summary chain along the topic's first-parent own chain (`topic_origin == topic_id`) in reverse-chronological order (head first; attaches `model` when the hop header has a non-empty `str` `model`); broken links output the reachable part + `missing`/chain-break annotations |
| `td delete` | `<expr> [--dry-run] [--yes]` | Preview (default / `--dry-run`): index expressions `2,4~6`, `keep 1,3` resolve to a topic_id list + exclusive hop counts + missing hop counts; execution `--yes` **accepts only an explicit topic_id list** (immune to concurrent re-ordering); physically deletes only exclusive shards with `live_refs == 0` |
| `td undo` | `[--force]` | Pops the `undo_stack` top (LIMIT 3): post_hashes verification (`--force` relaxes only this layer) + "no new live child-hop references" always enforced + **target hop sealed in `.topicdock-published.json` (or ledger corrupt) always enforced — `UNDO_BLOCKED`, exit code 7, `--force` cannot bypass either** → journal atomic write → idempotent absolute restore (restore pre_files / delete newly created / per-topic high-watermark bump / full-store recompute) → journal deleted |
| `td doctor` | `[--rebuild]` | No args: seven-step self-heal inspection + repair reports + the two seal/hash reports (`published_ledger` seal-ledger check — missing/valid = `ok`, corrupt = `invalid`; `hop_oid_drift` non-destructive header-hash drift listing) + the doctor-only `legacy_entity_cleanup` legacy over-escape sweep (see §7; not in the seven-step sequence, so read commands stay zero-write) + the `match_index` FTS5 index inspection (`ok` = schema + row count + integrity-check passed; `unavailable` = SQLite without FTS5; `mismatch` = integrity-check failure); `--rebuild`: forced 100% cold rebuild of the SQLite graph from valid surviving Markdown (the seal ledger is preserved), rebuilding the match index with it (`match_index: rebuilt`; FTS5-unavailable platforms report `match_index: unavailable`) |

Every command except `td init` runs the seven-step self-heal on startup under an exclusive lock (repair when possible; unrepairable corruptions or a changed ID cascade-delete; healthy topics are never blocked).

---

## 2. `td save` payload schema (`--stdin` / `--json-file`)

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
    {"op": "update", "item_id": "A2", "state": "~", "statement": "编写 `design.md` 接口契约与错误码定义", "basis": "待敲定 Q1 回调签名头格式", "rationale": "..."},
    {"op": "overturn", "from_item": "C1", "statement": "...", "basis": "...", "rationale": "...", "evidence_detail": "...", "review_cond": "..."},
    {"op": "resolve", "from_item": "Q1", "into_track": "C", "statement": "...", "evidence_status": "verified", "basis": "..."},
    {"op": "fold", "from_items": ["A1", "A2", "A3"], "statement": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}
  ],
  "artifacts": [
    {"name": "网关架构设计记录", "path": "/abs/path/to/design.md", "note": "定义 JWT 载荷字段与公钥轮换接口"}
  ]
}
```

- `topic_id`: `^[a-z0-9][a-z0-9-]{0,63}$`, immutable after creation; `title`: any language, changeable anytime (empty/absent = keep/inherit).
- `actor` normalization: `null`/whitespace → `""` (never stored as `"None"`); `model` (optional): `null`/absent/whitespace-after-`strip()` → key not written; a non-`None`, non-`str` value → `INVALID_INPUT` (exit code 7); stored as the `strip()`-ed value; a new hop never inherits the parent hop's `model`; on `--amend` a non-empty string updates it, absent/whitespace keeps the original.
- `base_hop: null` = create a new topic (scenario 1); the response then carries an optional `similar_topics` key — `[{topic_id, title, score}]`, 0..3 **existing** topics with `score >= 35` (title token coverage: `score = round(100 × matched / |title tokens|)`, a 0-100 percentage; the new topic's own id is excluded; bm25 tie-break) in descending score order, computed before the write from the payload `title` (or `topic_id` fallback); `[]` = computed with no similar topic, key omitted = the computation failed (fail-open, creation unaffected and exit code unchanged) or the save is not a new-topic path; `base_sha` rule: full 64-hex equality, or >=12 chars forming a prefix of the current `topics.md_sha256`.
- **`--fork-as <new_id>`**: the payload `topic_id` = the source topic id, `--fork-as` = the new id (regex-valid and nonexistent); source = the state at base_hop (ancestor forks skip the sha check); the new first hop gets `parents=[base_hop]` and all items copied as `op=inherit, origin=src:item@<initial-creation-hop>` + the payload's mutations stacked in the same hop; combining with `--amend`/`--force-merge` is always `INVALID_INPUT`.
- `artifacts` flow: omitted/`null` → inherit the base_hop snapshot; `[]` → clear; a new list → update and prune dead links.
- `fold` preconditions: every `from_items` entry exists and is `[x]`, otherwise `INVALID_INPUT`.
- `retract` form: `{"op": "retract", "item_id": "<id>"}` (removes a mistaken item without moving it into `[R]`).
- `state` semantics: on `create`, the A track accepts `x`/`~`/space with space as the default (key absent = space; the current validation is unchanged); on `update`, `state` applies to `[A]` items only. Carrying `state` on a non-A-track `create`/`update` → `INVALID_INPUT` (exit 7) — a non-A-track update carrying `state` is no longer silently ignored.
- **Payload whitelist (per-op exact key sets)**: unknown keys → `INVALID_INPUT` (exit 7) with **all** unknown key names listed in the error message. The check is pinned at the `_save_locked` entry, before the branch tree (new topic / `--fork-as` / `--force-merge` / `STALE_BASE` early exit), so it holds on every save path. Top-level allowed keys: `topic_id`, `title`, `base_hop`, `base_sha`, `session_id`, `actor`, `model`, `summary`, `next_focus`, `parent_hops`, `mutations`, `artifacts`. Mutation keys by op — common set `op`/`statement`/`basis`/`rationale`/`evidence_detail`/`review_cond`/`evidence_status`, plus:

  | op | keys beyond the common set |
  |---|---|
  | `create` | `track`, `state` (A-track only) |
  | `update` | `item_id`, `state` (A-track only) |
  | `overturn` | `from_item` |
  | `resolve` | `from_item`, `into_track` |
  | `fold` | `from_items` |
  | `retract` | none — a `retract` mutation carries **only** `op`/`item_id` (the common set does not apply) |

- **Separator guard**: a mutation `statement`/`basis` must not embed a complete item separator (` — 依据(verified): `, ` — 依据(inferred): `, ` — 原因: `, ` — 断点: `), and `basis` must not begin with one without a leading space (the structural separator's trailing space would complete a splice-boundary fake separator). A hit on any mutation of any op (create/update/overturn/resolve/fold, across the new-topic / amend / fork / force-merge payload paths) → `INVALID_INPUT` (exit code 7) with the item context; put the basis/reason/breakpoint text in the `basis` field and let the renderer add the structural separator. Ordinary punctuation such as `— ； （ ） 「」 ·` is unaffected; hop rich fields (`rationale`/`evidence_detail`/`review_cond`) are out of scope.

### ops state-transition matrix (7 ops × md changes × shard changes × amend merge rules)

| Op | Tracks | Change to top-level `topics/<id>.md` | Change to `hops/hop-NNN.md` and `item_hops / item_links` | Same-hop `--amend` merge rule |
|---|---|---|---|---|
| `create` | `C / R / Q / A` | Allocates a new number `next_seq[T]++` and appends to the matching section | Writes `### [<new_id>] (op=create, ...)`; `item_hops` records `op='create'` | Keeps `op=create` |
| `update` | `C / R / Q / A` | Updates the given `item_id`'s body, basis, or action state `[x/~]` in place | Writes `### [<id>] (op=update, ...)`; `item_hops` records `op='update'` | If the item was just `create`-d in this hop, the merged block keeps `op=create` (body updated); otherwise stays `op=update` |
| `overturn` | `C -> R` | Removes `from_item` from `[C]`, allocates a new `R<n>` appended to `[R]` tagged `(原立H_orig→推翻H_cur)` | Writes `### [R<n>] (op=overturn, from=C<k>, origin=...)`; records `item_links` | If the overturned `C<k>` was created in this hop, replaced directly by `R<n> (op=create)` |
| `resolve` | `Q -> C / R / None` | Removes `from_item` from `[Q]`; if `into_track='C'` or `'R'`, also creates the new item | Writes `### [Q<k>] (op=resolve)` (and the new `### [C<n>]`); records `item_links` | If `Q<k>` was created in this hop and resolved in the same hop, converges into the result item |
| `fold` | `A -> A` | Removes the given completed `- [x] A_i` rows, allocates a new `- [x] A_new` summary row in their place | Writes `### [A_new] (op=fold, from=A1+A2+A3)`; records `item_links` | Unions the `from` sets |
| `inherit` | `C/R/Q/A` on fork/consolidation | On fork or `td merge`, copies source items into the target top-level file per the target's `next_seq` | Writes `### [<id>] (op=inherit, origin=<src_topic>:<src_id>@<hop>)`, chaining cross-topic `trace` | Keeps `op=inherit` |
| `retract` | `C / R / Q / A` | Removes a mistaken or abandoned item (does not move into `[R]`) | Writes `### [<id>] (op=retract)` | If created in this hop, cancelled directly within this hop's shard |

Payload mutations are normalized into a HopBlock sequence and pass one by one through the single replay kernel `apply_hop_block` (both directions use the same function); a payload incompatible with topic state (e.g. `[retract X, update X]` illegal order, self-referencing fold) → `INVALID_INPUT` with **zero writes**.

---

## 3. Output JSON contract (`--json`; `status` maps 1:1 to exit codes)

`--json` output is byte-for-byte stable: each command emits exactly `json.dumps(result, ensure_ascii=False, indent=2)` plus a trailing newline, and the human-readable display layer described below never alters it.

| Command | Success payload fields |
|---|---|
| `init` | `status/store_dir/scope/created(bool)` |
| `save` | `status/topic_id/head_hop/base_sha(full 64 hex)/amend/degraded/created{track:n}/echo(⚓ line)/conflicts[]/overwritten[]`; on fork success `topic_id` = the new id; new-topic saves (`base_hop: null`) additionally carry `similar_topics[{topic_id,title,score}]` (0..3 existing topics, score = 0-100 title-token coverage percentage with score ≥ 35, descending; `[]` = none; key omitted on computation failure, fail-open), rendered through the generic key/value output rendering in both `--json` and human-readable modes |
| `use` | `status/topic_id/text(md full text)/head_hop/base_sha(full)/artifacts[{name,path,note,status: ok\|missing\|moved,new_path?}]`; with `--trace` also `traces{...}`; no selector and 0 topics → `{status, topic_id: null, note}`; >=2 → first-page list fields + `hint`. Human mode: multi-line — `text`/`traces` are emitted line by line, with long lines folded (see "Human-readable rendering" below) |
| `list` | `status/page/page_size/total/items[{index,topic_id,title,updated_at}]`; human mode: `items` renders one element per line (`- index: …` blocks) with folding (see below) |
| `match` | `status/results[{topic_id,title,updated_at,tier,matched_terms,score,full(bool),text?(top-5 full text),head_hop?,base_sha?,artifacts?,traces?}]` (`tier` ∈ `all|partial|any` = cumulative hit breadth, `matched_terms` = hit token count; `head_hop`/`base_sha`/`artifacts` attach to the top <=5 `full: true` items; `traces` additionally when `--trace` is given). Non-`--recent` calls additionally carry `recall{all_terms,partial,any_term,total,returned}` (`all_terms` = topics whose tier is `all`; `partial` = topics whose tier is `partial`; `any_term` = the union count of all three tiers — cumulative, **not** the any-tier count; `total` = topics in the store; `returned` = entries actually returned) and an optional `hint` (omitted on precise narrow hits); `--recent` entries carry `score: null` with no `tier`/`matched_terms`, and the payload has neither `recall` nor `hint`. Match **locates only** — the formal mount is always completed by `td use <id>`; a match hit (including its `text` field) is never the mount credential. Human mode: one block per hit — separator line + `topic_id`/`title`/`tier`/`matched_terms`/`score`/`head_hop`/`full`/`updated_at` key lines + full `base_sha` on its own line + `artifacts` line by line + decoded `text` line by line + end line; `full: false` hits are metadata-only (no `text`); long lines folded (see below) |
| `trace` | single item: `status/topic_id/item_id/blocks[{hop_id,op,text?,missing?,from_topic?}]/links[]/successor[]/successor_notes[]`; >=2 items: `status/topic_id/item_ids[]/items{<id>: <single-item trace dict>}`. Human mode: `blocks` renders one element per line with each block `text` line by line, folded (see below) |
| `view` | `status/topic_id/text` or (with `--history`) `history[{hop_id,created_at,actor,summary,model?,missing?,note?}]` (`model` output only when the hop header has a non-empty `str`); human mode: `text`/`history` line by line with folding (see below) |
| `delete` | preview: `status/topics[{topic_id,title}]/exclusive_hops/missing_hops`; execute: `status/deleted[]/exclusive_hops/missing_hops` |
| `merge` | `status/topic_id/dst_topic_id/head_hop/base_sha/merged_items{track:n}/merged_from/echo` |
| `undo` | empty stack: `{status,note:"回滚栈为空…"}` (exit 0); success: `status/restored[]/op_type/summary` |
| `doctor` | `status/checks[{name,status: ok\|repaired\|deleted\|invalid\|drift\|rebuilt\|unavailable\|mismatch,detail?}]` (structure in §7); human mode: `checks` renders one element per line, folded (see below) |

### Human-readable rendering (all 11 subcommands, when `--json` is absent)

- **Multi-line by default.** Nested values are never collapsed into one JSON line: a `dict` renders one line per key (2 spaces of indent per level), a `list` renders one line per element and recurses into a non-empty nested list element (a dict element becomes a `- key: value` block). Structural lines are indented by level, while **verbatim content lines** — the mount `text` and the hop-block `text` inside `traces`/`blocks` on `use --trace`, `match --trace`, and `trace` — are emitted **line by line verbatim at column 0**: their own newlines preserved, no JSON escaping, no added indentation, so markdown headings (`### [C1] …`) and item lines (`- 结论正文: …`) stay intact and are not turned into indented code blocks.
- **Over-long lines are folded.** Every final physical line whose **UTF-8 byte length** exceeds **1200 bytes** is hard-folded at a **code-point boundary** (a multi-byte character is never split). Each continuation line begins with the continuation marker, whose exact literal is pinned here:

  **`↪ `** — `U+21AA` RIGHTWARDS ARROW WITH HOOK followed by one `U+0020` space.

  The marker contains no ASCII `-`, no `—`, no `:`, no `#`, no `&#`, and none of the four item-separator literals (` — 依据(verified): `, ` — 依据(inferred): `, ` — 原因: `, ` — 断点: `), so it is protocol-neutral: a marker line neither triggers `_split_item_body` mis-splitting nor is consumed as hop content by `parse_hop_md`. Treat the marker as a display-layer artifact only — never copy it back into any `td save` payload.
- **Hard budget.** Every human-mode physical line is ≤ **1500 bytes** UTF-8 (and therefore ≤ 1500 characters, since a character is ≥ 1 byte), i.e. `len(line.encode("utf-8")) ≤ 1500` always holds. This keeps output intact under byte- or character-counting line truncation such as `awk '{print length}'` or `cut -c1-2000` in any locale.
- **`--json` is byte-for-byte unchanged.** JSON mode is always the canonical indented JSON (`json.dumps(result, ensure_ascii=False, indent=2)`) with no folding and no continuation markers; this display layer touches human-readable output only.

⚓ echo line (the `echo` field of `save`/`merge`), three formats — the Chinese words below (`新建`, `已封板 → 自动新建`, `无新增条目`) are exact runtime protocol literals, quoted verbatim:

- New topic: `⚓ TopicDock: <id> (<head_hop>@<sha6> 新建 | +1[C] +1[R] +1[A])`
- Sealed degradation: `⚓ TopicDock: <id> (<old> 已封板 → 自动新建 <head_hop>@<sha6> | +1[C] +1[A])`
- Regular / amend / merge: `⚓ TopicDock: <id> (<head_hop>@<sha6> | …)` (amend carries an ` amend` segment; no new items count as `无新增条目`)

The three sha sources: the CAS `base_sha` = `topics.md_sha256`; the md `## 4.` line sha = the first 12 hex of `hops.md_sha256`; ⚓ = the first 6 of `base_sha`; `use/save` JSON always returns the full base_sha.

**Hop-header line-truncation note (P6)**: the first line of `hops/hop-NNN.md` is the JSON comment header (title/summary/next_focus/artifacts plus the `topic_uid`/`parent_oids`/`origin_oids`/`hop_oid` Merkle fields) and routinely exceeds 2000 characters — reading hop shards directly through line-truncating tools (`cut -c1-2000`, `head -c`, terminal viewport clipping) necessarily breaks that line; this is expected behavior, not corruption. Authoritative field sources are the topic md `## 4.` latest-hop mirror and the hop body blocks; the engine paths are `td trace <topic_id> <item_id>` / `td view <topic_id> --history`.

---

## 4. Exit code table (0-7, complete)

| Code | status | Meaning |
|---|---|---|
| 0 | `OK` | Success (includes the undo empty-stack note and an all-`ok` doctor) |
| 1 | `ERROR` | Unexpected-exception fallback (`{status, error:"<type>: <message>"}`, no traceback) |
| 2 | — (no JSON) | argparse usage error (missing required option / unknown subcommand, etc.) |
| 3 | `UNINITIALIZED_STORE` | Neither the bubble search nor the global dir found a store: `{status, default_global:"~/.topicdock", workspace_local:"<cwd>/.topicdock"}` → bootstrap onboarding |
| 4 | `STALE_BASE` | CAS desync (payload in §5), never applied silently |
| 5 | `TOPIC_NOT_FOUND` / `ITEM_NOT_FOUND` | Topic/item not found (selector resolution failures in use/view/trace/merge/delete, `--yes` referencing a nonexistent id, missing items in multi-item trace) |
| 6 | `UNSUPPORTED_SCHEMA_VERSION` | `schema_version > 1` or hop header `v > 1` (guarded before any write/delete) |
| 7 | `INVALID_INPUT` | Input-class errors: payload not JSON / validation failure, `base_hop` not on the first-parent lineage chain, payload referencing vanished item ids (with `vanished_items`), `--fork-as` combined with `--amend/--force-merge`, unmet fold preconditions, illegal/out-of-range delete expression, empty `--trace` item list (e.g. `","`), `UNDO_BLOCKED` (see §6.2), undo-journal-residue entry rejection |

---

## 5. The `STALE_BASE` payload and 5C absorption flow

Mismatch payload fields:

```json
{
  "status": "STALE_BASE",
  "topic_id": "auth-refactor",
  "current_head": "H006",
  "current_sha": "<full 64 hex>",
  "hops_after_base": ["H005", "H006"],
  "changed_items": ["C2", "A1"],
  "conflicts": ["C2"],
  "auto_absorbable": false
}
```

- `hops_after_base`: new hops after base; `changed_items`: items changed by those hops; **conflict criteria** = the intersection of the item ids touched by the payload's mutations (update/overturn/resolve/fold/retract targets) with `changed_items` (`create` never conflicts), plus header-field conflicts (a payload carrying `title`/`artifacts` while any hop in `hops_after_base` has a `title` or `artifacts` header differing from `base_hop`'s same-named header → `conflicts` includes `"title"`/`"artifacts"`).
- `auto_absorbable = (conflicts is empty)`.
- **Absorption flow**: `auto_absorbable: true` → re-send with the new base to absorb (call `td use` to refresh the body first if needed; `base_hop: current_head`, `base_sha: current_sha`).
- `auto_absorbable: false` → present the user a two-way choice: `--fork-as <new_id>` fork (no overwrite risk; a fork hitting a same-head sha mismatch can still absorb, without modifying the source topic) or `--force-merge` overwrite (replays on top of the current head, last write wins; the success response attaches `overwritten[]` with elements fixed as `{item_id, by_hop}`; a payload referencing vanished item ids → `INVALID_INPUT` + `vanished_items`, exit 7).
- Special case: same head but sha mismatch (the head was amended by another window) → `{status:"STALE_BASE", reason:"BASE_SHA_MISMATCH_HEAD", changed_items:[], conflicts:[], auto_absorbable:false}`; `--force-merge` still passes.
- Precondition: `base_hop` must lie on the current head's first-parent lineage chain, otherwise `INVALID_INPUT` (exit 7).

---

## 6. Narrow-window roll-forward and `UNDO_BLOCKED`

### 6.1 Referenced-hop sha mismatch classification

- **Missing** = broken-link skip, no cascade: `hops.md_sha256` set to `MISSING` (`topics.md_sha256` untouched, keeping CAS stable); trace/view/delete annotate the missing hop; cascade only when the topic's md is also broken and replay needs that hop.
- **The single roll-forward window**: a sha mismatch rolls forward (file wins, recomputing all derived rows and the md via the replay kernel) only when **all** hold: the hop is some live topic's head + `live_refs == 1` + (the md `## 4.` sha prefix equals the new file hash, **or** md and DB are jointly stale = the amend 2→3 / 3→4 crash window) + the file hash matches the `.topicdock-amend-pending.json` record (`{"hop_id", "expected_sha"}`, atomically written before the in-place merge, deleted after the DB commit).
- **All other sha mismatches** (sealed ancestors, non-head, `live_refs > 1`, including syntactically valid manual tampering) and mismatches with no pending record / a mismatching / unparseable / header-mismatched record → treated as **corrupted** → dependent topics handled as "repair when possible, else cascade-delete", with the corrupted hop file physically removed in the cascade.
- **Merge windows (2)(3) narrow roll-forward**: if merge crashes between "dst md written" and "DB not committed", the next startup's crash-adoption step finds the dst head is a consolidation hop whose parents include a surviving src head → completes the consolidation (deleting src rows + src.md); undo has no entry for this window.
- Pending-record deletion is always deferred until after the transaction commits; unowned/already-persisted pending residue is swept by self-heal (the `pending_sweep` report item).

### 6.2 `UNDO_BLOCKED`

No dedicated status: always `{"status":"INVALID_INPUT", ...}` + exit code 7. Three triggers:

1. **Child-hop reference check (always enforced; `--force` does not relax)**: the hop to be removed/restored has later live child-hop references → `{status:"INVALID_INPUT", detail:"UNDO_BLOCKED：跳存在后续存活子跳引用", blocked_hop, child_hop}`. (The detail string is an exact runtime literal.)
2. **Seal-ledger check (always enforced; `--force` cannot bypass)**: the target hop's authoritative `compute_hop_oid` is listed in `.topicdock-published.json`, **or the ledger is corrupt** (fail-closed) → undo is always blocked, including `delete`-type stack entries that carry no `new_hop`.
3. **post_hashes verification (relaxable by `--force`)**: the on-disk file hash disagrees with the snapshot → `{status:"INVALID_INPUT", detail:"UNDO_BLOCKED：磁盘文件哈希与快照不一致（--force 可放宽）", path, expected, actual}`.

Additionally, an undo entry rejection: a `.topicdock-undo-pending.json` journal residue (last rollback interrupted) → `INVALID_INPUT` exit 7; delete that file manually first (heal wiring not implemented — see the known-boundaries section of `references/markdown-spec.md`).

---

## 7. `td doctor` report structure

No args → seven-step self-heal inspection (order: (7) missing/corrupt DB cold rebuild → (6) meta three-source max repair → (3) crash adoption → (1) wild-file cleanup → (2) top-level md repair → (5) referenced-hop classification → (4) orphan-hop cleanup), then appends the two seal/hash reports, the doctor-only `legacy_entity_cleanup` sweep, and returns:

```json
{"status": "OK", "checks": [{"name": "meta_json", "status": "ok"}, {"name": "md_repair", "status": "repaired", "detail": "auth-refactor（重放还原）"}, {"name": "match_index", "status": "ok"}]}
```

- `status ∈ {ok, repaired, deleted}` for the heal steps; the ledger report adds `published_ledger` with `status ∈ {ok, invalid}` (`detail: "无封板记录"` when empty — an exact runtime literal), and `hop_oid_drift` with `status ∈ {ok, drift}` (drift items listed non-destructively; hops without a `hop_oid` key do not count as drift; a per-hop `compute_hop_oid` failure degrades to that hop's report item instead of crashing doctor), and the `match_index` inspection with `status ∈ {ok, rebuilt, unavailable, mismatch}` (`ok` detail carries the schema, row count, and integrity-check result).
- Defined `name` values: `rebuild_db` / `meta_json` / `adopt_crashed` / `tmp_sweep` / `wild_cleanup` / `md_repair` / `hop_missing` / `amend_rollforward` / `pending_sweep` / `orphan_cleanup` / `cascade_delete` / `published_ledger` / `hop_oid_drift` / `legacy_entity_cleanup` / `match_index`.
- `legacy_entity_cleanup`（doctor-only, **not part of the seven-step self-heal sequence** — `use`/`match`/`list` and every other read command stay zero-write）: scans live topics whose item rows still contain `&#8212;` bytes **and** whose current md bytes differ from the new semantic chain-replay render. A chain-replayable topic is overwritten in place via the same path as `md_repair` and reports `repaired`, syncing `topics.md_sha256` (the replay render is parse-faithful by construction; no per-item re-check); its `next_seq` floor is the per-track max of the md frontmatter and the DB `next_seq_json`, and the DB `next_seq_json` row is committed **before** the md is overwritten (no dangling transaction that could roll back the watermark after a crash). A chain-unreplayable topic is silently skipped (zero report — its md repair belongs to `md_repair`'s cascade semantics). Rows whose bytes already equal the semantic render — semantic-required entity rows, or literal entity content — are zero-hit/zero-report, so the sweep is naturally idempotent (a second `doctor` adds no report). `&#40;` is not scanned. Legacy entity-free splice-boundary mis-split rows produced by the old engine's per-field escaping gap are outside the cleanup selection set; they are repaired by that topic's next `save` or on an sha mismatch.
- One-time `legacy_entity_cleanup` side effects (documented behavior): changing `topics.md_sha256` makes an already-mounted window see one `STALE_BASE` (`reason: "BASE_SHA_MISMATCH_HEAD"`, not auto-absorbable — refresh with `td use`); the affected hops' undo `post_hashes` mismatch → `UNDO_BLOCKED` unless `--force`.
- `--rebuild` → forced cold rebuild (full-table replay into `topicdock.db.tmp` → `os.replace` atomic swap + old `-wal/-shm` deleted), returning `{status, checks:[{name:"rebuild_db", status:"repaired", detail}...]}`; only topics that are syntactically valid, filename↔`topic_id` consistent, and have complete reachable hop chains are included — corrupted topics are skipped and cleaned up; the existing `.topicdock-published.json` is preserved; the match index is rebuilt with the cold rebuild and reported as `match_index: rebuilt` (`match_index: unavailable` on FTS5-lacking platforms).
