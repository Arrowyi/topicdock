#!/usr/bin/env python3
"""TopicDock 跨客户端安装脚本（M8）。

将 Skill 交付物（SKILL.md + references/ + 构建产物 scripts/topicdock.py）安装到
各 Agent 客户端的技能目录，并向客户端记忆文件幂等注入常驻唤醒守则块：

- claude   → ~/.claude/skills/topicdock            + <cwd>/CLAUDE.md
- opencode → ~/.config/opencode/skills/topicdock   + <cwd>/AGENTS.md
- cursor   → ~/.cursor/skills/topicdock            + <cwd>/AGENTS.md
- codex    → <cwd>/.agents/skills/topicdock        + <cwd>/AGENTS.md

四个客户端各用独立技能目录；codex 采用官方项目级技能根 <cwd>/.agents/skills。

claude / opencode / cursor 另安装 `/topicdock` 单命令模板
（单一事实源 scripts/templates/topicdock-command.md）；codex 无命令模板
（官方已弃用 custom prompts 机制，可复用提示改由 Agent Skills 承载）：

- claude   → ~/.claude/commands/topicdock.md
- opencode → ~/.config/opencode/commands/topicdock.md
- cursor   → ~/.cursor/commands/topicdock.md

- `--dest <dir>` 同时覆盖该 client 的 skills 根目录（目标仍为 <dest>/topicdock）
  与命令模板根（<dest>/<client>/commands/topicdock.md）；并跳过全局库自动初始化。
- 未传 `--dest` 时，安装末尾以本文件解释器运行本次已装引擎副本执行
  `init --scope global`（幂等）；失败仅打印一行提示，不中断、退出码仍为 0。
- `scripts/topicdock.py` 是构建产物：缺失时先以本文件解释器自动运行 scripts/build.py。
- 守则块以哨兵注释 `<!-- topicdock:begin/end -->` 包裹；重复运行只更新块内内容
  （文件其余内容不动）。
- 命令模板所有权判据 = 目标文件含固定哨兵行 `<!-- topicdock:command-managed -->`：
  含 → 本工具管理（安装覆盖升级、卸载删除）；不含 → 外来同名文件（提示跳过，
  绝不静默覆盖，卸载也不动）。
- `~/.local/bin` 可写时建立 `td` 符号链接指向已安装的 topicdock.py；
  不可写时打印一行 alias 指引。
- `--uninstall` 反向清理：删自身技能目录、删含哨兵的命令模板、解除记忆文件中的
  守则块、删符号链接（数据库永不删除）。守则块归属按记忆文件判定：
  - `<cwd>/CLAUDE.md` 块仅属 claude，claude 卸载时无条件移除；
  - `<cwd>/AGENTS.md` 块由 opencode / cursor / codex 共注，卸载其中任一客户端时，
    移除自身技能目录后探测其余客户端技能目录（opencode `~/.config/opencode/skills/topicdock`、
    cursor `~/.cursor/skills/topicdock`、codex `<cwd>/.agents/skills/topicdock`；claude
    不参与），任一存在则保留块并提示，全部不存在才移除；`--dest` 模式跳过探测直接移除。
- 幂等（重装/更新均为覆盖语义）；退出码 0 成功 / 1 目标不可写 / 2 参数错误。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "SKILL.md"
_REFERENCES_DIR = _REPO_ROOT / "references"
_ENGINE = _REPO_ROOT / "scripts" / "topicdock.py"
_BUILD = _REPO_ROOT / "scripts" / "build.py"
_TEMPLATE = _REPO_ROOT / "scripts" / "templates" / "topicdock-command.md"

CLIENTS = ("claude", "opencode", "cursor", "codex")
_COMMAND_CLIENTS = ("claude", "opencode", "cursor")  # codex 无命令模板
_AGENTS_CLIENTS = ("opencode", "cursor", "codex")  # 守则块注入 <cwd>/AGENTS.md

SENTINEL_BEGIN = "<!-- topicdock:begin -->"
SENTINEL_END = "<!-- topicdock:end -->"
COMMAND_SENTINEL = "<!-- topicdock:command-managed -->"

EXIT_OK = 0
EXIT_UNWRITABLE = 1
EXIT_USAGE = 2

_EXIT_CODE_LINE = (
    "td 退出码: 0=OK / 1=ERROR / 2=用法错误 / 3=UNINITIALIZED_STORE / "
    "4=STALE_BASE / 5=NOT_FOUND / 6=SCHEMA_VERSION / 7=INVALID_INPUT(含 UNDO_BLOCKED)"
)


def _wakeup_rules() -> list[str]:
    """从 SKILL.md 提取 3 行常驻唤醒守则（单一事实源，避免与守则块漂移）。"""
    try:
        text = _SKILL_MD.read_text(encoding="utf-8")
    except OSError:
        return []
    rules = []
    for line in text.splitlines():
        if line.startswith("- **TopicDock 调取（Read）**") or line.startswith(
            "- **TopicDock 追根（Trace）**"
        ) or line.startswith("- **TopicDock 沉淀（Write）**"):
            rules.append(line)
    return rules


def _guideline_block(engine_path: Path) -> str:
    """守则块全文：3 行唤醒守则 + 一行 td 调用提示 + 一行退出码表。"""
    rules = _wakeup_rules()
    if not rules:
        raise SystemExit(f"无法从 {_SKILL_MD} 提取 3 行唤醒守则")
    lines = [
        SENTINEL_BEGIN,
        "## TopicDock 常驻唤醒守则（由 scripts/install.py 自动注入/更新，勿手改本块）",
        *rules,
        (
            f"- **调用**：`python3 {engine_path} <子命令> …`（已建别名 `td`）；"
            "11 子命令与载荷/输出契约见技能目录 references/cli-contract.md。"
        ),
        f"- **退出码**：{_EXIT_CODE_LINE}",
        SENTINEL_END,
    ]
    return "\n".join(lines) + "\n"


def _client_skills_root(client: str, dest: str | None) -> Path:
    """client → skills 根目录；--dest 覆盖（目标仍为 <dest>/topicdock）。"""
    if dest:
        return Path(dest).expanduser()
    if client == "claude":
        return Path.home() / ".claude" / "skills"
    if client == "opencode":
        return Path.home() / ".config" / "opencode" / "skills"
    if client == "cursor":
        return Path.home() / ".cursor" / "skills"
    return Path.cwd() / ".agents" / "skills"  # codex（官方项目级技能根）


def _memory_file(client: str) -> Path:
    """守则块注入的记忆文件：claude → <cwd>/CLAUDE.md；opencode/cursor/codex → <cwd>/AGENTS.md。"""
    return Path.cwd() / ("CLAUDE.md" if client == "claude" else "AGENTS.md")


def _client_commands_root(client: str, dest: str | None) -> Path:
    """client → /topicdock 命令模板根目录；--dest 覆盖为 <dest>/<client>/commands。"""
    if dest:
        return Path(dest).expanduser() / client / "commands"
    if client == "claude":
        return Path.home() / ".claude" / "commands"
    if client == "opencode":
        return Path.home() / ".config" / "opencode" / "commands"
    return Path.home() / ".cursor" / "commands"  # cursor


def _install_command_template(client: str, dest: str | None, notes: list[str]) -> None:
    """安装 /topicdock 命令模板（覆盖升级语义）。

    所有权判据：目标文件含 COMMAND_SENTINEL → 本工具管理，覆盖为最新模板；
    不含 → 外来同名文件，提示跳过，绝不静默覆盖。
    """
    target = _client_commands_root(client, dest) / "topicdock.md"
    try:
        if target.is_file() and COMMAND_SENTINEL not in target.read_text(encoding="utf-8"):
            notes.append(
                f"提示: {target} 已存在且不是 TopicDock 安装的命令模板（缺少"
                f"{COMMAND_SENTINEL} 哨兵），已跳过不覆盖；如需接管请先手动删除该文件。"
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text(target, _TEMPLATE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Unwritable(f"无法写入 {client} 命令模板: {exc}") from exc


def _uninstall_command_template(client: str, dest: str | None) -> None:
    """卸载 /topicdock 命令模板：仅删除含哨兵的文件；外来文件不动。"""
    target = _client_commands_root(client, dest) / "topicdock.md"
    try:
        if target.is_file() and COMMAND_SENTINEL in target.read_text(encoding="utf-8"):
            target.unlink()
    except OSError as exc:
        raise Unwritable(f"卸载 {client} 命令模板失败: {exc}") from exc


def _ensure_engine() -> None:
    """构建产物缺失时以本文件解释器自动运行 scripts/build.py。"""
    if _ENGINE.is_file():
        return
    subprocess.run([sys.executable, str(_BUILD)], check=True)
    if not _ENGINE.is_file():
        raise SystemExit("构建产物 scripts/topicdock.py 生成失败")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _inject_block(md_path: Path, engine_path: Path) -> None:
    """幂等注入/更新守则块；文件其余内容不动。"""
    block = _guideline_block(engine_path)
    text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    begin = text.find(SENTINEL_BEGIN)
    if begin != -1:
        end = text.find(SENTINEL_END, begin)
        if end != -1:
            updated = text[:begin] + block + text[end + len(SENTINEL_END):]
        else:  # 残缺块（有 begin 无 end）：丢弃 begin 起的全部内容再重挂
            updated = text[:begin] + block
    else:
        updated = (text.rstrip("\n") + "\n\n" + block) if text.strip() else block
    _write_text(md_path, updated)


def _remove_block(md_path: Path) -> bool:
    """解除守则块；文件不存在或无块返回 False。"""
    if not md_path.is_file():
        return False
    text = md_path.read_text(encoding="utf-8")
    begin = text.find(SENTINEL_BEGIN)
    if begin == -1:
        return False
    end = text.find(SENTINEL_END, begin)
    if end != -1:
        updated = text[:begin] + text[end + len(SENTINEL_END):]
    else:
        updated = text[:begin]
    _write_text(md_path, updated.rstrip("\n") + "\n" if updated.strip() else "")
    return True


def _has_block(md_path: Path) -> bool:
    """记忆文件是否存在守则块起始哨兵。"""
    if not md_path.is_file():
        return False
    try:
        return SENTINEL_BEGIN in md_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _install_skill_dir(client: str, dest: str | None) -> Path:
    """复制 SKILL.md + references/ + 构建产物到技能目录（覆盖语义）。

    引擎副本显式 chmod 0o755：`td` 符号链接直接指向该副本，需保留执行位。
    """
    root = _client_skills_root(client, dest)
    try:
        target = root / "topicdock"
        target.mkdir(parents=True, exist_ok=True)
        (target / "references").mkdir(exist_ok=True)
        (target / "scripts").mkdir(exist_ok=True)
        _ensure_engine()
        shutil.copyfile(_SKILL_MD, target / "SKILL.md")
        for ref in sorted(_REFERENCES_DIR.glob("*.md")):
            shutil.copyfile(ref, target / "references" / ref.name)
        engine_target = target / "scripts" / "topicdock.py"
        shutil.copyfile(_ENGINE, engine_target)
        os.chmod(engine_target, 0o755)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unwritable(f"无法写入 {client} 技能目录: {exc}") from exc
    return engine_target


def _ensure_td_symlink(engine_targets: list[Path], notes: list[str]) -> None:
    """~/.local/bin 可写时建 `td` 符号链接；否则打印 alias 指引一行。

    只替换「已是指向 topicdock 引擎」的符号链接——用户自有的其他 `td`
    程序/链接不动，仅提示 alias。
    """
    candidates = [target for target in engine_targets if target.is_file()]
    if not candidates:
        return
    target = candidates[0]
    bin_dir = Path.home() / ".local" / "bin"
    link = bin_dir / "td"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            # 仅接管指向 topicdock 引擎的既有链接（替换 = 更新语义）
            try:
                resolves_to_topicdock = any(
                    link.resolve() == candidate.resolve()
                    for candidate in candidates
                )
            except OSError:
                resolves_to_topicdock = False
            if not resolves_to_topicdock:
                notes.append(
                    f"提示: {link} 已存在且不指向 TopicDock，未接管；"
                    f"请手动添加别名: alias td='python3 {target}'"
                )
                return
        elif link.exists():
            notes.append(
                f"提示: {link} 已存在（非符号链接），未接管；"
                f"请手动添加别名: alias td='python3 {target}'"
            )
            return
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target, link)
    except OSError as exc:
        notes.append(f"提示: 无法建立 {link}（{exc}）；请手动添加别名: alias td='python3 {target}'")


def _td_symlink_points_to_topicdock() -> bool:
    link = Path.home() / ".local" / "bin" / "td"
    if not link.is_symlink():
        return False
    try:
        return "topicdock" in str(Path(os.readlink(link)))
    except OSError:
        return False


def _remove_td_symlink(notes: list[str]) -> None:
    link = Path.home() / ".local" / "bin" / "td"
    try:
        if _td_symlink_points_to_topicdock():
            link.unlink()
    except OSError as exc:
        notes.append(f"提示: 无法移除 {link}: {exc}")


class Unwritable(Exception):
    """目标不可写 → 退出码 1。"""


def _init_global_store(engine_path: Path, notes: list[str]) -> None:
    """以本次已装引擎副本执行 `init --scope global`（幂等）。

    成功打印一行确认；失败降级为一行提示，不中断安装（退出码仍为 0）。
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(engine_path), "init", "--scope", "global"],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(
            f"提示: 自动初始化全局库失败（{exc}）；首次使用前请手动运行 "
            f"python3 {engine_path} init --scope global"
        )
        return
    if proc.returncode == 0:
        notes.append(f"已初始化全局库：{Path.home() / '.topicdock'}")
        return
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    suffix = f"（{detail[-1]}）" if detail else ""
    notes.append(
        f"提示: 自动初始化全局库失败{suffix}；首次使用前请手动运行 "
        f"python3 {engine_path} init --scope global"
    )


