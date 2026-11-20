# TopicDock (`td`)

*Your topics live in one dock — plug in Claude Code, Cursor, Codex, or OpenCode anytime to continue.*

[简体中文](README.zh-CN.md)

**TopicDock is a zero-dependency personal topic memory. It saves your discussions, decisions, and progress as Markdown ledgers organized by topic, so you can pick up where you left off in any AI agent client.**

## Why TopicDock

Say you are halfway through a design in Claude Code and switch to Cursor. You have to explain everything again. Or you settle a decision in Codex, and OpenCode has never heard of it. Your progress is locked inside each client's own "session" (one conversation window), and a session ends when you change windows or clients.

TopicDock changes the unit of memory from the client-bound **session** to a **topic** that you own. A topic is stored as plain Markdown files (`topics/<topic-id>.md` plus `hops/hop-NNN.md`) and is read and written by your agent through the `td` command, so it belongs to no client. Claude Code, Cursor, Codex, or OpenCode can mount the same topic at any time. The agent then sees the full set of conclusions, rejected options, open questions, and the current breakpoint, and keeps going.

The engine uses only the Python ≥ 3.9 standard library, with no third-party dependencies. Its only executable is the single file `scripts/topicdock.py` (alias `td`).

## Highlights & Core Concepts

- **Orthogonal dual-track ledger (`[C/R/Q]` + `[A]`).** The *knowledge & conclusions* track holds confirmed conclusions `[C]`, rejected or overturned options `[R]`, and open questions `[Q]`. The *session actions* track holds stage-level actions `[A]`, each marked `[ ]` to do, `[~]` in progress (a breakpoint), or `[x]` done. There is no lifecycle state machine. As long as a topic is not deleted, it stays active, resumable, and open to revision.
- **Two-layer Markdown ledger + derived graph index.**
  - `topics/<topic-id>.md` is the current-state, single-page panorama. Each line ends with an automatic evolution tag of at most 20 characters, such as `(H001→H003)`.
  - `hops/hop-NNN.md` stores each hop (one saved increment) with its rich evidence blocks and header snapshot.
  - `topicdock.db` (SQLite in WAL [write-ahead logging] mode) is only a derived index. It gives millisecond lookup, DAG [directed acyclic graph] reference counting, and the undo stack. If it is lost, it can be rebuilt 100% from the Markdown.
- **Upward store resolution.** From the current working directory, `td` walks up parent directories looking for `.topicdock/` (or the pointer file `.agents/topicdock.json`) and uses the nearest match. If none is found, it falls back to the global store `~/.topicdock/`. If that does not exist either, it returns `UNINITIALIZED_STORE` (exit code 3) to guide you through creating a store.
- **Anchor credential line (`⚓`).** After every `td save` (or `td merge`), the agent echoes `⚓ TopicDock: <topic-id> (<head_hop>@<first-6-chars-of-base_sha>)` at the end of its reply. While the context is intact, the session needs no repeated database reads. If a long session is compacted (older conversation is trimmed away), the agent re-runs `td use <id>` using this line and recovers the full context.

## Three-Pillar Workflow (Read / Trace / Write)

The authoritative workflow specification is [`SKILL.md`](SKILL.md). It defines three habits, which the installer wires into your agent as short resident rules.

**Read (mount a topic).** The agent loads a topic only when it needs one. That happens at the first substantive topic of a session, when you name a topic (with `/topicdock`, or the `继续 <id|序号>` phrase, meaning "continue `<id|index>`"), or when you move mid-session to a topic that is not mounted yet. The agent then either runs `td use` or breaks your request into keywords for `td match`. The first up to 5 `full: true` hits from `td match` (including `--recent`) come back directly with `head_hop`, the full 64-hex `base_sha`, the ledger `text`, and `artifacts`, so a single match completes the mount — no second `td use` in the same turn. Only when the target ranks 6th or later does the agent run one extra `td use <id>` to finish the mount. Once a topic is mounted, later turns in the same session and role talk directly, with zero repeated lookups. After context compaction, or when the session switches agent role, the agent re-runs `td use <id>` using the `⚓` line from its previous message.

