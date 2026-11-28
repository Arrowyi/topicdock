"""M1 测试 —— L1 寻址/锁/原子写 + `td init` + L6 CLI 骨架（tests.test_codec_and_rebuild）。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

from tests.fixtures.match_corpus import materialize_store
from tests.util import (
    TopicDockTestCase,
    build_big_corpus,
    build_merged_store,
    build_rich_store,
    dump_tables,
    make_store,
    run_cli,
    run_cli_json,
    run_save,
)

import topicdock
from topicdock import (
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_UNINITIALIZED_STORE,
    FileLock,
    StoreContext,
    atomic_write_text,
    ensure_store_healthy,
    file_sha256,
    get_or_create_db,
    rebuild_db_from_markdown,
    resolve_store,
    sha256_text,
)

STORE_GITIGNORE = (
    "topicdock.db*\n.topicdock.lock\n*.tmp\n"
    ".topicdock-undo-pending.json\n.topicdock-amend-pending.json\n"
    ".topicdock-published.json\n"
)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
DEFAULT_META = {"schema_version": 1, "max_hop_seq": 0}


# ---------- 1. resolve_store ----------

class TestResolveStore(TopicDockTestCase):
    def test_explicit_dir_passthrough(self):
        target = self.tmp_root / "explicit-store"
        ctx = resolve_store(None, str(target))
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.root, target.resolve())

    def test_explicit_dir_expanduser_uses_test_home(self):
        ctx = resolve_store(None, "~")
        self.assertEqual(ctx.root, self.home_dir.resolve())

    def test_parent_store_hit_and_stop(self):
        parent_store = self.tmp_root / "parent" / ".topicdock"
        (parent_store / "topics").mkdir(parents=True)
        os.chdir(self.tmp_root / "parent")
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, parent_store.resolve())

    def test_near_store_wins_over_far_store(self):
        near_store = self.workspace_dir / ".topicdock"
        (near_store / "topics").mkdir(parents=True)
        far_store = self.tmp_root / ".topicdock"
        (far_store / "topics").mkdir(parents=True)
        deep = self.workspace_dir / "deep"
        deep.mkdir()
        os.chdir(deep)
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, near_store.resolve())

    def test_pointer_wins_over_store_dir_same_level(self):
        target = self.tmp_root / "pointer-target"
        target.mkdir()
        (self.workspace_dir / ".topicdock" / "topics").mkdir(parents=True)
        agents_dir = self.workspace_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "topicdock.json").write_text(
            json.dumps({"store_dir": str(target)}), encoding="utf-8"
        )
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, target.resolve())

    def test_near_pointer_wins_over_far_store(self):
        target = self.tmp_root / "pointer-target-2"
        target.mkdir()
        far_store = self.tmp_root / ".topicdock"
        (far_store / "topics").mkdir(parents=True)
        deep = self.workspace_dir / "deep"
        deep.mkdir()
        os.chdir(deep)
        agents_dir = self.workspace_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "topicdock.json").write_text(
            json.dumps({"store_dir": str(target)}), encoding="utf-8"
        )
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, target.resolve())

    def test_broken_pointer_ignored_falls_to_store_dir(self):
        (self.workspace_dir / ".topicdock" / "topics").mkdir(parents=True)
        agents_dir = self.workspace_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "topicdock.json").write_text("{not json", encoding="utf-8")
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, (self.workspace_dir / ".topicdock").resolve())

    def test_pointer_without_store_dir_key_ignored(self):
        (self.workspace_dir / ".topicdock" / "topics").mkdir(parents=True)
        agents_dir = self.workspace_dir / ".agents"
        agents_dir.mkdir()
        (agents_dir / "topicdock.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, (self.workspace_dir / ".topicdock").resolve())

    def test_start_dir_param(self):
        base = self.tmp_root / "somewhere"
        store = base / ".topicdock"
        (store / "topics").mkdir(parents=True)
        ctx = resolve_store(base / "sub", None)
        self.assertEqual(ctx.root, store.resolve())

    def test_global_fallback_home_store(self):
        home_store = self.home_dir / ".topicdock"
        (home_store / "topics").mkdir(parents=True)
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, home_store.resolve())

    def test_global_fallback_home_pointer(self):
        target = self.tmp_root / "global-pointer-store"
        target.mkdir()
        pointer_dir = self.home_dir / ".agents" / "topicdock"
        pointer_dir.mkdir(parents=True)
        (pointer_dir / "config.json").write_text(
            json.dumps({"store_dir": str(target)}), encoding="utf-8"
        )
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, target.resolve())

    def test_none_when_nothing_found(self):
        self.assertIsNone(resolve_store(None, None))


# ---------- 2. td init ----------

class TestInit(TopicDockTestCase):
    def _assert_store_layout(self, store: Path) -> None:
        self.assertTrue((store / "topics").is_dir())
        self.assertTrue((store / "hops").is_dir())
        self.assertEqual(
            (store / ".gitignore").read_text(encoding="utf-8"), STORE_GITIGNORE
        )
        meta = json.loads((store / ".topicdock-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta, DEFAULT_META)
        self.assertFalse((store / "topicdock.db").exists())

    def test_init_local(self):
        code, data = run_cli_json(["init", "--scope", "local", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["scope"], "local")
        self.assertTrue(data["created"])
        expected_store = (self.workspace_dir / ".topicdock").resolve()
        self.assertEqual(data["store_dir"], str(expected_store))
        self._assert_store_layout(expected_store)
        self.assertFalse((self.workspace_dir / ".agents").exists())
        self.assertFalse((self.home_dir / ".agents").exists())

    def test_init_global(self):
        code, data = run_cli_json(["init", "--scope", "global", "--json"])
        self.assertEqual(code, 0)
        expected_store = (self.home_dir / ".topicdock").resolve()
        self.assertEqual(data["store_dir"], str(expected_store))
        self._assert_store_layout(expected_store)
        self.assertFalse((self.home_dir / ".agents").exists())
        self.assertFalse((self.workspace_dir / ".agents").exists())

    def test_init_path_local_writes_pointer(self):
        target = self.tmp_root / "custom-store"
        code, data = run_cli_json(
            ["init", "--scope", "local", "--path", str(target), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["store_dir"], str(target.resolve()))
        pointer = self.workspace_dir / ".agents" / "topicdock.json"
        self.assertTrue(pointer.is_file())
        self.assertEqual(
            json.loads(pointer.read_text(encoding="utf-8")),
            {"store_dir": str(target.resolve())},
        )
        self.assertFalse((self.home_dir / ".agents").exists())
        self._assert_store_layout(target)
        ctx = resolve_store(None, None)
        self.assertEqual(ctx.root, target.resolve())

    def test_init_path_global_writes_pointer(self):
        target = self.tmp_root / "custom-global-store"
        code, data = run_cli_json(
            ["init", "--scope", "global", "--path", str(target), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["store_dir"], str(target.resolve()))
        pointer = self.home_dir / ".agents" / "topicdock" / "config.json"
        self.assertTrue(pointer.is_file())
        self.assertEqual(
            json.loads(pointer.read_text(encoding="utf-8")),
            {"store_dir": str(target.resolve())},
        )
        self.assertFalse((self.workspace_dir / ".agents").exists())
        self._assert_store_layout(target)

    def test_init_idempotent_keeps_meta(self):
        run_cli(["init", "--scope", "local", "--json"])
        store = (self.workspace_dir / ".topicdock").resolve()
        custom_meta = {"schema_version": 1, "max_hop_seq": 7}
        (store / ".topicdock-meta.json").write_text(json.dumps(custom_meta), encoding="utf-8")
        code, data = run_cli_json(["init", "--scope", "local", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertFalse(data["created"])
        self.assertEqual(
            json.loads((store / ".topicdock-meta.json").read_text(encoding="utf-8")),
            custom_meta,
        )

    def test_init_rewrites_corrupt_meta(self):
        run_cli(["init", "--scope", "local", "--json"])
        store = (self.workspace_dir / ".topicdock").resolve()
        (store / ".topicdock-meta.json").write_text("{corrupt", encoding="utf-8")
        code, _data = run_cli_json(["init", "--scope", "local", "--json"])
        self.assertEqual(code, 0)
        meta = json.loads((store / ".topicdock-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta, DEFAULT_META)

    def test_init_gitignore_not_rewritten_when_identical(self):
        run_cli(["init", "--scope", "local", "--json"])
        store = (self.workspace_dir / ".topicdock").resolve()
        gitignore = store / ".gitignore"
        mtime_before = gitignore.stat().st_mtime_ns
        run_cli(["init", "--scope", "local", "--json"])
        self.assertEqual(gitignore.stat().st_mtime_ns, mtime_before)


# ---------- 3. 未初始化 ----------

class TestUninitializedStore(TopicDockTestCase):
    def test_list_json_without_store(self):
        code, data = run_cli_json(["list", "--json"])
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "UNINITIALIZED_STORE")
        self.assertEqual(data["default_global"], "~/.topicdock")
        self.assertIn("workspace_local", data)
        self.assertTrue(data["workspace_local"].endswith(".topicdock"))

    def test_list_human_readable_without_store(self):
        code, out = run_cli(["list"])
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertIn("UNINITIALIZED_STORE", out)
        self.assertIn("workspace_local", out)


# ---------- 4. 占位子命令 ----------

class TestPlaceholderCommands(TopicDockTestCase):
    def test_all_placeholder_commands(self):
        make_store()
        for name in topicdock.PLACEHOLDER_COMMANDS:
            with self.subTest(command=name):
                code, data = run_cli_json([name, "--json"])
                self.assertEqual(code, EXIT_INVALID_INPUT)
                self.assertEqual(data["status"], "INVALID_INPUT")


# ---------- 5. FileLock ----------

class TestFileLock(TopicDockTestCase):
    def test_exclusive_mutex_and_reacquire(self):
        lock_path = self.tmp_root / "guard1.lock"
        first = FileLock(lock_path)
        first.acquire()
        try:
            second = FileLock(lock_path)
            with self.assertRaises(OSError):
                second.acquire(blocking=False)
        finally:
            first.release()
        second.acquire(blocking=False)
        second.release()

    def test_shared_blocks_exclusive(self):
        lock_path = self.tmp_root / "guard2.lock"
        shared = FileLock(lock_path, shared=True)
        shared.acquire()
        try:
            exclusive = FileLock(lock_path)
            with self.assertRaises(OSError):
                exclusive.acquire(blocking=False)
        finally:
            shared.release()

    def test_exclusive_blocks_shared(self):
        lock_path = self.tmp_root / "guard3.lock"
        exclusive = FileLock(lock_path)
        exclusive.acquire()
        try:
            shared = FileLock(lock_path, shared=True)
            with self.assertRaises(OSError):
                shared.acquire(blocking=False)
        finally:
            exclusive.release()

    def test_shared_locks_coexist(self):
        lock_path = self.tmp_root / "guard4.lock"
        a = FileLock(lock_path, shared=True)
        b = FileLock(lock_path, shared=True)
        a.acquire(blocking=False)
        try:
            b.acquire(blocking=False)
            b.release()
        finally:
            a.release()

    def test_context_manager_releases_on_exit(self):
        lock_path = self.tmp_root / "guard5.lock"
        with FileLock(lock_path):
            with self.assertRaises(OSError):
                FileLock(lock_path).acquire(blocking=False)
        # __exit__ 已释放锁，可再次获取
        FileLock(lock_path).acquire(blocking=False).release()

    def test_creates_missing_lock_file(self):
        lock_path = self.tmp_root / "nested" / "guard6.lock"
        with FileLock(lock_path):
            self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.exists())


# ---------- 6. atomic_write_text ----------

class TestAtomicWrite(TopicDockTestCase):
    def test_overwrite_existing_file(self):
        target = self.workspace_dir / "note.txt"
        atomic_write_text(target, "v1")
        atomic_write_text(target, "v2-longer-content")
        self.assertEqual(target.read_text(encoding="utf-8"), "v2-longer-content")

    def test_no_tmp_residue(self):
        target = self.workspace_dir / "sub" / "note.txt"
        target.parent.mkdir()
        atomic_write_text(target, "content")
        self.assertEqual(target.read_text(encoding="utf-8"), "content")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["note.txt"])
        self.assertEqual([p.name for p in self.workspace_dir.iterdir()], ["sub"])


# ---------- 7. sha256_text / file_sha256 ----------

class TestHashes(TopicDockTestCase):
    def test_sha256_text_known_vectors(self):
        self.assertEqual(sha256_text(""), EMPTY_SHA256)
        self.assertEqual(sha256_text("abc"), ABC_SHA256)

    def test_file_sha256(self):
        target = self.workspace_dir / "f.bin"
        atomic_write_text(target, "abc")
        self.assertEqual(file_sha256(target), ABC_SHA256)
        self.assertEqual(file_sha256(self.workspace_dir / "nope.bin"), "MISSING")


# ---------- 8. StoreContext ----------

class TestStoreContext(TopicDockTestCase):
    def test_meta_json_missing_or_corrupt_returns_default(self):
        store = make_store()
        ctx = StoreContext(store)
        (store / ".topicdock-meta.json").unlink()
        self.assertEqual(ctx.meta_json(), DEFAULT_META)
        (store / ".topicdock-meta.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(ctx.meta_json(), DEFAULT_META)

    def test_write_meta_roundtrip(self):
        store = make_store()
        ctx = StoreContext(store)
        ctx.write_meta({"schema_version": 1, "max_hop_seq": 3})
        self.assertEqual(ctx.meta_json(), {"schema_version": 1, "max_hop_seq": 3})

    def test_lock_helpers_target_lock_path(self):
        store = make_store()
        ctx = StoreContext(store)
        self.assertEqual(ctx.shared_lock().lock_path, store / ".topicdock.lock")
        self.assertTrue(ctx.shared_lock().shared)
        self.assertFalse(ctx.exclusive_lock().shared)


# ---------- 9. --store-dir 显式参数 ----------

class TestStoreDirExplicit(TopicDockTestCase):
    def test_store_dir_bypasses_resolution(self):
        store = make_store()
        # --store-dir 显式指向已初始化的 store：绕过寻址由 handler 真正运行证明
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(data["status"], "OK")

    def test_store_dir_uninitialized_gate(self):
        bogus = self.tmp_root / "not-a-store"
        bogus.mkdir()
        code, data = run_cli_json(["list", "--store-dir", str(bogus), "--json"])
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertEqual(data["status"], "UNINITIALIZED_STORE")

    def test_store_dir_nonexistent_path_gated(self):
        bogus = self.tmp_root / "does-not-exist"
        code, data = run_cli_json(["list", "--store-dir", str(bogus), "--json"])
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertEqual(data["status"], "UNINITIALIZED_STORE")


# ---------- 10. import topicdock 可用性（D-P31） ----------

class TestModuleImport(TopicDockTestCase):
    def test_topicdock_importable_with_main(self):
        self.assertTrue(callable(topicdock.main))
        self.assertTrue(hasattr(topicdock, "render_output"))


# ---------- 11. L2 MarkdownCodec（M2） ----------

from topicdock import (  # noqa: E402
    ApplyContext,
    HopBlock,
    HopDoc,
    LatestHop,
    TopicDoc,
    TopicItem,
    UnsupportedSchemaVersion,
    apply_hop,
    apply_hop_block,
    format_trace_tag,
    parse_hop_md,
    parse_topic_md,
    render_hop_md,
    render_topic_md,
)


def _mk_item(item_id, **over):
    track = item_id[:1]
    values = dict(
        statement=f"{item_id} 结论正文",
        a_state=None,
        evidence_status=None,
        basis=None,
        rationale="",
        evidence_detail="",
        review_cond="",
        trace_records=[],
    )
    values.update(over)
    return TopicItem(item_id=item_id, track=track, **values)


def _mk_doc():
    doc = TopicDoc(
        topic_id="auth-refactor",
        title='认证模块无状态 JWT 改造 "灰度" 迁移',
        head_hop="H004",
        updated_at="2026-11-16T10:30:00Z",
        last_actor="architect",
        next_seq={"C": 3, "R": 1, "Q": 1, "A": 3},
    )
    doc.items["C"] = [
        _mk_item("C2", statement="外部网关统一签发 EdDSA 短效 JWT（15m）",
                 evidence_status="verified", basis="压测 P99 延迟从 18ms 降至 1.2ms"),
        _mk_item("C3", statement="Refresh Token 滑动窗口旋转",
                 evidence_status="inferred", basis="兼顾免登与吊销"),
    ]
    doc.items["R"] = [
        _mk_item("R1", statement="放弃 Session 共享库方案", basis="跨区延迟导致 401"),
    ]
    doc.items["Q"] = [_mk_item("Q1", statement="Webhook 是否走同一网关？")]
    doc.items["A"] = [
        _mk_item("A1", a_state="x", statement="完成压测与选型"),
        _mk_item("A2", a_state="~", statement="编写接口契约", basis="待敲定回调头"),
        _mk_item("A3", a_state=" ", statement="交由 coder 实施"),
    ]
    doc.artifacts = [
        ("网关设计", "/abs/path/design.md", "定义载荷字段"),
        ("无备注条目", "/abs/other.md", None),
    ]
    doc.latest = LatestHop(
        hop_id="H004", updated_at="2026-11-16T10:30:00Z", actor="architect",
        parents=["H001", "H002"], sha12="9f8e7d6c5b4a",
        summary="敲定旋转机制", next_focus="拍板 Q1",
    )
    return doc


TAGS = {
    "C2": "(H003)", "C3": "(H004)", "R1": "(原立H001→推翻H003)",
    "Q1": "(H004)", "A1": "(H001→H002)", "A2": "(H004)", "A3": "(H004)",
}


class TestTopicRoundtrip(TopicDockTestCase):
    def test_render_parse_render_idempotent(self):
        doc = _mk_doc()
        text = render_topic_md(doc, TAGS)
        reparsed = parse_topic_md(text)
        text2 = render_topic_md(reparsed, TAGS)
        self.assertEqual(text2, text)

    def test_parse_recovers_structure(self):
        doc = parse_topic_md(render_topic_md(_mk_doc(), TAGS))
        self.assertEqual(doc.topic_id, "auth-refactor")
        self.assertEqual(doc.title, '认证模块无状态 JWT 改造 "灰度" 迁移')
        self.assertEqual(doc.head_hop, "H004")
        self.assertEqual(doc.next_seq, {"C": 3, "R": 1, "Q": 1, "A": 3})
        self.assertEqual([i.item_id for i in doc.items["C"]], ["C2", "C3"])
        self.assertEqual([i.item_id for i in doc.items["A"]], ["A1", "A2", "A3"])
        self.assertEqual(len(doc.artifacts), 2)
        self.assertEqual(doc.artifacts[0], ("网关设计", "/abs/path/design.md", "定义载荷字段"))
        self.assertEqual(doc.artifacts[1], ("无备注条目", "/abs/other.md", None))
        self.assertEqual(doc.latest.parents, ["H001", "H002"])
        self.assertEqual(doc.latest.sha12, "9f8e7d6c5b4a")
        self.assertEqual(doc.latest.summary, "敲定旋转机制")

    def test_parse_recovers_item_fields(self):
        doc = parse_topic_md(render_topic_md(_mk_doc(), TAGS))
        c2 = doc.items["C"][0]
        self.assertEqual(c2.statement, "外部网关统一签发 EdDSA 短效 JWT（15m）")
        self.assertEqual(c2.evidence_status, "verified")
        self.assertEqual(c2.basis, "压测 P99 延迟从 18ms 降至 1.2ms")
        self.assertEqual(c2.trace_records, ["H003"])
        r1 = doc.items["R"][0]
        self.assertEqual(r1.basis, "跨区延迟导致 401")
        a2 = doc.items["A"][1]
        self.assertEqual(a2.a_state, "~")
        self.assertEqual(a2.basis, "待敲定回调头")
        self.assertEqual(doc.items["A"][2].a_state, " ")
        self.assertEqual(doc.items["A"][0].a_state, "x")

    def test_schema_version_guard(self):
        doc = _mk_doc()
        doc.schema_version = 2
        text = render_topic_md(doc, TAGS)
        with self.assertRaises(UnsupportedSchemaVersion):
            parse_topic_md(text)


class TestItemEscaping(TopicDockTestCase):
    def test_literal_separators_escaped_and_recovered(self):
        doc = _mk_doc()
        poison = "正常文本 — 依据(verified): 伪依据 且 — 原因: 伪原因 且 — 断点: 伪断点"
        doc.items["Q"][0].statement = poison
        text = render_topic_md(doc, TAGS)
        reparsed = parse_topic_md(text)
        self.assertEqual(reparsed.items["Q"][0].statement, poison)
        self.assertIsNone(reparsed.items["Q"][0].basis)
        self.assertEqual(render_topic_md(reparsed, TAGS), text)

    def test_trailing_tag_like_text_escaped(self):
        doc = _mk_doc()
        doc.items["Q"][0].statement = "以 (H999) 结尾的字面量 (H888)"
        text = render_topic_md(doc, TAGS)
        line = [ln for ln in text.split("\n") if ":Q1]" in ln][0]
        self.assertNotIn("&#40;H999)", line)   # 仅行尾组转义，中间组保持原样
        self.assertIn("&#40;H888)", line)      # 行尾组被转义
        reparsed = parse_topic_md(text)
        self.assertEqual(reparsed.items["Q"][0].statement, "以 (H999) 结尾的字面量 (H888)")
        self.assertEqual(reparsed.items["Q"][0].trace_records, ["H004"])
        self.assertEqual(render_topic_md(reparsed, TAGS), text)

    def test_statement_ending_with_paren_group(self):
        doc = _mk_doc()
        doc.items["Q"][0].statement = "字面量结尾 (H999)"
        text = render_topic_md(doc, TAGS)
        line = [ln for ln in text.split("\n") if ":Q1]" in ln][0]
        self.assertNotIn(" (H999)", line)
        reparsed = parse_topic_md(text)
        self.assertEqual(reparsed.items["Q"][0].statement, "字面量结尾 (H999)")
        self.assertEqual(reparsed.items["Q"][0].trace_records, ["H004"])

    def test_fullwidth_paren_not_escaped(self):
        doc = _mk_doc()
        doc.items["Q"][0].statement = "全角（15m）不转义"
        text = render_topic_md(doc, TAGS)
        self.assertIn("全角（15m）不转义 (H004)", text)
        self.assertEqual(parse_topic_md(text).items["Q"][0].statement, "全角（15m）不转义")


class TestSemanticMinimalEscaping(TopicDockTestCase):
    """P4 修订：渲染端合成体级语义最小转义（仅当明文形态会 parse 错分时转义）。"""

    def _line(self, text: str, marker: str) -> str:
        return [line for line in text.split("\n") if marker in line][0]

    def test_c_with_status_statement_pattern_zero_entity(self):
        # 结构分隔符存在且 statement 内含完整模式 → rfind 命中真实边界 → 零实体
        doc = _mk_doc()
        poison = "前文 — 依据(verified): 伪依据 尾部"
        doc.items["C"][0].statement = poison
        original_basis = doc.items["C"][0].basis
        text = render_topic_md(doc, TAGS)
        self.assertNotIn("&#8212;", self._line(text, ":C2]"))
        c2 = parse_topic_md(text).items["C"][0]
        self.assertEqual(c2.statement, poison)
        self.assertEqual(c2.evidence_status, "verified")
        self.assertEqual(c2.basis, original_basis)

    def test_q_with_basis_statement_pattern_minimal_escape(self):
        # Q 轨 basis 不渲染 → 合成体无结构分隔符 → 全部模式出现处最小转义
        doc = _mk_doc()
        poison = "问题 — 原因: 伪原因 尾部"
        doc.items["Q"][0].statement = poison
        doc.items["Q"][0].basis = "Q 依据保留于状态但不渲染"
        text = render_topic_md(doc, TAGS)
        line = self._line(text, ":Q1]")
        self.assertIn("&#8212;", line)
        self.assertNotIn("Q 依据保留于状态但不渲染", line)
        reparsed = parse_topic_md(text).items["Q"][0]
        self.assertEqual(reparsed.statement, poison)
        self.assertIsNone(reparsed.basis)

    def test_c_without_status_basis_unrendered_minimal_escape(self):
        # C-无-status：basis 保于状态但不渲染 → 合成体无结构分隔符 → 最小转义
        doc = _mk_doc()
        poison = "无状态结论 — 断点: 伪断点 尾部"
        item = doc.items["C"][1]  # C3
        item.evidence_status = None
        item.basis = "保留于状态但不渲染的依据"
        item.statement = poison
        text = render_topic_md(doc, TAGS)
        line = self._line(text, ":C3]")
        self.assertIn("&#8212;", line)
        self.assertNotIn("保留于状态但不渲染的依据", line)
        reparsed = parse_topic_md(text).items["C"][1]
        self.assertEqual(reparsed.statement, poison)
        self.assertIsNone(reparsed.basis)

    def test_no_basis_all_occurrences_escaped(self):
        # 无-basis 形态：只转义最后一个会让更早的明文模式赢 rfind → 必须转义全部
        doc = _mk_doc()
        poison = "甲 — 原因: 乙 — 断点: 丙"
        doc.items["Q"][0].statement = poison
        text = render_topic_md(doc, TAGS)
        line = self._line(text, ":Q1]")
        self.assertEqual(line.count("&#8212;"), 2)
        reparsed = parse_topic_md(text).items["Q"][0]
        self.assertEqual(reparsed.statement, poison)
        self.assertIsNone(reparsed.basis)

    def test_boundary_basis_without_leading_space_parse_faithful(self):
        # legacy 拼接边界：basis 以无前导空格的 `— 断点: ` 开头，借结构分隔符尾空格成假分隔符
        doc = _mk_doc()
        doc.items["A"][1].basis = "— 断点: 待敲定"  # A2
        text = render_topic_md(doc, TAGS)
        reparsed = parse_topic_md(text)
        a2 = reparsed.items["A"][1]
        self.assertEqual(a2.statement, doc.items["A"][1].statement)
        self.assertEqual(a2.basis, "— 断点: 待敲定")
        self.assertEqual(render_topic_md(reparsed, TAGS), text)

    def test_literal_entity_text_not_over_escaped(self):
        # 载荷字面量含 `&#8212;` 文本：判定不触发过转义，字节稳定
        doc = _mk_doc()
        doc.items["Q"][0].statement = "用户原文含 &#8212; 这个实体文本"
        text = render_topic_md(doc, TAGS)
        line = self._line(text, ":Q1]")
        self.assertEqual(line.count("&#8212;"), 1)  # 仅用户字面量，不新增
        self.assertEqual(render_topic_md(doc, TAGS), text)

    def test_payload_roundtrip_zero_entity(self):
        # 载荷含全角/破折号等字符：顶层行与 hop 分片字段逐字节同字符、零实体
        store = make_store()
        statement = "结论 —— ； （ ） 「」 · 保留原字符"
        basis = "依据 —— ； （ ） 「」 · 同样保留"
        code, data = run_save(store, {
            "topic_id": "escape", "title": "转义往返", "base_hop": None,
            "base_sha": None, "session_id": "s", "actor": "t", "summary": "首轮",
            "mutations": [{"op": "create", "track": "C", "statement": statement,
                           "evidence_status": "verified", "basis": basis}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        md_text = (store / "topics" / "escape.md").read_text(encoding="utf-8")
        self.assertIn(statement, md_text)
        self.assertIn(basis, md_text)
        self.assertNotIn("&#8212;", md_text)
        self.assertNotIn("&#40;", md_text)
        hop_text = (store / "hops" / topicdock.hop_file_name("H001")).read_text(
            encoding="utf-8"
        )
        fields = parse_hop_md(hop_text).blocks[0].fields
        self.assertEqual(fields["结论正文"], statement)
        self.assertEqual(fields["一句话依据"], basis)

    def test_legacy_entity_md_parses_back_to_plaintext(self):
        # 手工构造的存量 md：读取还原把 `&#8212;` 转为明文 `—`
        legacy = (
            "---\n"
            "schema_version: 1\n"
            "topic_id: legacy\n"
            'title: "legacy"\n'
            "head_hop: H001\n"
            "updated_at: 2026-11-01T00:00:00Z\n"
            "last_actor: tester\n"
            'next_seq: {"C":1,"R":0,"Q":0,"A":0}\n'
            "---\n\n"
            "## 1. 认知与定论 (Knowledge & Conclusions)\n\n"
            "### 核心背景与已拍板结论 (Confirmed `[C]`)\n"
            "- [legacy:C1] 旧文本 &#8212; 依据(verified): 伪 (H001)\n"
        )
        doc = parse_topic_md(legacy)
        item = doc.items["C"][0]
        self.assertEqual(item.statement, "旧文本 — 依据(verified): 伪")
        self.assertIsNone(item.basis)


def _mk_header(**over):
    header = {
        "v": 1, "hop_id": "H003", "topic_origin": "auth-refactor",
        "title": "認證模塊無狀態 JWT 改造", "parents": ["H002"], "session_id": "ses-01",
        "created_at": "2026-11-16T09:15:00Z", "actor": "consultant",
        "summary": "推翻共享 Session 方案，确立 EdDSA",
        "next_focus": "设计吊销机制",
        "artifacts": [{"name": "网关设计", "path": "/abs/design.md", "note": "载荷字段"}],
        "sha12": "9f8e7d6c5b4a",
    }
    header.update(over)
    return header


def _mk_blocks():
    return [
        HopBlock(
            item_id="R1", op="overturn", from_items=["C1"],
            origin="auth-refactor:C1@H001",
            fields={"结论正文": "放弃 Session 共享库方案", "一句话原因": "跨区延迟 401",
                    "推导与权衡": "副本延迟 120ms", "核心证据 (verified)": "复盘日志 L412",
                    "重审条件": "收敛单可用区"},
        ),
        HopBlock(
            item_id="C2", op="create", status="verified",
            fields={"结论正文": "EdDSA 短效 JWT", "一句话依据": "P99 1.2ms",
                    "核心证据 (verified)": "bench 实测", "重审条件": "FIPS 强制时重议"},
        ),
        HopBlock(
            item_id="A2", op="update", a_state="~",
            fields={"结论正文": "编写接口契约", "断点": "待敲定回调头"},
        ),
    ]


class TestHopCodec(TopicDockTestCase):
    def test_header_special_chars_roundtrip(self):
        header = _mk_header(
            summary="包含 --> 与 <script> 与 a--b 的摘要",
        )
        text = render_hop_md(HopDoc(header=header, blocks=[]))
        first_line = text.split("\n")[0]
        payload = first_line[
            len("<!-- topicdock-hop: "): -len(" -->")
        ]
        self.assertNotIn("-->", payload)   # 载荷内不得含裸注释结束符
        self.assertNotIn("<", payload)     # 载荷内不得含裸 <
        self.assertIn("\\u003c", payload)
        reparsed = parse_hop_md(text)
        self.assertEqual(reparsed.header["summary"], "包含 --> 与 <script> 与 a--b 的摘要")
        self.assertEqual(reparsed.header["title"], header["title"])
        self.assertEqual(reparsed.header["artifacts"], header["artifacts"])
        self.assertEqual(render_hop_md(reparsed), text)

    def test_header_missing_required_raises(self):
        header = _mk_header()
        del header["title"]
        with self.assertRaises(ValueError):
            render_hop_md(HopDoc(header=header, blocks=[]))

    def test_header_extra_fields_passthrough(self):
        header = _mk_header(custom_field={"k": [1, 2]})
        text = render_hop_md(HopDoc(header=header, blocks=[]))
        self.assertEqual(parse_hop_md(text).header["custom_field"], {"k": [1, 2]})

    def test_zero_block_hop_roundtrip(self):
        text = render_hop_md(HopDoc(header=_mk_header(), blocks=[]))
        reparsed = parse_hop_md(text)
        self.assertEqual(reparsed.blocks, [])
        self.assertEqual(render_hop_md(reparsed), text)

    def test_blocks_roundtrip_full_and_partial_attrs(self):
        blocks = _mk_blocks()
        blocks.append(HopBlock(item_id="A9", op="fold", a_state=" ",
                               from_items=["A1", "A2", "A3"],
                               fields={"结论正文": "汇总"}))
        blocks.append(HopBlock(item_id="C5", op="inherit",
                               origin="src:C1@H002", fields={"结论正文": "继承条目"}))
        text = render_hop_md(HopDoc(header=_mk_header(), blocks=blocks))
        reparsed = parse_hop_md(text)
        self.assertEqual(len(reparsed.blocks), 5)
        self.assertEqual(reparsed.blocks[0].op, "overturn")
        self.assertEqual(reparsed.blocks[0].from_items, ["C1"])
        self.assertEqual(reparsed.blocks[0].origin, "auth-refactor:C1@H001")
        self.assertEqual(reparsed.blocks[1].status, "verified")
        self.assertEqual(reparsed.blocks[2].a_state, "~")
        self.assertEqual(reparsed.blocks[3].a_state, " ")
        self.assertEqual(reparsed.blocks[3].from_items, ["A1", "A2", "A3"])
        self.assertEqual(reparsed.blocks[4].origin, "src:C1@H002")
        self.assertEqual(reparsed.blocks[0].fields["核心证据 (verified)"], "复盘日志 L412")
        self.assertEqual(render_hop_md(HopDoc(header=_mk_header(), blocks=reparsed.blocks)), text)

    def test_block_multiline_and_escape_roundtrip(self):
        fields = {"结论正文": "第一行\n第二行\n### [伪装块标题]\n第四行\n\n空行后"}
        text = render_hop_md(HopDoc(header=_mk_header(), blocks=[
            HopBlock(item_id="C2", op="create", fields=fields)]))
        reparsed = parse_hop_md(text)
        self.assertEqual(reparsed.blocks[0].fields["结论正文"], "第一行\n第二行\n### [伪装块标题]\n第四行\n\n空行后")


class TestComputeHopOid(TopicDockTestCase):
    """D8：位置无关 Merkle 内容哈希（固定 13 键 + 域分隔 SHA-256）。"""

    def _legacy_header(self):
        return {
            "v": 1, "hop_id": "H001", "topic_origin": "legacy",
            "title": "存量旧跳", "parents": [],
            "session_id": "ses-legacy", "created_at": "2026-11-01T00:00:00Z",
            "actor": "tester", "summary": "无 Merkle 字段",
            "next_focus": "", "artifacts": [],
        }

    def _oid(self, header, blocks=None):
        return topicdock.compute_hop_oid(
            HopDoc(header=header, blocks=list(blocks or []))
        )

    def test_deterministic_and_shape(self):
        hop = HopDoc(header=_mk_header(), blocks=_mk_blocks())
        first = topicdock.compute_hop_oid(hop)
        self.assertEqual(first, topicdock.compute_hop_oid(hop))
        self.assertRegex(first, r"^v1:[0-9a-f]{64}$")

    def test_position_independent_local_slots(self):
        ext = {"topic_uid": "0123456789abcdef0123456789abcdef",
               "parent_oids": ["v1:" + "b" * 64]}
        base_oid = self._oid(_mk_header(**ext), _mk_blocks())
        variants = [
            _mk_header(hop_id="H999", **ext),
            _mk_header(parents=["H001", "H002"], **ext),
            _mk_header(topic_origin="renamed-topic", **ext),
            _mk_header(hop_oid="v1:" + "f" * 64, sha12="000000000000", **ext),
        ]
        for header in variants:
            self.assertEqual(self._oid(header, _mk_blocks()), base_oid)
        local_origin_blocks = _mk_blocks()
        local_origin_blocks[0].origin = "another-topic:C1@H999"
        self.assertEqual(self._oid(_mk_header(**ext), local_origin_blocks), base_oid)
        self.assertNotEqual(
            self._oid(_mk_header(summary="改了摘要", **ext), _mk_blocks()), base_oid
        )

    def test_origin_item_included_but_origin_hop_ignored(self):
        header = self._legacy_header()

        def block(origin):
            return HopBlock(item_id="C1", op="inherit", origin=origin,
                            fields={"结论正文": "继承条目"})

        first = self._oid(header, [block("src:C1@H001")])
        self.assertEqual(first, self._oid(header, [block("src:C1@H999")]))
        self.assertNotEqual(first, self._oid(header, [block("src:C2@H001")]))

    def test_legacy_missing_extensions_equal_safe_defaults(self):
        legacy = HopDoc(header=self._legacy_header(), blocks=[])
        explicit = dict(self._legacy_header())
        explicit.update({"topic_uid": "", "parent_oids": [], "origin_oids": {}})
        self.assertEqual(
            topicdock.compute_hop_oid(legacy),
            topicdock.compute_hop_oid(HopDoc(header=explicit, blocks=[])),
        )

    def test_type_anomalies_normalized_safely(self):
        weird = dict(
            self._legacy_header(), topic_uid=None, parent_oids="oops",
            origin_oids=[1, 2], model=123,
            artifacts=[{"name": 1, "path": None, "note": None}],
        )
        safe = dict(
            self._legacy_header(), topic_uid="", parent_oids=[],
            origin_oids={},
            artifacts=[{"name": "1", "path": "None", "note": ""}],
        )
        self.assertEqual(self._oid(weird), self._oid(safe))

    def test_block_a_state_space_normalized(self):
        plain = HopDoc(header=self._legacy_header(), blocks=[
            HopBlock(item_id="A1", op="create", a_state=" ",
                     fields={"结论正文": "行动"}),
        ])
        word = HopDoc(header=self._legacy_header(), blocks=[
            HopBlock(item_id="A1", op="create", a_state="space",
                     fields={"结论正文": "行动"}),
        ])
        self.assertEqual(
            topicdock.compute_hop_oid(plain), topicdock.compute_hop_oid(word)
        )

    def test_golden_legacy_hop(self):
        """跨解释器冻结向量 ①：存量旧跳（无 4 字段、零块）。"""
        header = {
            "v": 1, "hop_id": "H001", "topic_origin": "legacy-doc",
            "title": "存量旧跳", "parents": [],
            "session_id": "ses-legacy", "created_at": "2026-11-01T00:00:00Z",
            "actor": "tester", "summary": "旧客户端写入，无 Merkle 字段",
            "next_focus": "", "artifacts": [],
        }
        self.assertEqual(
            self._oid(header),
            "v1:a31b360aa2f721c61350d9a12e9360a399b28da4f70bc9483dd41cd093a2a62e",
        )

    def test_golden_rich_hop(self):
        """跨解释器冻结向量 ②：块 + origin + artifacts + 非 ASCII。"""
        header = {
            "v": 1, "hop_id": "H042", "topic_origin": "认证-refactor",
            "title": "認證模塊無狀態 JWT 改造 / Auth Stateless JWT Refactor",
            "parents": ["H041"], "session_id": "ses-golden",
            "created_at": "2026-11-16T09:15:00Z", "actor": "opencode/architect",
            "model": "opencode-go/deepseek-v4.1-flash",
            "summary": "推翻共享 Session(C1→R1)，确立 EdDSA(C2)",
            "next_focus": "设计 Refresh Token 吊销机制",
            "artifacts": [
                {"name": "网关架构设计记录", "path": "/abs/design.md",
                 "note": "定义 JWT 载荷字段与公钥轮换接口"},
            ],
            "topic_uid": "0123456789abcdef0123456789abcdef",
            "parent_oids": ["v1:" + "a" * 64],
            "origin_oids": {"R1": "0123456789abcdef0123456789abcdef:C1@v1:" + "b" * 64},
        }
        blocks = [
            HopBlock(item_id="R1", op="overturn", from_items=["C1"],
                     origin="认证-refactor:C1@H041",
                     fields={"结论正文": "放弃全链路 Session 共享库",
                             "一句话原因": "跨区同步延迟导致偶发 401"}),
            HopBlock(item_id="C2", op="create", status="verified",
                     fields={"结论正文": "EdDSA 短效 JWT（15m）",
                             "一句话依据": "P99 从 18ms 降至 1.2ms"}),
            HopBlock(item_id="A2", op="update", a_state="~",
                     fields={"结论正文": "编写接口契约", "断点": "待敲定回调头"}),
        ]
        self.assertEqual(
            self._oid(header, blocks),
            "v1:e2247a2e70d9356539d75c8982a3aab07457d2e4e41fc64a97b9472b5a734917",
        )


class TestMerkleFaultTolerance(TopicDockTestCase):
    """D8 读侧非破坏性容错：4 字段缺失/类型异常/hop_oid 漂移不得判损坏。"""

    def _ctx(self):
        ctx = StoreContext(self.tmp_root / "merkle-store")
        ctx.hops_dir.mkdir(parents=True, exist_ok=True)
        return ctx

    def _write_hop(self, ctx, header, blocks=()):
        path = ctx.hops_dir / topicdock.hop_file_name(header["hop_id"])
        topicdock.atomic_write_text(
            path, topicdock.render_hop_md(HopDoc(header=header, blocks=list(blocks)))
        )
        return path

    def _valid_header(self, hop_id="H001", **over):
        header = {
            "v": 1, "hop_id": hop_id, "topic_origin": "alpha",
            "title": "alpha 主题标题", "parents": [],
            "session_id": "ses-01", "created_at": "2026-11-01T00:00:00Z",
            "actor": "tester", "summary": "摘要", "next_focus": "",
            "artifacts": [],
        }
        header.update(over)
        return header

    def test_load_hop_tolerates_missing_extension_fields(self):
        ctx = self._ctx()
        self._write_hop(ctx, self._valid_header())
        loaded = topicdock._load_hop(ctx, "H001")
        self.assertIsNotNone(loaded)
        self.assertNotIn("topic_uid", loaded.hop.header)
        self.assertNotIn("parent_oids", loaded.hop.header)
        self.assertNotIn("origin_oids", loaded.hop.header)
        self.assertNotIn("hop_oid", loaded.hop.header)

    def test_load_hop_normalizes_extension_type_anomalies(self):
        ctx = self._ctx()
        self._write_hop(ctx, self._valid_header(
            topic_uid=None, parent_oids="oops", origin_oids=[1, 2], hop_oid=None,
        ))
        loaded = topicdock._load_hop(ctx, "H001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.hop.header["topic_uid"], "")
        self.assertEqual(loaded.hop.header["parent_oids"], [])
        self.assertEqual(loaded.hop.header["origin_oids"], {})
        self.assertEqual(loaded.hop.header["hop_oid"], "")

    def test_hop_oid_drift_does_not_cascade(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "alpha 主题标题", "base_hop": None,
            "base_sha": None, "session_id": "ses-01", "actor": "tester",
            "mutations": [{"op": "create", "track": "C", "statement": "结论"}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        ctx = StoreContext(Path(store))
        hop_path = ctx.hops_dir / topicdock.hop_file_name("H001")
        hop = topicdock.parse_hop_md(hop_path.read_text(encoding="utf-8"))
        hop.header["hop_oid"] = "v1:" + "0" * 64
        topicdock.atomic_write_text(hop_path, topicdock.render_hop_md(hop))
        conn = get_or_create_db(ctx)
        conn.execute(
            "UPDATE hops SET md_sha256=? WHERE hop_id='H001'",
            (topicdock.file_sha256(hop_path),),
        )
        conn.commit()
        conn.close()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM topics WHERE topic_id='alpha'").fetchone()
            )
            self.assertNotIn("cascade_delete", [r["name"] for r in reports])
            loaded = topicdock._load_hop(ctx, "H001")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.hop.header["hop_oid"], "v1:" + "0" * 64)
        finally:
            conn.close()

    def test_doctor_rebuild_preserves_legacy_hop_bytes(self):
        store = make_store()
        build_rich_store(store)
        ctx = StoreContext(Path(store))
        before = {
            path.name: (path.read_bytes(), topicdock.file_sha256(path))
            for path in sorted(ctx.hops_dir.glob("hop-*.md"))
        }
        code, _report = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        after = {
            path.name: (path.read_bytes(), topicdock.file_sha256(path))
            for path in sorted(ctx.hops_dir.glob("hop-*.md"))
        }
        self.assertEqual(before, after)


class TestFormatTraceTag(TopicDockTestCase):
    def test_single_and_short_chains(self):
        self.assertEqual(format_trace_tag(["H001"]), "(H001)")
        self.assertEqual(format_trace_tag(["H001", "H003"]), "(H001→H003)")
        self.assertEqual(format_trace_tag(["H001", "H003", "H005"]), "(H001→H003→H005)")

    def test_folded_over_three(self):
        records = ["H001", "H002", "H003", "H004", "H005", "H012"]
        self.assertEqual(format_trace_tag(records), "(H001→..→H012, 6跳)")

    def test_r_track_form(self):
        self.assertEqual(
            format_trace_tag([], origin_hop="H001", overturn_hop="H003"),
            "(原立H001→推翻H003)",
        )

    def test_degradation_chain_hard_cap(self):
        wide3 = format_trace_tag(["H10000", "H10003", "H10005"])
        self.assertEqual(wide3, "(H10000)")
        wide_folded = format_trace_tag(
            ["H10000", "H10001", "H10002", "H10003", "H10004", "H10009"])
        self.assertEqual(wide_folded, "(H10000..+6跳)")
        wide_r = format_trace_tag([], origin_hop="H100001", overturn_hop="H100003")
        self.assertEqual(wide_r, "(推翻H100003)")

    def test_all_forms_within_hard_cap(self):
        cases = [
            format_trace_tag(["H001"]),
            format_trace_tag(["H001", "H003"]),
            format_trace_tag(["H001", "H003", "H005"]),
            format_trace_tag(["H001", "H002", "H003", "H004", "H005", "H012"]),
            format_trace_tag(["H10000", "H10001", "H10002", "H10003", "H10009"]),
            format_trace_tag([], origin_hop="H001", overturn_hop="H003"),
            format_trace_tag([], origin_hop="H100001", overturn_hop="H100003"),
            format_trace_tag(["H" + "9" * 20]),
        ]
        for tag in cases:
            self.assertLessEqual(len(tag), 20, tag)
            self.assertLessEqual(len(tag.encode("utf-8")), 32, tag)

    def test_empty_records(self):
        self.assertEqual(format_trace_tag([]), "")


class TestApplyHopBlock(TopicDockTestCase):
    def _doc_with_c1(self):
        doc = TopicDoc(topic_id="t", next_seq={"C": 1, "R": 0, "Q": 1, "A": 2})
        doc.items["C"] = [_mk_item("C1", statement="原结论")]
        doc.items["Q"] = [_mk_item("Q1", statement="待决")]
        doc.items["A"] = [_mk_item("A1", a_state="x"), _mk_item("A2", a_state="x")]
        return doc

    def test_create_appends_and_notes_seq(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={"C": 1, "R": 0, "Q": 1, "A": 2})
        apply_hop_block(doc, HopBlock(
            item_id="C2", op="create", status="verified",
            fields={"结论正文": "新结论", "一句话依据": "依据"}),
            ctx)
        self.assertEqual(doc.items["C"][1].item_id, "C2")
        self.assertEqual(doc.items["C"][1].trace_records, ["H002"])
        self.assertEqual(ctx.new_seq["C"], 2)
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(item_id="C2", op="create", fields={}), ctx)

    def test_update_modifies_fields(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={})
        apply_hop_block(doc, HopBlock(
            item_id="C1", op="update", status="inferred",
            fields={"结论正文": "更新后", "一句话依据": "新依据"}), ctx)
        item = doc.items["C"][0]
        self.assertEqual(item.statement, "更新后")
        self.assertEqual(item.basis, "新依据")
        self.assertEqual(item.evidence_status, "inferred")
        self.assertIn("H002", item.trace_records)
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(item_id="C9", op="update", fields={}), ctx)

    def test_overturn_moves_c_to_r(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={})
        apply_hop_block(doc, HopBlock(
            item_id="R1", op="overturn", from_items=["C1"],
            fields={"结论正文": "推翻结论", "一句话原因": "原因"}), ctx)
        self.assertEqual([i.item_id for i in doc.items["C"]], [])
        self.assertEqual(doc.items["R"][0].statement, "推翻结论")
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(
                item_id="R2", op="overturn", from_items=["C404"], fields={}), ctx)

    def test_resolve_removes_q(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={})
        apply_hop_block(doc, HopBlock(item_id="Q1", op="resolve", from_items=["Q1"]), ctx)
        self.assertEqual(doc.items["Q"], [])
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(item_id="Q1", op="resolve", from_items=["Q1"]), ctx)

    def test_fold_merges_completed_actions(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={"A": 2})
        apply_hop_block(doc, HopBlock(
            item_id="A3", op="fold", from_items=["A1", "A2"],
            fields={"结论正文": "阶段汇总"}), ctx)
        self.assertEqual([i.item_id for i in doc.items["A"]], ["A3"])
        self.assertEqual(doc.items["A"][0].a_state, "x")
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(
                item_id="A4", op="fold", from_items=["A1"], fields={}), ctx)

    def test_retract_removes_item(self):
        doc = self._doc_with_c1()
        ctx = ApplyContext(hop_id="H002", new_seq={})
        apply_hop_block(doc, HopBlock(item_id="Q1", op="retract"), ctx)
        self.assertEqual(doc.items["Q"], [])
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(item_id="Q1", op="retract"), ctx)

    def test_unknown_op_raises(self):
        doc = self._doc_with_c1()
        with self.assertRaises(ValueError):
            apply_hop_block(doc, HopBlock(item_id="C1", op="explode"), ApplyContext())


class TestApplyHop(TopicDockTestCase):
    @staticmethod
    def _seed(doc):
        """前置种子跳：建立 C1 与 A2，供主跳 overturn/update。"""
        seed_header = _mk_header(hop_id="H001", created_at="2026-11-16T09:00:00Z")
        seed_blocks = [
            HopBlock(item_id="C1", op="create",
                     fields={"结论正文": "原结论"}),
            HopBlock(item_id="A2", op="create", a_state=" ",
                     fields={"结论正文": "编写接口契约"}),
        ]
        apply_hop(doc, seed_header, seed_blocks)

    def test_forward_on_fresh_doc(self):
        doc = TopicDoc(topic_id="auth-refactor", next_seq={})
        self._seed(doc)
        apply_hop(doc, _mk_header(), _mk_blocks())
        self.assertEqual(doc.title, "認證模塊無狀態 JWT 改造")
        self.assertEqual(doc.head_hop, "H003")
        self.assertEqual(doc.updated_at, "2026-11-16T09:15:00Z")
        self.assertEqual(doc.last_actor, "consultant")
        self.assertEqual(doc.artifacts, [("网关设计", "/abs/design.md", "载荷字段")])
        self.assertEqual(doc.latest.parents, ["H002"])
        self.assertEqual(doc.latest.sha12, "9f8e7d6c5b4a")
        self.assertEqual([i.item_id for i in doc.items["R"]], ["R1"])
        self.assertEqual([i.item_id for i in doc.items["C"]], ["C2"])
        self.assertEqual(doc.items["A"][0].basis, "待敲定回调头")
        self.assertEqual(doc.next_seq, {"C": 2, "R": 1, "A": 2})

    def test_forward_reverse_consistency(self):
        header, blocks = _mk_header(), _mk_blocks()
        doc_forward = TopicDoc(topic_id="auth-refactor", next_seq={})
        self._seed(doc_forward)
        apply_hop(doc_forward, header, blocks)
        parsed = parse_hop_md(render_hop_md(HopDoc(header=header, blocks=blocks)))
        doc_reverse = TopicDoc(topic_id="auth-refactor", next_seq={})
        self._seed(doc_reverse)
        apply_hop(doc_reverse, parsed.header, parsed.blocks)

        def _snapshot(doc):
            return {
                "title": doc.title, "head_hop": doc.head_hop,
                "updated_at": doc.updated_at, "last_actor": doc.last_actor,
                "artifacts": doc.artifacts, "next_seq": doc.next_seq,
                "items": {
                    track: [(i.item_id, i.statement, i.basis, i.a_state,
                             i.evidence_status, tuple(i.trace_records))
                            for i in items]
                    for track, items in doc.items.items()
                },
                "latest": (doc.latest.hop_id, doc.latest.actor, doc.latest.sha12,
                           doc.latest.summary, doc.latest.next_focus),
            }

        self.assertEqual(_snapshot(doc_forward), _snapshot(doc_reverse))

    def test_zero_block_apply_updates_header_only(self):
        doc = _mk_doc()
        doc.items["C"] = list(doc.items["C"])
        before = {t: [i.item_id for i in v] for t, v in doc.items.items()}
        apply_hop(doc, _mk_header(hop_id="H009", title="新标题"), [])
        self.assertEqual(doc.title, "新标题")
        self.assertEqual(doc.head_hop, "H009")
        after = {t: [i.item_id for i in v] for t, v in doc.items.items()}
        self.assertEqual(before, after)


# ---------- 12. L3 水位线 / 打分 / main 门禁（M3） ----------

from topicdock import (  # noqa: E402
    allocate_hop_id,
    delete_match_index,
    get_or_create_db,
    match_recall,
    match_topics,
    next_hop_seq_value,
    refresh_match_index,
    similar_topics,
    tokenize,
    tokenize_terms,
)


class TestTokenize(TopicDockTestCase):
    def test_mixed_tokens(self):
        tokens = tokenize("Auth网关架构JWT改造")
        self.assertIn("auth", tokens)
        self.assertIn("jwt", tokens)
        self.assertIn("网关", tokens)
        self.assertIn("关架", tokens)
        self.assertIn("架构", tokens)
        self.assertIn("改造", tokens)
        self.assertNotIn("网关架构", tokens)  # 长串只出二元组，不出全词

    def test_short_run_adds_whole_word(self):
        tokens = tokenize("网关")
        self.assertIn("网关", tokens)
        tokens = tokenize("网关auth")
        self.assertIn("网关", tokens)
        self.assertIn("auth", tokens)


class TestWatermark(TopicDockTestCase):
    def test_allocate_first_hop(self):
        store = make_store()
        ctx = StoreContext(store)
        conn = get_or_create_db(ctx)
        self.addCleanup(conn.close)
        self.assertEqual(allocate_hop_id(ctx, conn), "H001")
        self.assertEqual(ctx.meta_json()["max_hop_seq"], 1)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='next_hop_seq'"
        ).fetchone()
        self.assertEqual(row["value"], "2")

    def test_watermark_takes_max_of_all_sources(self):
        store = make_store()
        ctx = StoreContext(store)
        ctx.hops_dir.mkdir(parents=True, exist_ok=True)
        (ctx.hops_dir / "hop-007.md").write_text("x", encoding="utf-8")
        conn = get_or_create_db(ctx)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
            " next_seq_json, md_sha256) VALUES('t','T','H009','2026-01-01T00:00:00Z',"
            "'a','{}','x')"
        )
        conn.commit()
        self.assertEqual(next_hop_seq_value(ctx, conn), 10)
        self.assertEqual(allocate_hop_id(ctx, conn), "H010")
        self.assertEqual(ctx.meta_json()["max_hop_seq"], 10)

    def test_natural_growth_beyond_999(self):
        store = make_store()
        ctx = StoreContext(store)
        conn = get_or_create_db(ctx)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO meta(key, value) VALUES('next_hop_seq', '1000')")
        conn.commit()
        self.assertEqual(allocate_hop_id(ctx, conn), "H1000")


class TestMatchScoring(TopicDockTestCase):
    def _seed(self):
        store = make_store()
        ctx = StoreContext(store)
        conn = get_or_create_db(ctx)
        self.addCleanup(conn.close)
        doc1 = TopicDoc(topic_id="auth-jwt", title="认证模块 JWT 改造", next_seq={})
        doc1.items["C"] = [
            TopicItem(item_id="C1", track="C", statement="网关统一验签 EdDSA")
        ]
        doc1.latest = LatestHop(hop_id="H001", next_focus="吊销机制")
        doc2 = TopicDoc(topic_id="db-migration", title="数据库灰度迁移", next_seq={})
        doc2.items["Q"] = [
            TopicItem(item_id="Q1", track="Q", statement="双写窗口多长")
        ]
        doc2.latest = LatestHop(hop_id="H002", next_focus="回滚预案")
        for topic_id, doc, updated in (
            ("auth-jwt", doc1, "2026-11-10T00:00:00Z"),
            ("db-migration", doc2, "2026-11-12T00:00:00Z"),
        ):
            refresh_match_index(conn, topic_id, doc, "sha-" + topic_id)
            conn.execute(
                "INSERT INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
                " next_seq_json, md_sha256) VALUES(?,?,?,?,?,?,?)",
                (topic_id, doc.title, "H001", updated, "tester", "{}", "x"),
            )
        conn.commit()
        return conn

    def test_full_phrase_title_hit_enters_all(self):
        conn = self._seed()
        results = match_topics(conn, ["认证模块"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["topic_id"], "auth-jwt")
        self.assertEqual(results[0]["tier"], "all")
        self.assertEqual(results[0]["matched_terms"], len(tokenize("认证模块")))
        self.assertEqual(results[0]["score"], 100)  # 单条层内相对排名

    def test_topic_id_substring_hit_enters_all(self):
        conn = self._seed()
        results = match_topics(conn, ["th-jw"])
        self.assertEqual([entry["topic_id"] for entry in results], ["auth-jwt"])
        self.assertEqual(results[0]["tier"], "all")
        self.assertEqual(results[0]["matched_terms"], 2)

    def test_multi_phrase_substring_does_not_hijack_other_phrase(self):
        conn = self._seed()
        # "认证模块" 子串命中标题，但 "数据库" 未命中 → 不得整体拉进 all 层
        results = match_topics(conn, ["认证模块", "数据库"], tier="any")
        entry = next(row for row in results if row["topic_id"] == "auth-jwt")
        self.assertEqual(entry["tier"], "partial")
        self.assertLess(entry["matched_terms"], len(tokenize("认证模块 ") | tokenize("数据库")))

    def test_token_coverage_joins_all(self):
        conn = self._seed()
        results = match_topics(conn, ["网关统一验签"])
        self.assertEqual([entry["topic_id"] for entry in results], ["auth-jwt"])
        self.assertEqual(results[0]["tier"], "all")

    def test_unrelated_keywords_recall_nothing(self):
        conn = self._seed()
        self.assertEqual(match_topics(conn, ["完全不相关关键词"]), [])

    def test_tier_is_cumulative_floor(self):
        conn = self._seed()
        self.assertEqual(
            [entry["topic_id"] for entry in match_topics(conn, ["认证模块"], tier="all")],
            ["auth-jwt"],
        )
        self.assertEqual(
            [entry["topic_id"] for entry in match_topics(conn, ["认证模块"], tier="partial")],
            ["auth-jwt"],
        )
        self.assertEqual(
            [entry["topic_id"] for entry in match_topics(conn, ["认证模块"], tier="any")],
            ["auth-jwt"],
        )

    def test_recent_bypass(self):
        conn = self._seed()
        results = match_topics(conn, [], recent=True)
        self.assertEqual(
            [entry["topic_id"] for entry in results], ["db-migration", "auth-jwt"]
        )
        self.assertIsNone(results[0]["score"])
        self.assertNotIn("tier", results[0])
        self.assertNotIn("matched_terms", results[0])

    def test_match_recall_matched_sets(self):
        conn = self._seed()
        recall = match_recall(conn, tokenize("网关统一验签"))
        self.assertIn("auth-jwt", recall)
        self.assertEqual(recall["auth-jwt"]["matched"], tokenize("网关统一验签"))
        self.assertLess(recall["auth-jwt"]["bm25"], 0.0)


class TestMainInitializedGate(TopicDockTestCase):
    def test_pointer_to_missing_store_gated(self):
        target = self.tmp_root / "gone"
        pointer = Path.cwd() / ".agents" / "topicdock.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"store_dir": str(target)}), encoding="utf-8")
        code, data = run_cli_json(["list", "--json"])
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertEqual(data["status"], "UNINITIALIZED_STORE")

    def test_workspace_pointer_to_missing_store_gated(self):
        target = self.tmp_root / "gone-too"
        ws = self.tmp_root / "ws"
        ws.mkdir()
        pointer = ws / ".agents" / "topicdock.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"store_dir": str(target)}), encoding="utf-8")
        code, data = run_cli_json(
            ["list", "--workspace", str(ws), "--json"]
        )
        self.assertEqual(code, EXIT_UNINITIALIZED_STORE)
        self.assertEqual(data["status"], "UNINITIALIZED_STORE")


# ---------- M4: resolve 内核扩展 + QS-1 恒等与性能 ----------


class TestResolveKernel(TopicDockTestCase):
    """resolve 块同时新建结果条目（design §4.3(1) 状态转移矩阵）。"""

    def _doc_with_q1(self):
        doc = topicdock.TopicDoc(topic_id="t", title="t")
        ctx = topicdock.ApplyContext(hop_id="H001", new_seq={})
        topicdock.apply_hop_block(
            doc,
            topicdock.HopBlock(item_id="Q1", op="create", fields={"结论正文": "问题"}),
            ctx,
        )
        return doc, ctx

    def test_resolve_into_track_creates_item(self):
        doc, ctx = self._doc_with_q1()
        topicdock.apply_hop_block(
            doc,
            topicdock.HopBlock(
                item_id="C2", op="resolve", from_items=["Q1"], status="verified",
                origin="t:Q1@H001",
                fields={"结论正文": "结论", "一句话依据": "依据"},
            ),
            ctx,
        )
        self.assertEqual(doc.items["Q"], [])
        self.assertEqual(len(doc.items["C"]), 1)
        item = doc.items["C"][0]
        self.assertEqual(item.item_id, "C2")
        self.assertEqual(item.statement, "结论")
        self.assertEqual(item.evidence_status, "verified")
        self.assertEqual(item.basis, "依据")
        self.assertEqual(item.trace_records, ["H001"])
        self.assertEqual(ctx.new_seq["C"], 2)

    def test_resolve_into_none_removes_only(self):
        doc, ctx = self._doc_with_q1()
        topicdock.apply_hop_block(
            doc,
            topicdock.HopBlock(item_id="Q1", op="resolve", from_items=["Q1"]),
            ctx,
        )
        self.assertEqual(doc.items["Q"], [])
        self.assertEqual(doc.items["C"], [])
        self.assertNotIn("C", ctx.new_seq)

    def test_resolve_into_invalid_track_raises(self):
        """被篡改的 resolve 块（结果条目轨道非法）抛 ValueError 而非 KeyError。"""
        doc, ctx = self._doc_with_q1()
        with self.assertRaises(ValueError):
            topicdock.apply_hop_block(
                doc,
                topicdock.HopBlock(
                    item_id="X1", op="resolve", from_items=["Q1"],
                    fields={"结论正文": "x"},
                ),
                ctx,
            )


class TestQS1RebuildIdentity(TopicDockTestCase):
    """QS-1：冷重建 ↔ 纳管增量两路径六表恒等（D-P23）+ 100 主题/500 跳性能。"""

    def test_rich_store_rebuild_identity(self):
        store = make_store()
        build_rich_store(store)
        conn, _ = ensure_store_healthy(StoreContext(Path(store)))
        first = dump_tables(conn)
        self._assert_match_index_matches_topics(conn)
        conn.close()
        rebuild_db_from_markdown(StoreContext(Path(store)))
        conn = get_or_create_db(StoreContext(Path(store)))
        second = dump_tables(conn)
        self._assert_match_index_matches_topics(conn)
        conn.close()
        self.assertEqual(first, second)

    def test_merged_store_rebuild_identity(self):
        store = make_store()
        build_merged_store(store)
        conn, _ = ensure_store_healthy(StoreContext(Path(store)))
        first = dump_tables(conn)
        self._assert_match_index_matches_topics(conn)
        conn.close()
        rebuild_db_from_markdown(StoreContext(Path(store)))
        conn = get_or_create_db(StoreContext(Path(store)))
        second = dump_tables(conn)
        self._assert_match_index_matches_topics(conn)
        conn.close()
        self.assertEqual(first, second)
        # 已并流 src（gamma 无 md、无 topics 行）的派生行两路径一致（复审 #8）
        self.assertIn(("gamma", "C1", "H006", "create", 1), second["item_hops"])
        self.assertIn(("gamma", "Q1", "H006", "create", 1), second["item_hops"])
        # 死 src 不得写入 topic_fts（幽灵行守卫）
        fts_ids = [row[0] for row in second["topic_fts"]]
        self.assertEqual(fts_ids, sorted(set(fts_ids)))
        self.assertNotIn("gamma", fts_ids)

    def _assert_match_index_matches_topics(self, conn):
        """topic_fts.md_sha256 快照必须与 topics.md_sha256 逐主题恒等（design §5.3）。"""
        indexed = [
            tuple(row)
            for row in conn.execute(
                "SELECT topic_id, md_sha256 FROM topic_fts ORDER BY topic_id"
            )
        ]
        topics = [
            tuple(row)
            for row in conn.execute(
                "SELECT topic_id, md_sha256 FROM topics ORDER BY topic_id"
            )
        ]
        self.assertEqual(indexed, topics)

    def test_perf_100_topics_500_hops(self):
        """真实正文语料（40 主题、约 10~15KB/主题）冷重建 ≤ 500ms。"""
        store = make_store()
        materialize_store(store)
        started = time.perf_counter()
        rebuild_db_from_markdown(StoreContext(Path(store)))
        elapsed = time.perf_counter() - started
        conn = get_or_create_db(StoreContext(Path(store)))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 40
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hops").fetchone()[0], 40)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0], 40
            )
        finally:
            conn.close()
        self.assertLessEqual(elapsed, 0.5, f"重建耗时 {elapsed:.3f}s 超出 500ms 预算")
        print(f"[M2 perf] 真实正文 40 主题冷重建 {elapsed * 1000:.1f}ms")



# ---------- M3/D9: 查询词元上限 ----------


class TestMatchTopicsBatch(TopicDockTestCase):
    """MAX_QUERY_TOKENS 边界：L3 原语信任调用方，上限校验钉在 L5。"""

    def test_max_query_tokens_constant(self):
        self.assertEqual(topicdock.MAX_QUERY_TOKENS, 500)

    def test_l5_rejects_over_limit(self):
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["match", "--store-dir", str(store), "--json",
             "--topic", " ".join(f"z{index}" for index in range(501))]
        )
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("500", data.get("error", ""))

    def test_l5_rejects_empty_tokens(self):
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["match", "--topic", "!!!", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")


# ---------- M3/D9: _load_hop 单次读盘 / 换行归一 / _walk_origin_chain 跨步复用 ----------


class TestHopHotPathIo(TopicDockTestCase):
    def _ctx(self):
        ctx = StoreContext(self.tmp_root / "hop-io-store")
        ctx.hops_dir.mkdir(parents=True, exist_ok=True)
        return ctx

    def _header(self, hop_id="H001", **over):
        header = {
            "v": 1, "hop_id": hop_id, "topic_origin": "alpha",
            "title": "alpha 主题标题", "parents": [],
            "session_id": "ses-01", "created_at": "2026-11-01T00:00:00Z",
            "actor": "tester", "summary": "摘要", "next_focus": "",
            "artifacts": [],
        }
        header.update(over)
        return header

    def _write_hop(self, ctx, header, blocks=()):
        path = ctx.hops_dir / topicdock.hop_file_name(header["hop_id"])
        atomic_write_text(path, render_hop_md(HopDoc(header=header, blocks=list(blocks))))
        return path

    def test_load_hop_reads_disk_once_and_sha_matches_raw_bytes(self):
        ctx = self._ctx()
        path = self._write_hop(ctx, self._header())
        real_open = Path.open
        opens = {"n": 0}

        def _counting_open(self, *args, **kwargs):
            opens["n"] += 1
            return real_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", _counting_open):
            loaded = topicdock._load_hop(ctx, "H001")
        self.assertIsNotNone(loaded)
        self.assertEqual(opens["n"], 1)  # 单次读盘（基线 read_text + file_sha256 = 2）
        self.assertEqual(loaded.sha, file_sha256(path))

    def test_load_hop_normalizes_crlf_and_isolated_cr(self):
        ctx = self._ctx()
        blocks = [
            HopBlock(item_id="C1", op="create", status="verified",
                     fields={"结论正文": "换行容错"}),
        ]
        text = render_hop_md(HopDoc(header=self._header(), blocks=blocks))
        path = ctx.hops_dir / topicdock.hop_file_name("H001")
        for newline in ("\r\n", "\r"):
            path.write_bytes(text.replace("\n", newline).encode("utf-8"))
            loaded = topicdock._load_hop(ctx, "H001")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.hop.header["hop_id"], "H001")
            self.assertEqual([b.item_id for b in loaded.hop.blocks], ["C1"])
            self.assertEqual(loaded.sha, file_sha256(path))

    def test_walk_origin_chain_loads_each_hop_once(self):
        store = make_store()
        build_rich_store(store)
        ctx = StoreContext(Path(store))
        real_open = Path.open
        counts: dict[str, int] = {}

        def _counting_open(self, *args, **kwargs):
            key = str(self)
            counts[key] = counts.get(key, 0) + 1
            return real_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", _counting_open):
            chain, missing, invalid = topicdock._walk_origin_chain(
                ctx, "H004", "alpha"
            )
        self.assertIsNotNone(chain)
        self.assertIsNone(missing)
        self.assertIsNone(invalid)
        self.assertEqual(
            [loaded.hop.header["hop_id"] for loaded in chain],
            ["H001", "H002", "H003", "H004"],
        )
        for hop_path in sorted(ctx.hops_dir.glob("hop-00[1-4].md")):
            self.assertEqual(counts.get(str(hop_path), 0), 1)