def install(args: argparse.Namespace) -> tuple[int, list[str]]:
    notes: list[str] = []
    engine_targets: list[Path] = []
    for client in _selected(args.client):
        engine_targets.append(_install_skill_dir(client, args.dest))
        if client in _COMMAND_CLIENTS:
            _install_command_template(client, args.dest, notes)
        try:
            _inject_block(_memory_file(client), engine_targets[-1])
        except OSError as exc:
            raise Unwritable(f"无法写入记忆文件: {exc}") from exc
    _ensure_td_symlink(engine_targets, notes)
    if not args.dest:  # --dest 是自定义/测试落点，不触碰 HOME 全局库
        _init_global_store(engine_targets[0], notes)
    return EXIT_OK, notes


def uninstall(args: argparse.Namespace) -> tuple[int, list[str]]:
    """反向清理：删自身技能目录/命令模板，守则块归属按记忆文件与其余安装探测判定。"""
    notes: list[str] = []
    selected = _selected(args.client)
    try:
        for client in selected:
            target = _client_skills_root(client, args.dest) / "topicdock"
            if target.is_dir():
                shutil.rmtree(target)
            if client in _COMMAND_CLIENTS:
                _uninstall_command_template(client, args.dest)
        if "claude" in selected:
            _remove_block(_memory_file("claude"))  # CLAUDE.md 块仅属 claude
        if any(client in _AGENTS_CLIENTS for client in selected):
            agents_md = Path.cwd() / "AGENTS.md"
            if args.dest:
                _remove_block(agents_md)
            else:
                remaining = [
                    client
                    for client in _AGENTS_CLIENTS
                    if (_client_skills_root(client, None) / "topicdock").is_dir()
                ]
                if remaining:
                    if _has_block(agents_md):
                        notes.append(
                            f"提示: {agents_md} 守则块保留——仍检测到 "
                            f"{' / '.join(remaining)} 的技能目录；如不再使用请卸载对应"
                            "客户端或手动删除该块。"
                        )
                else:
                    _remove_block(agents_md)
    except OSError as exc:
        raise Unwritable(f"卸载失败: {exc}") from exc
    _remove_td_symlink(notes)
    return EXIT_OK, notes


def _selected(client: str) -> list[str]:
    return list(CLIENTS) if client == "all" else [client]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install",
        description="TopicDock 跨客户端 Skill 安装/卸载（claude|opencode|cursor|codex|all）",
    )
    parser.add_argument(
        "--client", required=True, choices=(*CLIENTS, "all"), help="目标客户端"
    )
    parser.add_argument(
        "--dest", default=None,
        help="覆盖 skills 根目录（目标仍为 <dest>/topicdock）与命令模板根（<dest>/<client>/commands）",
    )
    parser.add_argument("--uninstall", action="store_true", help="反向清理")
    args = parser.parse_args(argv)
    try:
        code, notes = uninstall(args) if args.uninstall else install(args)
    except Unwritable as exc:
        sys.stderr.write(f"install: {exc}\n")
        return EXIT_UNWRITABLE
    for note in notes:
        sys.stdout.write(note + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