**Trace (go to the evidence).** The agent runs `td trace <topic_id> <item_id>` before answering in three cases (multiple items can be traced in one call: `td trace <topic> C1,C2,Q3`):
1. You question, overturn, or dig into a mounted item (`[C]`, `[R]`, `[Q]`, or `[A]`, including folded, overturned, or merged items).
2. A new session or role takes over an open `[Q]` or a `[~]` breakpoint and needs the multi-line reasoning and core evidence from earlier hops.
3. The agent is building on existing `[C]`/`[R]` conclusions and the one-line summary does not settle an edge case.

The trace pulls the full evidence blocks from the underlying `hop-NNN.md` files. The agent must not guess history from a one-line summary.

**Write (save at natural convergence points).** Only the top-level main agent saves. Subagents never call `td match` or `td save`. The agent saves at four points: a conclusion is reached or overturned, an open question appears that needs a decision from you or a later session or role, an action finishes (`[x]`) or stops at a handoff breakpoint (`[~]`), or the topic changes or wraps up. It saves in the same turn, before replying, and does not ask "should I save?" first. Within the same session and the same `actor`, follow-up increments pass `--amend` to update the current hop in place. Across sessions, or when the `actor` changes, the agent omits `--amend` and a new hop is created automatically. After saving, the reply ends with the `⚓` line. Markdown files are written only by `td`, so do not edit them by hand.

How the pieces connect: `install.py` injects the three resident rules into your workspace's `CLAUDE.md` or `AGENTS.md` (see [Installation](#installation)). The `/topicdock` command template points to this same workflow (see [The /topicdock Command](#the-topicdock-command)).

```bash
td match --topic jwt --topic refresh-token   # find candidate topics by keyword (score 0–100, default threshold 35)
td use auth-refactor                         # mount: returns the full ledger + head_hop + full base_sha
td trace auth-refactor C2                    # trace: every evidence block and the fate of C2
td save --stdin --json --amend < payload.json   # same-session increment: update the current hop in place
```

## The /topicdock Command

After installation, type `/topicdock` in your client to mount or switch topics. It is one command, not a command family.

- **Direct mount with an argument.** `/topicdock <id|index>` calls `td use <id|index>` to mount that topic. The agent receives the ledger text and continues with the three-pillar workflow.
- **Guided flow without an argument.** `/topicdock` calls `td use` with no argument, and what you see depends on how many topics the store holds:
  - **0 topics:** the agent asks what you want to work on, then creates the topic.
  - **Exactly 1 topic:** it mounts directly.
  - **2 or more topics:** it shows page 1 as a list, with rows in the format `序号 | 标题 | id | 更新时间` (index | title | id | updated at). Below the list are plain-language prompts that adapt to the list's actual state:
    - Reply with an index to mount that topic.
    - Reply `删 <序号>` ("delete `<index>`") to delete. The agent first shows a dry-run preview, and nothing is deleted until you reply with that topic's exact `topic_id`.
    - Reply "next page" or "all" if there are more pages.
    - Give a keyword to filter the list.

  The list exists only for choosing and deleting.
- **Argument passing.** The command template receives any text after the command through `$ARGUMENTS`. If it looks like `<id|index>`, the agent runs `td use <id|index>` directly. Otherwise it falls back to the no-argument guided flow above.

The single source of truth for the command is `scripts/templates/topicdock-command.md`. The line after its YAML frontmatter (the metadata header at the top of the file) is the fixed sentinel `<!-- topicdock:command-managed -->`. Installed copies live here:

