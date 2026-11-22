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
- **Two-layer Markdown ledger + derived local index.**
  - `topics/<topic-id>.md` is the current-state, single-page panorama. Each line ends with an automatic evolution tag, such as `(H001→H003)`.
  - `hops/hop-NNN.md` stores each hop (one saved increment) with its rich evidence blocks and header snapshot.
  - `topicdock.db` is a derived local SQLite index for fast lookup and undo. If it is lost, it can be rebuilt 100% from the Markdown files.
- **Upward store resolution.** From the current working directory, `td` walks up parent directories looking for the pointer file `.agents/topicdock.json` (a small JSON file that points to a store directory) or a `.topicdock/` directory, checking the pointer file first at each level, and uses the nearest match. If none is found, it falls back to the global store: the path set in `~/.agents/topicdock/config.json` if that file exists, otherwise `~/.topicdock/`. If that does not exist either, it returns `UNINITIALIZED_STORE` (exit code 3) to guide you through creating a store.
- **Anchor credential line (`⚓`).** After every `td save` (or `td merge`), the agent echoes `⚓ TopicDock: <topic-id> (<head_hop>@<first-6-chars-of-base_sha>)` at the end of its reply. While the context is intact, the session needs no repeated database reads. If a long session is compacted (older conversation is trimmed away), the agent re-runs `td use <id>` using this line and recovers the full context.
- **Optimistic concurrency control (`STALE_BASE`).** Concurrent agent windows never silently overwrite each other. When a save's `(base_hop, base_sha)` is out of date, `td save` returns `STALE_BASE` (exit code 4): if your changes do not overlap (`auto_absorbable: true`), resending on `current_head` and `current_sha` absorbs them automatically; on a conflict (`auto_absorbable: false`), you choose between forking a new topic (`--fork-as <new_id>`) and replaying on top of the current head (`--force-merge`).
- **Automatic self-healing & 3-step rollback (`td doctor` / `td undo`).** Every command (except `td init`) checks ledger integrity on startup and repairs replayable corruptions from `topicdock.db` and `hops/` (or forces a 100% cold rebuild with `td doctor --rebuild`), while unrepairable corruptions or altered `topic_id` files are removed so healthy topics are never blocked. `td undo` rolls back the most recent entry of a 3-deep undo stack (`save`, `amend`, `merge`, `delete`) when no newer live hop depends on it, and item/hop numbers never reuse retracted IDs.

## Three-Pillar Workflow (Read / Trace / Write)

The agent follows three habits, which the installer wires in as short resident rules. The complete workflow contract is [`SKILL.md`](SKILL.md).

- **Read (mount a topic).** The agent mounts a topic via `td use`, `td match`, or `/topicdock` when it opens a session's first topic, when you name a topic, or when you switch topics. Later turns in the same session and role talk directly, with zero repeated lookups. After context compaction, the agent recovers the topic from the `⚓` line of its previous message.
- **Trace (go to the evidence).** Before answering questions about an existing `[C]`, `[R]`, `[Q]`, or `[A]` item, the agent runs `td trace <topic_id> <item_id>` to pull the full historical evidence from `hops/hop-NNN.md` instead of guessing from a one-line summary.
- **Write (save at natural convergence points).** Only the top-level primary agent runs `td save`. It saves at the 4 natural convergence points (a conclusion reached or overturned, an open question raised, an action finished or paused at a breakpoint, a topic switch or wrap-up), in the same turn before replying. It uses `--amend` within the same session and `actor`, and creates a new hop across sessions or `actor` switches. The reply then echoes the `⚓` line. Never edit the Markdown files by hand.

```bash
td match --topic jwt --topic refresh-token      # find candidate topics by keyword
td use auth-refactor                            # mount: full ledger + head_hop + full base_sha
td trace auth-refactor C2                       # trace: every evidence block and the fate of C2
td save --stdin --json --amend < payload.json   # same-session increment: update the current hop in place
```

**Global flags** can go before or after the subcommand:

- `--json`: indented JSON output.
- `--store-dir <dir>`: use this store directory explicitly.
- `--workspace <dir>`: override the starting point of store resolution (default: the current directory).

See [`SKILL.md`](SKILL.md) for the complete agent workflow contract, and [`references/cli-contract.md`](references/cli-contract.md) for all 11 subcommands, JSON payload/output schemas, and exit codes `0–7`.

## The /topicdock Command

After installation, type `/topicdock` in your client to mount or switch topics. It is one command, not a command family. `/topicdock` is installed as a command file for `claude` (`~/.claude/commands/topicdock.md`) and `opencode` (`~/.config/opencode/commands/topicdock.md`). On `cursor` (2.4+) and `codex`, the skill itself carries the entry.

