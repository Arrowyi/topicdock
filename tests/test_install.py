"""M8/M5 安装测试 —— tests.test_install：四 client 各自独立技能目录（cursor 全局
~/.cursor/skills、codex 项目级 <cwd>/.agents/skills）、哨兵块幂等、按记忆文件定义的
uninstall 归属（AGENTS.md 跨 client 探测 / CLAUDE.md 仅 claude）、引擎执行位、全局库
自动初始化、td 链接、自动 build、/topicdock 命令模板（claude/opencode 两客户端；
cursor 已下线，含哨兵的遗留文件在安装/卸载时自动清理，codex 无）。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.util import TopicDockTestCase

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _REPO_ROOT / "scripts" / "install.py"
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
_SKILL_MD = _REPO_ROOT / "SKILL.md"
_TEMPLATE = _REPO_ROOT / "scripts" / "templates" / "topicdock-command.md"

SENTINEL_BEGIN = "<!-- topicdock:begin -->"
SENTINEL_END = "<!-- topicdock:end -->"
COMMAND_SENTINEL = "<!-- topicdock:command-managed -->"

SKILL_DIR_FILES = (
    "SKILL.md",
    "references/markdown-spec.md",
    "references/cli-contract.md",
    "scripts/topicdock.py",
)


def run_install(*cli_args: str) -> subprocess.CompletedProcess:
    """在隔离 cwd/HOME 下以子进程运行 scripts/install.py（继承环境）。"""
    return subprocess.run(
        [sys.executable, str(_INSTALL), *cli_args],
        capture_output=True, text=True,
    )


class InstallLayoutTestCase(TopicDockTestCase):
    """三 client 目录/文件落位 + 记忆文件注入断言。"""

    def _skill_root(self, client: str) -> Path:
        return {
            "claude": self.home_dir / ".claude" / "skills" / "topicdock",
            "opencode": self.home_dir / ".config" / "opencode" / "skills" / "topicdock",
            # cursor 全局技能目录
            "cursor": self.home_dir / ".cursor" / "skills" / "topicdock",
            # codex 官方项目级技能根
            "codex": self.workspace_dir / ".agents" / "skills" / "topicdock",
        }[client]

    def _memory_file(self, client: str) -> Path:
        return self.workspace_dir / ("CLAUDE.md" if client == "claude" else "AGENTS.md")

    def _command_path(self, client: str) -> Path:
        return {
            "claude": self.home_dir / ".claude" / "commands" / "topicdock.md",
            "opencode": self.home_dir / ".config" / "opencode" / "commands" / "topicdock.md",
            "cursor": self.home_dir / ".cursor" / "commands" / "topicdock.md",
        }[client]

    def _assert_skill_layout(self, client: str) -> None:
        root = self._skill_root(client)
        for rel in SKILL_DIR_FILES:
            self.assertTrue((root / rel).is_file(), f"{client} 缺少 {rel}")
        # 不复制分片与 build.py（只复制构建产物）
        self.assertFalse((root / "scripts" / "layers").exists())
        self.assertFalse((root / "scripts" / "build.py").exists())

    def _assert_block(self, client: str, count: int = 1) -> None:
        text = self._memory_file(client).read_text(encoding="utf-8")
        self.assertEqual(text.count(SENTINEL_BEGIN), count)
        self.assertEqual(text.count(SENTINEL_END), count)
        self.assertIn("## TopicDock Resident Rules (auto-injected by scripts/install.py; do not edit manually)", text)
        self.assertIn("- **TopicDock Read (Mount)**:", text)
        self.assertIn("- **TopicDock Trace (Lineage)**:", text)
        self.assertIn("- **TopicDock Write (Distill)**:", text)
        self.assertIn("td exit codes: 0=OK", text)
        self.assertIn("- **Invoke**: `python3", text)
        self.assertIn("- **Exit codes**: td exit codes:", text)
        self.assertIn("scripts/topicdock.py", text)


class TestInstallLayout(InstallLayoutTestCase):
    def test_install_claude(self):
        proc = run_install("--client", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_skill_layout("claude")
        self._assert_block("claude")

    def test_install_opencode(self):
        proc = run_install("--client", "opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_skill_layout("opencode")
        self._assert_block("opencode")

    def test_install_cursor(self):
        proc = run_install("--client", "cursor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_skill_layout("cursor")
        self._assert_block("cursor")

    def test_install_codex(self):
        proc = run_install("--client", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_skill_layout("codex")
        self._assert_block("codex")
        # codex 无命令模板：命令客户端落位均不得出现
        self.assertFalse(self._command_path("cursor").exists())
        self.assertFalse((self.home_dir / ".claude" / "commands").exists())
        self.assertFalse((self.home_dir / ".config" / "opencode" / "commands").exists())
        # 无 ~/.codex / .codex 写入
        self.assertFalse((self.home_dir / ".codex").exists())
        self.assertFalse((self.workspace_dir / ".codex").exists())
        # 幂等重装
        self.assertEqual(run_install("--client", "codex").returncode, 0)
        self._assert_block("codex", count=1)

    def test_install_all_targets_all_four(self):
        proc = run_install("--client", "all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for client in ("claude", "opencode", "cursor", "codex"):
            self._assert_skill_layout(client)
            self._assert_block(client)
        # 4 个独立技能目录：cursor 全局 ~/.cursor/skills，codex 项目级 <cwd>/.agents/skills
        skill_roots = {self._skill_root(client) for client in ("claude", "opencode", "cursor", "codex")}
        self.assertEqual(len(skill_roots), 4, "四客户端技能目录必须彼此独立")
        for client in ("claude", "opencode", "cursor", "codex"):
            self.assertTrue(self._skill_root(client).is_dir())
        # 2 个命令模板（claude/opencode），codex 无，cursor 已下线不再生成
        for client in ("claude", "opencode"):
            self.assertTrue(self._command_path(client).is_file())
        self.assertFalse(self._command_path("cursor").exists())
        self.assertFalse((self.workspace_dir / ".codex").exists())
        self.assertFalse((self.home_dir / ".codex").exists())
        # opencode/cursor/codex 共注后 <cwd>/AGENTS.md 恰有一个哨兵块
        agents_text = (self.workspace_dir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_text.count(SENTINEL_BEGIN), 1)

    def test_dest_overrides_skills_root(self):
        dest = self.tmp_root / "custom-skills"
        proc = run_install("--client", "cursor", "--dest", str(dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((dest / "topicdock" / "SKILL.md").is_file())
        self.assertFalse((self.workspace_dir / ".agents" / "skills" / "topicdock").exists())

    def test_bad_client_exits_2(self):
        proc = run_install("--client", "vscode")
        self.assertEqual(proc.returncode, 2)

    def test_unwritable_target_exits_1(self):
        blocked = self.home_dir / ".claude"
        blocked.mkdir()
        os.chmod(blocked, 0o555)
        try:
            proc = run_install("--client", "claude")
        finally:
            os.chmod(blocked, 0o755)
        self.assertEqual(proc.returncode, 1, proc.stderr)


class TestSentinelIdempotent(InstallLayoutTestCase):
    def test_second_install_does_not_duplicate(self):
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        self._assert_block("opencode", count=1)

    def test_opencode_cursor_codex_coinject_single_block(self):
        """三客户端顺序安装共注同一 <cwd>/AGENTS.md：哨兵块恰为一个。"""
        for client in ("opencode", "cursor", "codex"):
            self.assertEqual(run_install("--client", client).returncode, 0)
        text = (self.workspace_dir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(SENTINEL_BEGIN), 1)
        self.assertEqual(text.count(SENTINEL_END), 1)

    def test_block_content_update_replaces_in_place(self):
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        memory = self._memory_file("cursor")
        text = memory.read_text(encoding="utf-8")
        # 模拟旧版本块内容：人为改写块内调用提示后再装，块内容被替换回标准形态
        corrupted = text.replace("scripts/topicdock.py", "stale/old-engine.py")
        memory.write_text(corrupted, encoding="utf-8")
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        updated = memory.read_text(encoding="utf-8")
        self.assertEqual(updated.count(SENTINEL_BEGIN), 1)
        self.assertNotIn("stale/old-engine.py", updated)
        self.assertIn("- **TopicDock Read (Mount)**", updated)


class TestWakeupRulesFailFast(InstallLayoutTestCase):
    """守则提取失败必须显式报错（SKILL.md 不可读 / 锚点不足 3 行），绝不静默注入零守则块。"""

    def _fresh_install_module(self):
        spec = importlib.util.spec_from_file_location(
            f"topicdock_install_under_test_{id(self)}", _INSTALL
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_unreadable_skill_md_raises(self):
        mod = self._fresh_install_module()
        mod._SKILL_MD = self.tmp_root / "missing" / "SKILL.md"
        with self.assertRaises(SystemExit):
            mod._wakeup_rules()

    def test_insufficient_anchors_abort_install_without_block(self):
        fake = self.tmp_root / "fake-skill" / "SKILL.md"
        fake.parent.mkdir(parents=True)
        fake.write_text(
            "# Fake Skill\n\n- **TopicDock Read (Mount)**: only one anchor here\n",
            encoding="utf-8",
        )
        mod = self._fresh_install_module()
        mod._SKILL_MD = fake
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--client", "cursor"])
        # 必须是守则提取失败（锚点计数实证），而非其他路径的退出
        self.assertIn("found 1", str(ctx.exception))
        self.assertIn("resident wakeup rules", str(ctx.exception))
        # 钉死：SKILL.md 不可读时 AGENTS.md 不得被创建/注入守则块
        agents_md = self.workspace_dir / "AGENTS.md"
        self.assertFalse(
            agents_md.exists() and SENTINEL_BEGIN in agents_md.read_text(encoding="utf-8"),
            "守则提取失败时不得静默注入零守则块",
        )


class TestUninstall(InstallLayoutTestCase):
    def _seed_memory(self, client: str) -> str:
        memory = self._memory_file(client)
        memory.write_text("# 项目说明\n\n自有内容，安装与卸载均不得改动本段。\n", encoding="utf-8")
        return memory.read_text(encoding="utf-8")

    def test_uninstall_reverses_all(self):
        for client in ("claude", "opencode", "cursor", "codex"):
            self.assertEqual(run_install("--client", client).returncode, 0)
        self.assertTrue((self.home_dir / ".local" / "bin" / "td").is_symlink())
        for client in ("claude", "opencode", "cursor", "codex"):
            self.assertEqual(run_install("--client", client, "--uninstall").returncode, 0)
        for client in ("claude", "opencode", "cursor", "codex"):
            self.assertFalse(self._skill_root(client).exists())
            text = self._memory_file(client).read_text(encoding="utf-8")
            self.assertNotIn(SENTINEL_BEGIN, text)
            self.assertNotIn(SENTINEL_END, text)
        self.assertFalse((self.home_dir / ".local" / "bin" / "td").exists())

    def test_uninstall_keeps_other_memory_content(self):
        original = self._seed_memory("opencode")
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        self.assertEqual(run_install("--client", "opencode", "--uninstall").returncode, 0)
        text = self._memory_file("opencode").read_text(encoding="utf-8")
        self.assertIn("# 项目说明", text)
        self.assertIn("自有内容，安装与卸载均不得改动本段。", text)
        self.assertNotIn(SENTINEL_BEGIN, text)

    def test_uninstall_without_install_is_noop(self):
        proc = run_install("--client", "cursor", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "no-op 卸载不得输出共享产物提示")

    def test_uninstall_codex_removes_own_products_keeps_cursor(self):
        """卸 codex：删自己的 <cwd>/.agents/skills 技能目录；cursor 全局技能目录保留
        （命令模板已下线不再生成）；AGENTS.md 因 cursor 仍装而保留块并提示。"""
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        self.assertEqual(run_install("--client", "codex").returncode, 0)
        proc = run_install("--client", "codex", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._skill_root("codex").exists())
        self.assertTrue(self._skill_root("cursor").is_dir(), "codex 卸载不得移除 cursor 技能目录")
        self.assertFalse(self._command_path("cursor").exists(), "cursor 命令模板已下线，安装不再生成")
        text = self._memory_file("codex").read_text(encoding="utf-8")
        self.assertIn(SENTINEL_BEGIN, text, "cursor 仍装时 AGENTS.md 守则块应保留")
        self.assertIn("保留", proc.stdout)
        self.assertIn("cursor", proc.stdout)

    def test_uninstall_cursor_removes_own_products_and_command(self):
        """卸 cursor：删自己的 ~/.cursor/skills 与预置的哨兵遗留命令文件（静默清理）；
        codex 仍在，AGENTS.md 守则块保留并提示。"""
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        self.assertEqual(run_install("--client", "codex").returncode, 0)
        legacy = self._command_path("cursor")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(f"{COMMAND_SENTINEL}\n# 遗留命令模板\n", encoding="utf-8")
        proc = run_install("--client", "cursor", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._skill_root("cursor").exists())
        self.assertFalse(legacy.exists(), "cursor 卸载应清理预置的哨兵遗留命令文件")
        self.assertTrue(self._skill_root("codex").is_dir(), "cursor 卸载不得移除 codex 技能目录")
        text = self._memory_file("cursor").read_text(encoding="utf-8")
        self.assertIn(SENTINEL_BEGIN, text, "codex 仍装时 AGENTS.md 守则块应保留")
        self.assertIn("保留", proc.stdout)
        self.assertIn("codex", proc.stdout)


class TestTdSymlink(InstallLayoutTestCase):
    def test_symlink_points_to_installed_engine(self):
        self.assertEqual(run_install("--client", "claude").returncode, 0)
        link = self.home_dir / ".local" / "bin" / "td"
        self.assertTrue(link.is_symlink())
        target = Path(os.readlink(link))
        self.assertEqual(
            self.home_dir / ".claude" / "skills" / "topicdock" / "scripts" / "topicdock.py",
            target if target.is_absolute() else link.parent / target,
        )

    def test_unwritable_local_bin_prints_alias_hint(self):
        bin_dir = self.home_dir / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        os.chmod(bin_dir, 0o555)
        try:
            proc = run_install("--client", "claude")
        finally:
            os.chmod(bin_dir, 0o755)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("alias td=", proc.stdout)


class TestAutoBuild(InstallLayoutTestCase):
    def test_missing_artifact_is_rebuilt_by_install(self):
        engine = _REPO_ROOT / "scripts" / "topicdock.py"
        existed = engine.is_file()
        try:
            if existed:
                engine.unlink()
            self.assertFalse(engine.is_file())
            proc = run_install("--client", "cursor")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(engine.is_file(), "install 应自动重建构建产物")
            self.assertGreater(engine.stat().st_size, 0)
        finally:
            if not engine.is_file():
                subprocess.run(
                    [sys.executable, str(_REPO_ROOT / "scripts" / "build.py")],
                    check=True, capture_output=True,
                )


class TestCommandTemplate(InstallLayoutTestCase):
    """/topicdock 命令模板：两客户端（claude/opencode）落位、幂等、卸载对称、接管升级、
    外来不动、--dest 重定向；cursor 遗留哨兵文件在安装/卸载时自动清理。"""

    TEMPLATE_TEXT = _TEMPLATE.read_text(encoding="utf-8")

    def _assert_command_template(self, client: str) -> None:
        path = self._command_path(client)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, self.TEMPLATE_TEXT, f"{client} 命令模板内容应与单一事实源一致")
        # 哨兵位于 frontmatter 之后第一行
        lines = text.splitlines()
        closing = lines.index("---", 1)  # frontmatter 结束行
        self.assertEqual(lines[closing + 1], COMMAND_SENTINEL)
        # 关键契约元素：$ARGUMENTS 参数判断、无参引导、列表列序、操作提示
        self.assertIn("$ARGUMENTS", text)
        self.assertIn("td use", text)
        self.assertIn("序号 | 标题 | id | 更新时间", text)
        self.assertIn("删 <序号>", text)
        # 合并零提及
        self.assertNotIn("merge", text)
        self.assertNotIn("合并", text)

    def test_command_template_installed_claude_opencode(self):
        proc = run_install("--client", "all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for client in ("claude", "opencode"):
            self._assert_command_template(client)
        # cursor 命令模板已下线：全量安装不得生成
        self.assertFalse(self._command_path("cursor").exists())

    def test_command_reinstall_is_idempotent_overwrite(self):
        self.assertEqual(run_install("--client", "claude").returncode, 0)
        path = self._command_path("claude")
        corrupted = path.read_text(encoding="utf-8").replace("td use", "stale/old-flow")
        path.write_text(corrupted, encoding="utf-8")
        self.assertEqual(run_install("--client", "claude").returncode, 0)
        self._assert_command_template("claude")  # 内容恢复为最新模板（含哨兵恰一处）

    def test_command_uninstall_symmetric(self):
        self.assertEqual(run_install("--client", "all").returncode, 0)
        self.assertEqual(run_install("--client", "all", "--uninstall").returncode, 0)
        for client in ("claude", "opencode"):
            self.assertFalse(self._command_path(client).exists())

    def test_uninstall_without_install_is_noop_for_commands(self):
        proc = run_install("--client", "claude", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._command_path("claude").exists())

    def test_foreign_command_file_left_untouched_silently(self):
        """cursor 安装不再管理命令文件：外来同名文件静默保留（无任何提示），卸载同样不动。"""
        path = self._command_path("cursor")
        path.parent.mkdir(parents=True)
        foreign = "# 我的自定义 /topicdock 命令\n\n自有内容，不应被覆盖。\n"
        path.write_text(foreign, encoding="utf-8")
        proc = run_install("--client", "cursor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), foreign, "外来同名文件绝不能被静默覆盖")
        self.assertNotIn("跳过", proc.stdout)
        self.assertNotIn(str(path), proc.stdout)
        # 卸载也不动外来文件
        self.assertEqual(run_install("--client", "cursor", "--uninstall").returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), foreign)

    def test_sentinel_file_takeover_upgrades_to_latest_template(self):
        path = self._command_path("opencode")
        path.parent.mkdir(parents=True)
        stale = (
            "---\ndescription: 旧版模板\n---\n\n"
            f"{COMMAND_SENTINEL}\n\n# 旧版 /topicdock\n\nstale/old-flow\n"
        )
        path.write_text(stale, encoding="utf-8")
        proc = run_install("--client", "opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_command_template("opencode")

    def test_dest_redirects_command_root_and_writes_nothing_to_real_locations(self):
        dest = self.tmp_root / "custom-root"
        proc = run_install("--client", "all", "--dest", str(dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for client in ("claude", "opencode"):
            redirected = dest / client / "commands" / "topicdock.md"
            self.assertTrue(redirected.is_file(), f"{client} 命令模板应落在 <dest>/<client>/commands/")
            self.assertEqual(
                redirected.read_text(encoding="utf-8"), self.TEMPLATE_TEXT,
            )
        # 隔离 HOME 的默认命令目录与隔离 cwd 的 cursor 命令目录均不得写入
        self.assertFalse((self.home_dir / ".claude" / "commands").exists())
        self.assertFalse((self.home_dir / ".config" / "opencode" / "commands").exists())
        self.assertFalse((self.home_dir / ".cursor" / "commands").exists())


class TestEngineExecutable(InstallLayoutTestCase):
    def test_installed_engine_copy_is_executable(self):
        """技能目录内引擎副本必须保留执行位（否则 ~/.local/bin/td 会 permission denied）。"""
        proc = run_install("--client", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        engine = self._skill_root("claude") / "scripts" / "topicdock.py"
        self.assertTrue(os.access(engine, os.X_OK), f"{engine} 应保留执行位")


class TestGlobalStoreInit(InstallLayoutTestCase):
    """无 --dest 安装后自动 td init --scope global；--dest 跳过；卸载不删库。"""

    def _meta_path(self) -> Path:
        return self.home_dir / ".topicdock" / ".topicdock-meta.json"

    def test_install_initializes_global_store(self):
        proc = run_install("--client", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._meta_path().is_file(), "无 --dest 安装后应自动建全局库")
        self.assertIn("已初始化全局库", proc.stdout)

    def test_dest_install_skips_global_store(self):
        dest = self.tmp_root / "custom-skills"
        proc = run_install("--client", "codex", "--dest", str(dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((dest / "topicdock" / "SKILL.md").is_file())
        self.assertFalse((self.home_dir / ".topicdock").exists(), "--dest 模式不得自动建库")

    def test_uninstall_keeps_global_store(self):
        self.assertEqual(run_install("--client", "codex").returncode, 0)
        self.assertTrue(self._meta_path().is_file())
        proc = run_install("--client", "codex", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._meta_path().is_file(), "uninstall 绝不删除数据库")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "以 root 运行时权限位无效")
    def test_init_failure_degrades_without_failing_install(self):
        """codex 写 cwd（不碰 HOME）成功、HOME 只读使 init 失败：安装仍 exit 0 且打印提示。"""
        os.chmod(self.home_dir, 0o555)
        try:
            proc = run_install("--client", "codex")
        finally:
            os.chmod(self.home_dir, 0o755)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_skill_layout("codex")
        self.assertIn(SENTINEL_BEGIN, self._memory_file("codex").read_text(encoding="utf-8"))
        self.assertIn("自动初始化全局库失败", proc.stdout)
        self.assertIn("init --scope global", proc.stdout)


class TestUninstallMemoryMatrix(InstallLayoutTestCase):
    """卸载归属按记忆文件判定：AGENTS.md 块跨 client 探测，CLAUDE.md 块仅 claude。"""

    def test_opencode_uninstall_keeps_block_while_cursor_installed(self):
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        proc = run_install("--client", "opencode", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._skill_root("opencode").exists())
        self.assertTrue(self._skill_root("cursor").is_dir())
        text = self._memory_file("opencode").read_text(encoding="utf-8")
        self.assertIn(SENTINEL_BEGIN, text, "仍有客户端安装时 AGENTS.md 块应保留")
        self.assertIn("保留", proc.stdout)
        self.assertIn("cursor", proc.stdout)

    def test_last_agents_client_uninstall_removes_block(self):
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        self.assertEqual(run_install("--client", "codex").returncode, 0)
        self.assertEqual(run_install("--client", "opencode", "--uninstall").returncode, 0)
        self.assertIn(
            SENTINEL_BEGIN, self._memory_file("opencode").read_text(encoding="utf-8")
        )
        proc = run_install("--client", "codex", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("codex").read_text(encoding="utf-8"),
            "最后一个 AGENTS.md 客户端卸载后守则块应移除",
        )

    def test_claude_uninstall_removes_claude_block_only(self):
        self.assertEqual(run_install("--client", "claude").returncode, 0)
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        proc = run_install("--client", "claude", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._skill_root("claude").exists())
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("claude").read_text(encoding="utf-8"),
            "CLAUDE.md 块仅属 claude，卸载时无条件移除",
        )
        self.assertIn(
            SENTINEL_BEGIN, self._memory_file("opencode").read_text(encoding="utf-8"),
            "claude 卸载不得触碰 AGENTS.md",
        )

    def test_all_uninstall_removes_blocks(self):
        self.assertEqual(run_install("--client", "all").returncode, 0)
        proc = run_install("--client", "all", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("opencode").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("claude").read_text(encoding="utf-8")
        )
        for client in ("claude", "opencode", "cursor", "codex"):
            self.assertFalse(self._skill_root(client).exists())

    def test_dest_uninstall_skips_probe_and_removes_block(self):
        dest = self.tmp_root / "custom-skills"
        self.assertEqual(
            run_install("--client", "cursor", "--dest", str(dest)).returncode, 0
        )
        # 预置另一客户端全局技能目录：--dest 模式跳过探测，仍应移除块
        other = self.home_dir / ".config" / "opencode" / "skills" / "topicdock"
        other.mkdir(parents=True)
        proc = run_install("--client", "cursor", "--dest", str(dest), "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(other.is_dir())
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("cursor").read_text(encoding="utf-8"),
            "--dest 模式应跳过探测直接移除守则块",
        )


class TestLegacyAgentSkillsDir(InstallLayoutTestCase):
    """旧版 cursor 产物 <cwd>/.agents/skills/topicdock：不迁移不删除，探测按 codex 安装处置。"""

    def _seed_legacy_dir(self) -> Path:
        legacy = self.workspace_dir / ".agents" / "skills" / "topicdock"
        (legacy / "scripts").mkdir(parents=True)
        (legacy / "scripts" / "topicdock.py").write_text("# legacy engine\n", encoding="utf-8")
        return legacy

    def test_opencode_uninstall_treats_legacy_dir_as_codex(self):
        legacy = self._seed_legacy_dir()
        self.assertEqual(run_install("--client", "opencode").returncode, 0)
        proc = run_install("--client", "opencode", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(legacy.is_dir(), "旧版产物不得被自动删除")
        self.assertIn(
            SENTINEL_BEGIN, self._memory_file("opencode").read_text(encoding="utf-8"),
            "探测到 <cwd>/.agents/skills/topicdock 时应按 codex 仍装保留守则块",
        )
        self.assertIn("保留", proc.stdout)
        self.assertIn("codex", proc.stdout)

    def test_cursor_uninstall_treats_legacy_dir_as_codex(self):
        legacy = self._seed_legacy_dir()
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        proc = run_install("--client", "cursor", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(legacy.is_dir(), "旧版产物不得被自动删除")
        self.assertIn(
            SENTINEL_BEGIN, self._memory_file("cursor").read_text(encoding="utf-8"),
            "探测到 <cwd>/.agents/skills/topicdock 时应按 codex 仍装保留守则块",
        )
        self.assertIn("保留", proc.stdout)
        self.assertIn("codex", proc.stdout)


class TestInstallShell(TopicDockTestCase):
    """scripts/install.sh 一句话安装：bash 子进程 + 隔离 HOME/cwd/PATH。

    TOPICDOCK_SRC_DIR 指向仓库根，跳过下载分支（git clone / tarball / 临时清理
    属已声明未测项）。假体 python3 的 --version 固定输出 3.6.0、其余调用退出 1。
    """

    def _run(self, *args, path=None, extra_env=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        env["TOPICDOCK_SRC_DIR"] = str(_REPO_ROOT)
        if path is not None:
            env["PATH"] = path
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(_INSTALL_SH), *args],
            capture_output=True, text=True,
            cwd=str(self.workspace_dir), env=env,
        )

    def _cursor_skill(self) -> Path:
        return self.home_dir / ".cursor" / "skills" / "topicdock"

    def _write_fake_python3(self, directory: Path, log_path: Path | None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        fake = directory / "python3"
        log_line = (
            f'printf "%s\\n" "$*" >> "{log_path}"\n' if log_path is not None else ""
        )
        fake.write_text(
            "#!/bin/sh\n"
            + log_line
            + 'if [ "$1" = "--version" ]; then echo "Python 3.6.0"; exit 0; fi\n'
            + "exit 1\n",
            encoding="utf-8",
        )
        os.chmod(fake, 0o755)
        return fake

    def test_client_cursor_fresh_install(self):
        proc = self._run("--client", "cursor")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        skill = self._cursor_skill() / "SKILL.md"
        command = self.home_dir / ".cursor" / "commands" / "topicdock.md"
        engine = self._cursor_skill() / "scripts" / "topicdock.py"
        self.assertTrue(skill.is_file(), "cursor 技能应落位 ~/.cursor/skills/topicdock/SKILL.md")
        self.assertFalse(command.exists(), "cursor 命令模板已下线：新装不得生成 ~/.cursor/commands/topicdock.md")
        self.assertTrue(os.access(engine, os.X_OK), "已装引擎副本应保留执行位")
        self.assertTrue(
            (self.home_dir / ".topicdock" / ".topicdock-meta.json").is_file(),
            "安装应自动建立全局库",
        )
        self.assertIn("自检通过", proc.stdout)

    def test_no_arg_without_install_exits_2_with_hint(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("未检测到已安装的 TopicDock 客户端", combined)
        self.assertIn("--client", combined)

    def test_no_arg_upgrades_installed_client(self):
        self.assertEqual(self._run("--client", "cursor").returncode, 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("cursor", proc.stdout)
        self.assertTrue((self._cursor_skill() / "SKILL.md").is_file())

    def test_fake_python3_falls_back_to_modern_interpreter(self):
        fallback_name = None
        real_python = None
        for name in ("python3.13", "python3.12", "python3.11"):
            found = shutil.which(name)
            if found:
                fallback_name, real_python = name, found
                break
        if real_python is None or fallback_name is None:
            self.skipTest("本机无 python3.13/3.12/3.11，跳过 fallback 用例")

        fake_dir = self.tmp_root / "fakebin"
        fake_log = self.tmp_root / "fake-python.log"
        self._write_fake_python3(fake_dir, fake_log)

        # 使 fallback 解释器可被正确定位，同时记录其调用以断言验证步骤用的是它。
        wrapper_dir = self.tmp_root / "wrapbin"
        wrapper_dir.mkdir()
        fallback_log = self.tmp_root / "fallback-python.log"
        wrapper = wrapper_dir / fallback_name
        wrapper.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{fallback_log}"\n'
            f'exec "{real_python}" "$@"\n',
            encoding="utf-8",
        )
        os.chmod(wrapper, 0o755)

        path = os.pathsep.join(
            [str(fake_dir), str(wrapper_dir), os.environ.get("PATH", "")]
        )
        proc = self._run("--client", "cursor", path=path)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue((self._cursor_skill() / "SKILL.md").is_file())

        fake_calls = fake_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(fake_calls, "假体 python3 应至少被探测调用一次")
        self.assertTrue(
            all(call.strip() == "--version" for call in fake_calls),
            f"假体 python3 只应被用于 --version 探测: {fake_calls}",
        )
        fallback_calls = fallback_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            any("doctor" in call for call in fallback_calls),
            f"验证步骤应由 fallback 解释器执行: {fallback_calls}",
        )

    def test_no_usable_python_exits_1_with_guidance(self):
        fake_dir = self.tmp_root / "fakebin-nopy"
        self._write_fake_python3(fake_dir, None)
        path = os.pathsep.join([str(fake_dir), "/usr/bin", "/bin"])
        proc = self._run("--client", "cursor", path=path)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("请先安装 Python ≥ 3.9", proc.stderr)
        self.assertIn("brew install python", proc.stderr)

    def test_invalid_client_value_fails(self):
        proc = self._run("--client", "vscode")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestLegacyCursorCommandCleanup(InstallLayoutTestCase):
    """cursor 命令模板下线后的存量自愈：安装/卸载 cursor（或 all）自动清理含哨兵的
    遗留命令文件——安装侧命中打印提示、卸载侧静默；外来（不含哨兵）与不可读
    （非 UTF-8）文件一律不动；安装其他单客户端不触发清理。"""

    def _legacy_path(self) -> Path:
        return self._command_path("cursor")

    def test_install_cursor_removes_sentinel_legacy_file_with_hint(self):
        path = self._legacy_path()
        path.parent.mkdir(parents=True)
        path.write_text(f"{COMMAND_SENTINEL}\n# 旧版 /topicdock\n", encoding="utf-8")
        proc = run_install("--client", "cursor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(path.exists(), "安装 cursor 应自动清理含哨兵的遗留命令文件")
        self.assertIn(str(path), proc.stdout, "清理命中应打印一行含路径的提示")
        self.assertIn("清理", proc.stdout)

    def test_install_cursor_keeps_non_utf8_file_with_degrade_hint(self):
        path = self._legacy_path()
        path.parent.mkdir(parents=True)
        raw = b"\xff\xfe\x00binary-not-utf8"
        path.write_bytes(raw)
        proc = run_install("--client", "cursor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_bytes(), raw, "不可读（非 UTF-8）文件绝不能被动")
        self.assertIn("保留原样", proc.stdout, "读取失败应打印降级提示且不中断安装")

    def test_uninstall_non_utf8_file_survives_and_converges(self):
        """先装 cursor 再预置非 UTF-8 命令文件：卸载不中断（退出码 0）、文件原样保留、
        技能目录已删、守则块被移除（cursor 为唯一守则注入方时），且再次卸载同样
        退出码 0（重跑收敛钉）。"""
        self.assertEqual(run_install("--client", "cursor").returncode, 0)
        path = self._legacy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b"\xff\xfe\x00binary-not-utf8"
        path.write_bytes(raw)
        proc = run_install("--client", "cursor", "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_bytes(), raw, "卸载不得动不可读（非 UTF-8）文件")
        self.assertFalse(self._skill_root("cursor").exists(), "cursor 技能目录应被删除")
        self.assertNotIn(
            SENTINEL_BEGIN, self._memory_file("cursor").read_text(encoding="utf-8"),
            "cursor 为唯一 AGENTS.md 守则注入方时，卸载应移除守则块",
        )
        # 重跑收敛钉：重复卸载仍成功且文件仍原样
        self.assertEqual(run_install("--client", "cursor", "--uninstall").returncode, 0)
        self.assertEqual(path.read_bytes(), raw)

    def test_install_codex_does_not_touch_cursor_legacy_file(self):
        """README 口径钉子：仅 install/uninstall cursor 或 all 触发清理。"""
        path = self._legacy_path()
        path.parent.mkdir(parents=True)
        legacy = f"{COMMAND_SENTINEL}\n# 旧版 /topicdock\n"
        path.write_text(legacy, encoding="utf-8")
        proc = run_install("--client", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), legacy, "安装其他单客户端不得触发清理")


if __name__ == "__main__":
    unittest.main()