| Client | Command file |
|---|---|
| `claude` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/commands/topicdock.md` |

A file with the same name that contains the sentinel is managed by TopicDock. Reinstalling upgrades it and uninstalling removes it. A file without the sentinel is a foreign file. The installer warns and skips it, so it is never silently overwritten, and uninstall leaves it alone.

`codex` does not get a command template, because Codex has deprecated custom prompts (custom slash commands) in favor of Agent Skills. Codex therefore uses the skill directory plus the rule block in `<cwd>/AGENTS.md`.

## Installation

**Requirements:** Python ≥ 3.9 and nothing else. It uses only the standard library, so there is no `pip install`.

**One-line install.** Run it in a terminal, or paste it into an AI agent chat:

```bash
curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | bash
```

- To read the script before running it, pipe it to a pager instead: `curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | less`. It is plain text, not compressed or obfuscated.
- In an AI agent chat (Claude Code, Cursor, OpenCode, Codex), paste the command as-is. The agent runs it and completes the install.
- To install or upgrade a specific client, add the optional flag: `... | bash -s -- --client cursor`. With no arguments, the script upgrades the clients already installed on your machine. If none has ever been installed, it prints example commands for all four clients and exits with code 2. Re-run it with `--client`.
- The install injects a resident wake-up rule block into `<cwd>/AGENTS.md` in your **current working directory** (`<cwd>/CLAUDE.md` for `claude`). Run the installer from your project root so the rules land in your project's root `AGENTS.md` / `CLAUDE.md`. The block includes the engine call path for your machine, and injection is idempotent, so re-running it is safe. On uninstall, the block is kept or removed depending on whether other installed clients still need it.

**Install from source.** Clone the repository and run:

```bash
python3 scripts/install.py --client claude|opencode|cursor|codex|all [--dest <dir>] [--uninstall]
```

`install.py` does the following:

1. Copies `SKILL.md`, `references/`, and `scripts/topicdock.py` into the client's skill directory.
2. Idempotently injects the resident wake-up rule block into `<cwd>/CLAUDE.md` (`claude`) or `<cwd>/AGENTS.md` (`opencode` / `cursor` / `codex`).
3. Installs the `/topicdock` command template (see the table below). `--dest <dir>` installs somewhere other than the default client locations, redirects command templates to `<dest>/<client>/commands/`, and skips creating the global store.
4. Creates a symlink at `~/.local/bin/td` pointing to the installed engine. That directory must be on your `PATH`. If it is not writable, the installer prints an `alias` suggestion instead.
5. Unless `--dest` is set, creates or confirms the global store `~/.topicdock/` at the end (idempotent).

`--uninstall` reverses all of this: the skill directory, the command templates (only files carrying the sentinel), the rule block, and the symlink. Anything in your memory files outside the rule block is preserved. Uninstalling never deletes your topic database.

Where each client's skill and command template go (the `--client` values match the first column):

| Client | Skill directory | Command template |
|---|---|---|
| `claude` | `~/.claude/skills/topicdock` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/skills/topicdock` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/skills/topicdock` | `~/.cursor/commands/topicdock.md` |
| `codex` | `<cwd>/.agents/skills/topicdock` | none (Codex custom prompts are deprecated) |

Notes:

- The Codex skill directory is the only project-level shared root, `<cwd>/.agents/skills/topicdock`. The rule block in `<cwd>/AGENTS.md` is shared by `opencode`, `cursor`, and `codex`. Uninstalling any one of them checks the other clients' skill directories and keeps or removes the block accordingly. The block in `<cwd>/CLAUDE.md` belongs to `claude` alone.
- The `td` symlink runs the `python3` found on your `PATH`, which must be ≥ 3.9. If your default `python3` is older than 3.9, or `~/.local/bin` is not on your `PATH`, invoke the engine with a specific Python ≥ 3.9 interpreter, for example `python3.11 <skill-dir>/scripts/topicdock.py <subcommand>`.
- TopicDock v1 targets macOS and Linux (the one-line installer is a bash script). On Windows, use the one-line install through WSL (Windows Subsystem for Linux), or install from source.

## Quickstart

A minimal loop: create a store, save a first topic, then mount and trace it.

```bash
# 1) Initialize a store (global = ~/.topicdock/, local = <cwd>/.topicdock/). Safe to repeat.
td init --scope global