- **Direct mount with an argument.** `/topicdock <id|index>` calls `td use <id|index> --trace auto` to mount that topic, which also loads the full history of every open `[Q]` item and `[~]` breakpoint. The agent receives the ledger text and continues with the three-pillar workflow.
- **Guided flow without an argument.** `/topicdock` calls `td use` with no argument, and what you see depends on how many topics the store holds:
  - **0 topics:** the agent asks what you want to work on, then creates the topic.
  - **Exactly 1 topic:** it mounts directly.
  - **2 or more topics:** it shows page 1 as a list, with rows in the format `序号 | 标题 | id | 更新时间` (index | title | id | updated at). Below the list are plain-language prompts that adapt to the list's actual state:
    - Reply with an index to mount that topic.
    - Reply `删 <序号>` ("delete `<index>`") to delete. The agent first shows a dry-run preview, and nothing is deleted until you reply with that topic's exact `topic_id`.
    - Reply "next page" or "all" if there are more pages.
    - Give a keyword to filter the list.

  The list exists only for choosing and deleting.
- **Argument passing.** Any text after the command is passed to the agent. If it looks like `<id|index>`, the agent runs `td use <id|index> --trace auto` directly. Otherwise it falls back to the no-argument guided flow above.

Reinstalling upgrades TopicDock-managed command files, and uninstalling removes them. A custom file of yours with the same name is left untouched: the installer warns and skips it, so it is never silently overwritten.

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
3. Installs the `/topicdock` command file for `claude` and `opencode` (see the table below). `--dest <dir>` installs somewhere other than the default client locations, redirects command files to `<dest>/<client>/commands/`, and skips creating the global store.
4. Creates a symlink at `~/.local/bin/td` pointing to the installed engine. That directory must be on your `PATH`. If it is not writable, the installer prints an `alias` suggestion instead.
5. Unless `--dest` is set, creates or confirms the global store `~/.topicdock/` at the end (idempotent).

`--uninstall` reverses all of this: the skill directory, the TopicDock-managed command files, the rule block, and the symlink. Anything in your memory files outside the rule block is preserved. Uninstalling never deletes your topic database.

Where each client's skill and command file go (the `--client` values match the first column):

| Client | Skill directory | Command file |
|---|---|---|
| `claude` | `~/.claude/skills/topicdock` | `~/.claude/commands/topicdock.md` |
| `opencode` | `~/.config/opencode/skills/topicdock` | `~/.config/opencode/commands/topicdock.md` |
| `cursor` | `~/.cursor/skills/topicdock` | none (the skill carries the entry) |
| `codex` | `<cwd>/.agents/skills/topicdock` | none (the skill carries the entry) |

Notes:

- Installing or uninstalling `cursor` (or `all`) automatically cleans up any older TopicDock-managed `~/.cursor/commands/topicdock.md`. Files of yours with that name are left untouched, and in `--dest` mode the cleanup only touches `<dest>/cursor/commands`.
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

*Note: Engine runtime strings (`status`/`echo`/`hint`) and Markdown ledger protocol literals use fixed Chinese tokens.*

## Local Data & Privacy

All topic ledgers (`topics/<topic-id>.md`, `hops/hop-NNN.md`) and the derived SQLite index (`topicdock.db`) live 100% on your local disk — in `<cwd>/.topicdock/` for a project store, `~/.topicdock/` for the global store, or a custom local path configured via `td init --path`. The `td` engine uses only the Python standard library and makes zero network requests or telemetry uploads.

## FAQ

**Can I edit the Markdown files by hand?**
No. `topics/<id>.md` and `hops/hop-NNN.md` are written only by `td`, and any manual edit counts as file corruption. TopicDock tries to restore the topic by replaying from `topicdock.db` and `hops/`. If that is impossible, or you changed the `topic_id`, the topic is removed. If you did edit something, run `td doctor` right away.

**What happens if two agent windows save to the same topic at the same time?**
Neither overwrites the other. The window with the out-of-date base gets `STALE_BASE` (exit code 4). If its changes do not overlap the newer ones, it can resend on the new base and be absorbed. If they overlap, you choose between forking a new topic (`--fork-as`) and overwriting (`--force-merge`). See [Highlights & Core Concepts](#highlights--core-concepts).

**Will uninstalling delete my topics?**
No. Uninstall removes only the skill directory, the TopicDock-managed command files, the injected rule blocks, and the `td` symlink. It never deletes your topic database.

**How do I use TopicDock on Windows?**
TopicDock v1 targets macOS and Linux. On Windows, run the one-line installer inside WSL, or install from source with `python3 scripts/install.py` (Python ≥ 3.9).

**Can I version-control or back up my `.topicdock/` store?**
The store is made of ordinary files, so any backup or sync tool works on it. `topicdock.db` is derived and can be rebuilt from the Markdown with `td doctor --rebuild`. The `topics/` and `hops/` files are the source of truth, so back up the whole store directory. Linked artifacts (the `artifacts` entries) are path references to external documents, such as design or plan files, not copies of their contents inside the store, so a store backup holds the references and not those documents. Each store is independent, and only `td` should write to a store, so do not manually mix or merge files from two store copies.

## Documentation

- [`SKILL.md`](SKILL.md): the resident rules and the complete workflow contract for the main agent
- [`references/markdown-spec.md`](references/markdown-spec.md): exact syntax of `topics/<id>.md` and `hops/hop-NNN.md`, escaping rules, and cold-rebuild invariants
- [`references/cli-contract.md`](references/cli-contract.md): the 11 subcommands, JSON input/output schemas, exit codes `0–7`, and the `STALE_BASE` absorption protocol
- [`README.zh-CN.md`](README.zh-CN.md): this page in Simplified Chinese
