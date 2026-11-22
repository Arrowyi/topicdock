# TopicDock CLI Contract Quick Reference (11 subcommands + payload schema + output contract + exit codes)

Command form: `td <subcommand> ...` (`td` = the single-file engine `scripts/topicdock.py`; Python >= 3.9, standard library only).
Global options `--json` / `--store-dir <dir>` / `--workspace <dir>` may appear before or after the subcommand: `--json` prints indented JSON (human-readable mode prints key-value lines); `--store-dir` sets the store directory explicitly; `--workspace` overrides the address-resolution start point (default cwd, bubbling up to the global `~/.topicdock/` — see the bootstrap section in `SKILL.md`).

Syntax authority: `references/markdown-spec.md` (this file does not repeat the dual-template syntax).

---

## 1. The 11 subcommands

| Subcommand | Signature | Core behavior & self-heal guarantees |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | Initializes `<store_dir>` (`.gitignore` incl. the `.topicdock-published.json` line + `.topicdock-meta.json`; the DB is created lazily); with `--path` it additionally writes the pointer config (global → `~/.agents/topicdock/config.json`, local → `<cwd>/.agents/topicdock.json`, content `{"store_dir":"<abs>"}`); idempotent, returns the current state |
| `td use` | `[<topic_id\|index>] [--trace <auto\|items>]` | Explicit selector (pure digits = 1-based index in the same order as `list`, otherwise a topic_id) → returns the md full text + `(head_hop, base_sha full 64 hex)` + three-state artifact probing; optional `--trace` (takes a required value when present and may be omitted entirely — a single-value option with no `nargs="?"`, eliminating argparse ambiguity with the optional positional `topic`): `auto` inlines the full historical evidence blocks of all currently unresolved `[Q]` and `[~]`-state `[A]` items in the same call (`"traces": {"Q1": {...}, ...}`); a comma-separated id list (e.g. `C3,C4,Q3`) inlines the specified items' history blocks (a missing explicit item sets `"traces"[id]` to `{"missing_item": true}` without interrupting the mount); the no-selector list/empty-store branches ignore `--trace` |
| `td match` | `[--topic <kw> [--topic <kw2> ...]] [--threshold 35] [--recent] [--trace <auto\|items>]` | Call after the primary LLM decomposes keywords; deterministic 0-100 scoring, full recall at `>= tau` (ties by `updated_at` DESC, then `topic_id` ASC); the top <=5 hits (`full: true`) return, besides `text`, **`head_hop`, full 64-hex `base_sha`, and `artifacts` directly** (the top-5 hits complete the formal mount in a single call — no second `td use` round; from the 6th item on, `full: false` without these fields); with `--trace <auto\|items>` the top <=5 `full: true` items additionally carry `"traces"`; `--recent` skips scoring and takes the <=5 most recent topics without `--topic` (same ordering as `list`/`use`) |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | The only authoritative write entry: `(base_hop, base_sha)` two-factor CAS + exclusive lock + four-step commit (hop shard → md commit point → DB transaction + undo push); validates the optional `model` type; all 6 hop-writing paths route through `_finalize_hop_merkle_header` to write `topic_uid / parent_oids / origin_oids / hop_oid`; when the head's authoritative `compute_hop_oid` is absent from the `.topicdock-published.json` seal ledger (and the ledger is not corrupt), `--amend` merges in place when a non-empty `session_id` matches, `live_refs(head_hop) == 1`, the head chain has no missing ancestor, and the normalized `actor` subject has not changed (an empty `actor` never counts as a change); otherwise it auto-degrades to a new next hop (`degraded` flagged in the output) — including a sealed head or a corrupt ledger; when the in-place head's `actor` is empty and the payload `actor` is non-empty, the head's `actor` is backfilled |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id <id>] [--actor <name>] [--model <id>] [--next-focus "..."]` | Merges src's live items into dst, renumbered per dst's `next_seq` (`op=inherit, origin=src:item@hop` — trace stays unbroken); generates a dual-parent consolidation hop `[H_dst, H_src]` (inherits dst's `topic_uid`; both parents' `parent_oids`; `--actor` and `--model` default to `""` and do not inherit either parent's `model`); deletes `src.md` (its shards survive via the consolidation hop's references); undo push |
| `td trace` | `<topic_id> <item_id> [<extra_items> ...]` | Single- and multi-item lineage: positional `nargs="+"`, each element also accepts comma splitting (e.g. `C3,C4,Q3`), order-preserving dedupe, one exclusive lock and one in-memory `hop_cache`; **1 parsed item → the single-item structure**; >=2 items → any missing item reports `ITEM_NOT_FOUND` (exit code 5), all present → `{"status": "OK", "topic_id": ..., "item_ids": [...], "items": {...}}` (top-level `blocks` are not duplicated); internally walks `item_hops` in order + `item_links` cross-topic recursion (cycle-guarded) + overturn destination blocks + trailing destination notes; overturned/resolved/folded/consolidated items remain 100% traceable; missing hops are annotated `missing` |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | `updated_at` DESC, 10 per page, continuous global numbering across pages (page 2 = 11-20); `--filter` is a case-insensitive substring match on topic_id/title |
| `td view` | `<topic_id\|index> [--history]` | Purely read-only; `--history` outputs the hop summary chain along the topic's first-parent own chain (`topic_origin == topic_id`) in reverse-chronological order (head first; attaches `model` when the hop header has a non-empty `str` `model`); broken links output the reachable part + `missing`/chain-break annotations |
| `td delete` | `<expr> [--dry-run] [--yes]` | Preview (default / `--dry-run`): index expressions `2,4~6`, `keep 1,3` resolve to a topic_id list + exclusive hop counts + missing hop counts; execution `--yes` **accepts only an explicit topic_id list** (immune to concurrent re-ordering); physically deletes only exclusive shards with `live_refs == 0` |
| `td undo` | `[--force]` | Pops the `undo_stack` top (LIMIT 3): post_hashes verification (`--force` relaxes only this layer) + "no new live child-hop references" always enforced + **target hop sealed in `.topicdock-published.json` (or ledger corrupt) always enforced — `UNDO_BLOCKED`, exit code 7, `--force` cannot bypass either** → journal atomic write → idempotent absolute restore (restore pre_files / delete newly created / per-topic high-watermark bump / full-store recompute) → journal deleted |
| `td doctor` | `[--rebuild]` | No args: seven-step self-heal inspection + repair reports + the two seal/hash reports (`published_ledger` seal-ledger check — missing/valid = `ok`, corrupt = `invalid`; `hop_oid_drift` non-destructive header-hash drift listing); `--rebuild`: forced 100% cold rebuild of the SQLite graph from valid surviving Markdown (the seal ledger is preserved) |

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

- `topic_id`: `^[a-z0-9][a-z0-9-]{0,63}$`, immutable after creation; `title`: any language, changeable anytime (empty/absent = keep/inherit).
- `actor` normalization: `null`/whitespace → `""` (never stored as `"None"`); `model` (optional): `null`/absent/whitespace-after-`strip()` → key not written; a non-`None`, non-`str` value → `INVALID_INPUT` (exit code 7); stored as the `strip()`-ed value; a new hop never inherits the parent hop's `model`; on `--amend` a non-empty string updates it, absent/whitespace keeps the original.
- `base_hop: null` = create a new topic (scenario 1); the response then carries an optional `similar_topics` key — `[{topic_id, title, score}]`, 0..3 **existing** topics with `score >= 35` in descending score order, computed before the write from the payload `title` (or `topic_id` fallback) as a single query phrase; `[]` = computed with no similar topic, key omitted = the computation failed (fail-open, creation unaffected and exit code unchanged) or the save is not a new-topic path; `base_sha` rule: full 64-hex equality, or >=12 chars forming a prefix of the current `topics.md_sha256`.
- **`--fork-as <new_id>`**: the payload `topic_id` = the source topic id, `--fork-as` = the new id (regex-valid and nonexistent); source = the state at base_hop (ancestor forks skip the sha check); the new first hop gets `parents=[base_hop]` and all items copied as `op=inherit, origin=src:item@<initial-creation-hop>` + the payload's mutations stacked in the same hop; combining with `--amend`/`--force-merge` is always `INVALID_INPUT`.
- `artifacts` flow: omitted/`null` → inherit the base_hop snapshot; `[]` → clear; a new list → update and prune dead links.
- `fold` preconditions: every `from_items` entry exists and is `[x]`, otherwise `INVALID_INPUT`.
- `retract` form: `{"op": "retract", "item_id": "<id>"}` (removes a mistaken item without moving it into `[R]`).

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

| Command | Success payload fields |
|---|---|
| `init` | `status/store_dir/scope/created(bool)` |
| `save` | `status/topic_id/head_hop/base_sha(full 64 hex)/amend/degraded/created{track:n}/echo(⚓ line)/conflicts[]/overwritten[]`; on fork success `topic_id` = the new id; new-topic saves (`base_hop: null`) additionally carry `similar_topics[{topic_id,title,score}]` (0..3 existing topics, score ≥ 35 descending; `[]` = none; key omitted on computation failure, fail-open), rendered through the generic key/value output rendering in both `--json` and human-readable modes |
| `use` | `status/topic_id/text(md full text)/head_hop/base_sha(full)/artifacts[{name,path,note,status: ok\|missing\|moved,new_path?}]`; with `--trace` also `traces{...}`; no selector and 0 topics → `{status, topic_id: null, note}`; >=2 → first-page list fields + `hint` |
| `list` | `status/page/page_size/total/items[{index,topic_id,title,updated_at}]` |
| `match` | `status/results[{topic_id,score,title,full(bool),text?(top-5 full text),head_hop?,base_sha?,artifacts?,traces?}]` (`head_hop`/`base_sha`/`artifacts` attach to the top <=5 `full: true` items; `traces` additionally when `--trace` is given) |
| `trace` | single item: `status/topic_id/item_id/blocks[{hop_id,op,text?,missing?,from_topic?}]/links[]/successor[]/successor_notes[]`; >=2 items: `status/topic_id/item_ids[]/items{<id>: <single-item trace dict>}` |
| `view` | `status/topic_id/text` or (with `--history`) `history[{hop_id,created_at,actor,summary,model?,missing?,note?}]` (`model` output only when the hop header has a non-empty `str`) |
| `delete` | preview: `status/topics[{topic_id,title}]/exclusive_hops/missing_hops`; execute: `status/deleted[]/exclusive_hops/missing_hops` |
| `merge` | `status/topic_id/dst_topic_id/head_hop/base_sha/merged_items{track:n}/merged_from/echo` |
| `undo` | empty stack: `{status,note:"回滚栈为空…"}` (exit 0); success: `status/restored[]/op_type/summary` |
| `doctor` | `status/checks[{name,status: ok\|repaired\|deleted\|invalid\|drift,detail?}]` (structure in §7) |

⚓ echo line (the `echo` field of `save`/`merge`), three formats — the Chinese words below (`新建`, `已封板 → 自动新建`, `无新增条目`) are exact runtime protocol literals, quoted verbatim:

- New topic: `⚓ TopicDock: <id> (<head_hop>@<sha6> 新建 | +1[C] +1[R] +1[A])`
- Sealed degradation: `⚓ TopicDock: <id> (<old> 已封板 → 自动新建 <head_hop>@<sha6> | +1[C] +1[A])`
- Regular / amend / merge: `⚓ TopicDock: <id> (<head_hop>@<sha6> | …)` (amend carries an ` amend` segment; no new items count as `无新增条目`)

The three sha sources: the CAS `base_sha` = `topics.md_sha256`; the md `## 4.` line sha = the first 12 hex of `hops.md_sha256`; ⚓ = the first 6 of `base_sha`; `use/save` JSON always returns the full base_sha.

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

No args → seven-step self-heal inspection (order: (7) missing/corrupt DB cold rebuild → (6) meta three-source max repair → (3) crash adoption → (1) wild-file cleanup → (2) top-level md repair → (5) referenced-hop classification → (4) orphan-hop cleanup), then appends the two seal/hash reports and returns:

```json
{"status": "OK", "checks": [{"name": "meta_json", "status": "ok"}, {"name": "md_repair", "status": "repaired", "detail": "auth-refactor（重放还原）"}]}
```

- `status ∈ {ok, repaired, deleted}` for the heal steps; the ledger report adds `published_ledger` with `status ∈ {ok, invalid}` (`detail: "无封板记录"` when empty — an exact runtime literal), and `hop_oid_drift` with `status ∈ {ok, drift}` (drift items listed non-destructively; hops without a `hop_oid` key do not count as drift; a per-hop `compute_hop_oid` failure degrades to that hop's report item instead of crashing doctor).
- Defined `name` values: `rebuild_db` / `meta_json` / `adopt_crashed` / `tmp_sweep` / `wild_cleanup` / `md_repair` / `hop_missing` / `amend_rollforward` / `pending_sweep` / `orphan_cleanup` / `cascade_delete` / `published_ledger` / `hop_oid_drift`.
- `--rebuild` → forced cold rebuild (full-table replay into `topicdock.db.tmp` → `os.replace` atomic swap + old `-wal/-shm` deleted), returning `{status, checks:[{name:"rebuild_db", status:"repaired", detail}...]}`; only topics that are syntactically valid, filename↔`topic_id` consistent, and have complete reachable hop chains are included — corrupted topics are skipped and cleaned up; the existing `.topicdock-published.json` is preserved.