# 2) First save creates the topic: base_hop null means "new topic". This one creates C1 and A1.
td save --stdin --json <<'EOF'
{
  "topic_id": "auth-refactor",
  "title": "Stateless JWT for the auth module",
  "base_hop": null,
  "session_id": "ses-01",
  "actor": "opencode/architect",
  "summary": "Settled on stateless JWT; started on the refresh endpoint",
  "next_focus": "Finish the refresh-token rotation logic",
  "mutations": [
    {"op": "create", "track": "C", "statement": "Use stateless JWT with refresh-token rotation",
     "evidence_status": "inferred", "basis": "The gateway has no shared session store"},
    {"op": "create", "track": "A", "state": "~", "statement": "Implement the /refresh endpoint",
     "basis": "Routing is done; rotation logic is still to write"}
  ]
}
EOF
# The JSON result has head_hop H001; its echo field is the anchor line:
# ⚓ TopicDock: auth-refactor (H001@<sha6> 新建 | +1[C] +1[A])

# 3) Later, from any client and any session: mount, then trace
td use auth-refactor
td trace auth-refactor C1
```

If the store does not exist yet, every command except `td init` exits with code 3 (`UNINITIALIZED_STORE`). When you install with the one-line installer, the global store is already created for you, and `td init` is idempotent anyway.

For your next save on this topic, set `base_hop` to `H001` and `base_sha` to the full `base_sha` returned by `td use` or `td save`.

### The `⚓` echo line

The `echo` field returned by `td save` and `td merge` is the authoritative source of the anchor line. It has three formats:

- **New topic:** `⚓ TopicDock: <id> (<head_hop>@<sha6> 新建 | +1[C] +1[R] +1[A])`. The `+n[track]` segments count the items created in each track.
- **Sealed-hop fallback:** `⚓ TopicDock: <id> (<old> 已封板 → 自动新建 <head_hop>@<sha6> | +1[C] +1[A])`. This appears when the previous head hop was sealed and the save automatically started a new hop.
- **Regular / amend / merge:** `⚓ TopicDock: <id> (<head_hop>@<sha6> | …)`. An `--amend` save includes an ` amend` segment. When nothing new was created, the line shows `无新增条目` ("no new items").

*Note: The prompt and documentation surface are fully English in v1 (bilingual trigger words are kept). Engine runtime strings (`status`/`echo`/`hint`) and Markdown protocol literals are intentionally left in v1 Chinese, unchanged.*

## 11 Subcommands & Exit Codes

| Command | Argument signature | What it does |
|---|---|---|
| `td init` | `--scope global\|local [--path <dir>]` | Initialize a store. Idempotent. |
| `td use` | `[<topic_id\|index>] [--trace <auto\|items>]` | Mount a topic. Returns the full ledger and `(head_hop, base_sha)`. With no argument, it guides you based on how many topics exist. Optional `--trace auto` inlines, in the same turn, the full history blocks of all open `[Q]` items and `[~][A]` breakpoints. |
| `td match` | `[--topic <kw> [--topic <kw2> …]] [--threshold 35] [--recent] [--trace <auto\|items>]` | Keyword scoring and recall. Ties are ordered by `updated_at` descending, then `topic_id` ascending. `--recent` skips `--topic` and returns the most recent topics (up to 5). The first 5 hits (`full: true`) carry the mount credentials `head_hop` / full `base_sha` / `artifacts`, so one match completes the mount; from the 6th on they are not attached. |
| `td save` | `--stdin \| --json-file <path> [--amend] [--fork-as <new_id>] [--force-merge]` | The one authoritative write entry, with optimistic concurrency checking. The payload accepts an optional `model` string (omitted on new hops when blank; preserved on `--amend` when omitted). |
| `td merge` | `<src_topic_id> <dst_topic_id> [--summary "..."] [--session-id] [--actor] [--model] [--next-focus]` | Cross-topic merge. Items from `src` are merged into `dst` and trace links stay unbroken. `--actor` and `--model` default to `""`. |
| `td trace` | `<topic_id> <item_id> [<item2> …]` | Pull every historical evidence block of an item. Overturned, folded, and merged items can still be traced. Multiple items in one call (space or comma separated, e.g. `C1,C2,Q3`); single-item output is identical to the previous version. |
| `td list` | `[--page P] [--page-size 10] [--all] [--filter "<kw>"]` | List topics in pages, ordered by `updated_at` descending. |
| `td view` | `<topic_id\|index> [--history]` | Read-only view. `--history` prints the chain of hop summaries (including `model` when present). |
| `td delete` | `<expr> [--dry-run] [--yes]` | Preview by default. `--yes` accepts only an explicit `topic_id` list. |
| `td undo` | `[--force]` | Roll back the most recent entry of a 3-deep undo stack. |
| `td doctor` | `[--rebuild]` | Run the seven-step self-healing inspection. `--rebuild` forces a cold rebuild of the graph index from Markdown. |

**Global flags** can go before or after the subcommand:

- `--json`: indented JSON output.
- `--store-dir <dir>`: use this store directory explicitly.
- `--workspace <dir>`: override the starting point of store resolution (default: the current directory).

**Exit codes `0–7`:**

| Code | Status | Meaning |
|---|---|---|
| 0 | `OK` | Success (including an empty undo stack and an all-`ok` doctor run). |
| 1 | `ERROR` | Unexpected error, reported as `{status, error}` without a traceback. |
| 2 | — (no JSON) | Usage error from argparse, such as a missing required argument or an unknown subcommand. |
| 3 | `UNINITIALIZED_STORE` | No store was found by upward search or in the global location. |
| 4 | `STALE_BASE` | Concurrency check failed because another window moved the topic forward. Never silently applied. |
| 5 | `TOPIC_NOT_FOUND` / `ITEM_NOT_FOUND` | The topic or item does not exist, including a selector that fails to resolve. |
| 6 | `UNSUPPORTED_SCHEMA_VERSION` | The data's `schema_version` is newer than this engine supports (checked before any write or delete). |
| 7 | `INVALID_INPUT` | Input error: invalid or non-JSON payload, `--fork-as` combined with `--amend`/`--force-merge`, an invalid delete expression, or a blocked rollback (`UNDO_BLOCKED`). |

Full JSON input and output schemas and the operation matrix are in [`references/cli-contract.md`](references/cli-contract.md). Cross-store `export / import` is planned for v2 and is not provided in v1.

## Reliability & Safeguards

- **Dual-factor optimistic concurrency control (CAS: compare first, then write) on `(base_hop, base_sha)`.** Concurrent windows, or an in-place `--amend`, never silently overwrite each other. When your base is out of date, `td save` returns `STALE_BASE` (exit code 4) with the current head, the changed items, and `auto_absorbable`:
  - `auto_absorbable: true` means your payload does not touch anything that changed. Resend it with `base_hop` set to `current_head` and `base_sha` set to `current_sha`, and it is absorbed.
  - `auto_absorbable: false` means there is a conflict, and you choose between two options. `--fork-as <new_id>` forks a new topic from the state at your base, with no overwrite risk. `--force-merge` replays on top of the current head, where the last write wins and the response lists the overwritten items.
- **Seven-step startup self-healing (`td doctor`).** Every command starts with an automatic inspection under a cross-platform file lock. Ledger files may be changed only through `td`, so a manual edit is treated as corruption:
  - If the topic can be restored by replaying from `topicdock.db` + `hops/`, it is rewritten automatically. This includes the title and the linked-file snapshot.
  - If it cannot be repaired, or its `topic_id` was altered, the damaged topic is cascade-cleaned (removed together with the hop files that depend on it). Other topics are never blocked by it.
- **Disaster recovery and 100% cold rebuild.**
  - A missing or corrupt `topicdock.db` is rebuilt automatically. You can also force it with `td doctor --rebuild`.
  - A deleted `hop-NNN.md` is skipped as a broken link instead of crashing.
  - When an external linked file moves or goes missing, TopicDock first looks for a file with the same name. If none is found, it adds a warning and drops the dead link on the next save or inspection.
  - Deleting a topic checks the live DAG reference count and physically removes only the hops with `live_refs == 0`.
- **Fixed-depth 3-step rollback (`td undo`).** The engine keeps snapshots of the last 3 `save` / `amend` / `merge` / `delete` operations (`LIMIT 3`). Before restoring, it verifies file hashes. `--force` relaxes only the disk-hash mismatch check. It also always checks that no live child hop still references the hop being removed. If one does, the undo is refused with `INVALID_INPUT` (exit code 7) and an `UNDO_BLOCKED` detail. Item and hop sequence numbers only move forward, so a retracted ID is never reused. If an undo was interrupted, a leftover `.topicdock-undo-pending.json` file makes the next undo fail with `INVALID_INPUT` until you delete that file. One documented corner case: if the database is also lost after an undo and then cold-rebuilt, a retracted number could be reused (see the known boundaries in [`references/markdown-spec.md`](references/markdown-spec.md)).

## FAQ

**Can I edit the Markdown files by hand?**
No. `topics/<id>.md` and `hops/hop-NNN.md` are written only by `td`, and any manual edit counts as file corruption. TopicDock tries to restore the topic by replaying from `topicdock.db` and `hops/`. If that is impossible, or you changed the `topic_id`, the topic is removed. If you did edit something, run `td doctor` right away.

**What happens if two agent windows save to the same topic at the same time?**
Neither overwrites the other. The window with the out-of-date base gets `STALE_BASE` (exit code 4). If its changes do not overlap the newer ones, it can resend on the new base and be absorbed. If they overlap, you choose between forking a new topic (`--fork-as`) and overwriting (`--force-merge`). See [Reliability & Safeguards](#reliability--safeguards).

**Will uninstalling delete my topics?**
No. Uninstall removes only the skill directory, the command templates that carry the sentinel, the injected rule blocks, and the `td` symlink. It never deletes your topic database.

**How do I use TopicDock on Windows?**
TopicDock v1 targets macOS and Linux. On Windows, run the one-line installer inside WSL, or install from source with `python3 scripts/install.py` (Python ≥ 3.9).

**Can I version-control or back up my `.topicdock/` store?**
The store is made of ordinary files, so any backup or sync tool works on it. `topicdock.db` is derived and can be rebuilt from the Markdown with `td doctor --rebuild`. The `topics/` and `hops/` files are the source of truth, so back up the whole store directory. Linked artifacts (the `artifacts` entries) are path references to external documents, such as design or plan files, not copies of their contents inside the store, so a store backup holds the references and not those documents. Keep the rule in mind: only `td` should write to the store, so a merge that mixes files from two copies is, from TopicDock's point of view, a manual edit. Cross-store `export / import` is planned for v2.

## Documentation

- [`SKILL.md`](SKILL.md): the resident rules and the complete workflow contract for the main agent
- [`references/markdown-spec.md`](references/markdown-spec.md): exact syntax of `topics/<id>.md` and `hops/hop-NNN.md`, escaping rules, and cold-rebuild invariants
- [`references/cli-contract.md`](references/cli-contract.md): the 11 subcommands, JSON input/output schemas, exit codes `0–7`, and the `STALE_BASE` absorption protocol
- [`README.zh-CN.md`](README.zh-CN.md): this page in Simplified Chinese
