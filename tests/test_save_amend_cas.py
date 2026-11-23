"""M5 测试 —— cmd_save 写路径：CAS/失步仲裁、四步落盘、amend 原地合并与降级、
fork 分叉、ops 状态转移矩阵、D-P33 artifacts 流转、正向序列 rebuild 恒等、并发串行化。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.util import (
    ChainBuilder,
    TopicDockTestCase,
    build_rich_store,
    dump_tables,
    make_store,
    run_cli_json,
    run_save,
)

import topicdock
from topicdock import (
    StoreContext,
    ensure_store_healthy,
    get_or_create_db,
    live_refs,
    rebuild_db_from_markdown,
)


def _ctx(store):
    return StoreContext(Path(store))


def _conn(store):
    return get_or_create_db(_ctx(store))


def _new_topic(store, tmp, topic_id="alpha", session="ses-01", **extra):
    payload = {
        "topic_id": topic_id, "title": f"{topic_id} 主题标题", "base_hop": None,
        "base_sha": None, "session_id": session, "actor": "tester",
        "summary": "首轮建档", "next_focus": "推进下一阶段",
        "mutations": [
            {"op": "create", "track": "C", "statement": "核心结论甲",
             "evidence_status": "verified", "basis": "基准证据"},
            {"op": "create", "track": "R", "statement": "排除项乙", "basis": "原因明确"},
            {"op": "create", "track": "Q", "statement": "遗留问题丙"},
            {"op": "create", "track": "A", "state": "~", "statement": "启动行动丁",
             "basis": "待细化"},
        ],
    }
    payload.update(extra)
    code, data = run_save(store, payload, tmp)
    return code, data, payload


def _init_store_at(path):
    code, data = run_cli_json(
        ["init", "--scope", "local", "--path", str(path), "--json"]
    )
    if code != 0 or data.get("status") != "OK":
        raise AssertionError(f"init --path 失败: exit={code} payload={data!r}")
    return Path(data["store_dir"])


def _hop_header(store, hop_id):
    path = Path(store) / "hops" / topicdock.hop_file_name(hop_id)
    return topicdock.parse_hop_md(path.read_text(encoding="utf-8")).header


class TestSaveNewTopic(TopicDockTestCase):
    def test_first_save_creates_h001(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["head_hop"], "H001")
        self.assertFalse(data["amend"])
        self.assertEqual(
            data["created"], {"C": 1, "R": 1, "Q": 1, "A": 1}
        )
        self.assertRegex(
            data["echo"], r"^⚓ TopicDock: alpha \(H001@[0-9a-f]{6} 新建 \| "
            r"\+1\[C\] \+1\[R\] \+1\[Q\] \+1\[A\]\)$"
        )
        self.assertRegex(data["base_sha"], r"^[0-9a-f]{64}$")
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] 核心结论甲 — 依据(verified): 基准证据 (H001)", md)
        self.assertIn("- [alpha:R1] 排除项乙 — 原因: 原因明确 (H001)", md)
        self.assertIn("- [alpha:Q1] 遗留问题丙 (H001)", md)
        self.assertIn("- [~] [alpha:A1] 启动行动丁 — 断点: 待细化 (H001)", md)
        self.assertEqual(_ctx(store).meta_json()["max_hop_seq"], 1)
        conn = _conn(store)
        try:
            undo = conn.execute(
                "SELECT op_type, payload_json FROM undo_stack"
            ).fetchall()
            self.assertEqual([r["op_type"] for r in undo], ["save"])
            self.assertEqual(live_refs(conn, "H001"), 1)
        finally:
            conn.close()

    def test_base_hop_on_missing_topic_is_not_found(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "ghost", "base_hop": "H001", "base_sha": "x" * 64,
            "mutations": []}, self.tmp_root)
        self.assertEqual(code, 5)
        self.assertEqual(data["status"], "TOPIC_NOT_FOUND")

    def test_invalid_topic_id_or_payload(self):
        store = make_store()
        code, data = run_save(store, {"topic_id": "Bad_Id!", "mutations": []},
                              self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")


class TestSeparatorGuard(TopicDockTestCase):
    """P4 存期守卫：statement/basis/拼接边界不得内嵌完整条目分隔符（_normalize_mutations）。"""

    PATTERNS = (
        " — 依据(verified): ",
        " — 依据(inferred): ",
        " — 原因: ",
        " — 断点: ",
    )

    def _payload(self, mutation, topic_id="guard"):
        return {
            "topic_id": topic_id, "title": "守卫主题", "base_hop": None,
            "base_sha": None, "session_id": "s", "actor": "t", "summary": "首轮",
            "mutations": [mutation],
        }

    def _assert_rejected(self, store, payload):
        code, data = run_save(store, payload, self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("basis", data.get("error", ""))
        return data

    def test_statement_patterns_rejected(self):
        store = make_store()
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                self._assert_rejected(store, self._payload({
                    "op": "create", "track": "Q",
                    "statement": f"前文{pattern}伪 尾部",
                }))
        self.assertFalse((Path(store) / "topics" / "guard.md").exists())

    def test_basis_patterns_rejected(self):
        store = make_store()
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                self._assert_rejected(store, self._payload({
                    "op": "create", "track": "C", "evidence_status": "verified",
                    "statement": "正常结论", "basis": f"前文{pattern}伪 尾部",
                }))

    def test_boundary_basis_without_leading_space_rejected(self):
        # 无前导空格开头 basis 借结构分隔符尾空格形成高位假分隔符
        store = make_store()
        data = self._assert_rejected(store, self._payload({
            "op": "create", "track": "A", "state": "~",
            "statement": "行动正文", "basis": "— 断点: 待定",
        }))
        self.assertIn("A 轨", data.get("error", ""))

    def test_update_path_guarded_with_item_context(self):
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        data = self._assert_rejected(store, {
            "topic_id": "alpha", "base_hop": first["head_hop"],
            "base_sha": first["base_sha"], "session_id": "s2", "actor": "tester",
            "summary": "推进",
            "mutations": [{"op": "update", "item_id": "C1",
                           "statement": "更新 — 原因: 伪"}],
        })
        self.assertIn("C1", data.get("error", ""))

    def test_normal_payload_unaffected(self):
        store = make_store()
        statement = "结论 —— ； （ ） 「」 · 正常字符"
        basis = "依据 —— ； （ ） 「」 · 正常字符"
        code, data = run_save(store, self._payload({
            "op": "create", "track": "C", "evidence_status": "verified",
            "statement": statement, "basis": basis,
        }), self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        md_text = (Path(store) / "topics" / "guard.md").read_text(encoding="utf-8")
        self.assertIn(statement, md_text)
        self.assertIn(basis, md_text)
        self.assertNotIn("&#8212;", md_text)

    NEWLINE_BYPASSES = ("\n", "\r")

    def _assert_no_topic(self, store, topic_id):
        self.assertFalse((Path(store) / "topics" / f"{topic_id}.md").exists())
        conn = get_or_create_db(_ctx(store))
        try:
            row = conn.execute(
                "SELECT 1 FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_statement_newline_bypass_rejected(self):
        # 原始字符串无完整模式，压平后（渲染语义）才形成 → 仍须拒绝
        store = make_store()
        for index, newline in enumerate(self.NEWLINE_BYPASSES):
            with self.subTest(newline=repr(newline)):
                topic_id = f"nlstmt{index}"
                self._assert_rejected(store, self._payload({
                    "op": "create", "track": "Q",
                    "statement": f"问题{newline}— 原因: 伪",
                }, topic_id=topic_id))
                self._assert_no_topic(store, topic_id)

    def test_basis_newline_bypass_rejected(self):
        store = make_store()
        for index, newline in enumerate(self.NEWLINE_BYPASSES):
            with self.subTest(newline=repr(newline)):
                topic_id = f"nlbasis{index}"
                self._assert_rejected(store, self._payload({
                    "op": "create", "track": "C", "evidence_status": "verified",
                    "statement": "正常结论",
                    "basis": f"前文{newline}— 依据(verified): 伪",
                }, topic_id=topic_id))
                self._assert_no_topic(store, topic_id)

    def test_boundary_newline_bypass_rejected(self):
        # 压平后 basis 以空格开头，与结构分隔符尾空格拼接出完整模式 → 仍须拒绝
        store = make_store()
        for index, newline in enumerate(self.NEWLINE_BYPASSES):
            with self.subTest(newline=repr(newline)):
                topic_id = f"nlbnd{index}"
                self._assert_rejected(store, self._payload({
                    "op": "create", "track": "A", "state": "~",
                    "statement": "行动正文", "basis": f"{newline}— 断点: 待定",
                }, topic_id=topic_id))
                self._assert_no_topic(store, topic_id)


class TestPayloadWhitelist(TopicDockTestCase):
    """M4/P5：save 载荷按 op 精确白名单（_save_locked 入口、分支树之前、全路径生效）
    + state-on-create 文档化（A 轨 x/~/space、默认 space；非 A 轨 create/update 拒绝）。
    """

    def _advance(self, first, mutations):
        return {
            "topic_id": "alpha", "base_hop": first["head_hop"],
            "base_sha": first["base_sha"], "session_id": "ses-02",
            "summary": "推进", "mutations": mutations,
        }

    # ---- state-on-create 三例（A 轨 x / ~ / 默认 space） ----

    def test_state_on_create_x_tilde_and_default_space(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "首轮",
            "mutations": [
                {"op": "create", "track": "A", "state": "x", "statement": "已完成行动"},
                {"op": "create", "track": "A", "state": "~", "statement": "断点行动"},
                {"op": "create", "track": "A", "statement": "默认行动"},
            ]}, self.tmp_root)
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [x] [alpha:A1] 已完成行动 (H001)", md)
        self.assertIn("- [~] [alpha:A2] 断点行动 (H001)", md)
        self.assertIn("- [ ] [alpha:A3] 默认行动 (H001)", md)

    def test_state_on_create_non_a_track_rejected(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "首轮",
            "mutations": [
                {"op": "create", "track": "Q", "state": "x", "statement": "问题带状态"},
            ]}, self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("state", data.get("error", ""))
        self.assertFalse((Path(store) / "topics" / "alpha.md").exists())

    # ---- 白名单拒绝（mutation 级 / 顶层级 / update 非 A 轨 state） ----

    def test_mutation_level_unknown_key_rejected(self):
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data = run_save(store, self._advance(first, [
            {"op": "create", "track": "C", "statement": "白名单外结论",
             "priority": "high"},
        ]), self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("priority", data.get("error", ""))
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("白名单外结论", md)  # 零落盘

    def test_top_level_unknown_keys_all_listed(self):
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data = run_save(store, dict(
            self._advance(first, []), zebra=1, alpha_key=2), self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        error = data.get("error", "")
        self.assertIn("zebra", error)
        self.assertIn("alpha_key", error)  # 错误信息列出全部未知键名

    def test_update_non_a_track_state_rejected(self):
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data = run_save(store, self._advance(first, [
            {"op": "update", "item_id": "C1", "state": "x",
             "statement": "结论更新"},
        ]), self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("state", data.get("error", ""))
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("结论更新", md)

    def test_retract_common_keys_rejected(self):
        # retract 仅 op/item_id；公共键（statement 等）一律拒绝
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data = run_save(store, self._advance(first, [
            {"op": "retract", "item_id": "Q1", "statement": "撤回说明"},
        ]), self.tmp_root)
        self.assertEqual(code, 7)
        self.assertIn("statement", data.get("error", ""))

    # ---- 入口钉死：新建主题 / force-merge / STALE_BASE 早退路径同样拒绝 ----

    def test_unknown_key_via_new_topic_path_rejected(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "fresh", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "mutations": [
                {"op": "create", "track": "C", "statement": "新建结论", "bogus": 1},
            ]}, self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("bogus", data.get("error", ""))
        self.assertFalse((Path(store) / "topics" / "fresh.md").exists())

    def test_unknown_key_via_force_merge_path_rejected(self):
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        old_sha = first["base_sha"]
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-02", "summary": "另一窗口推进", "mutations": [
                {"op": "create", "track": "C", "statement": "他窗结论"}]},
            self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "mutations": [
                {"op": "create", "track": "C", "statement": "强制结论", "bogus": 1},
            ]}, self.tmp_root, "--force-merge")
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("bogus", data.get("error", ""))

    def test_unknown_key_rejected_before_stale_early_exit(self):
        """白名单校验先于 STALE_BASE 早退：失步载荷带未知键 → 7 而非 4。"""
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        old_sha = first["base_sha"]
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-02", "summary": "另一窗口推进", "mutations": [
                {"op": "create", "track": "C", "statement": "他窗结论"}]},
            self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "stale_probe": 1, "mutations": []},
            self.tmp_root)
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertIn("stale_probe", data.get("error", ""))


class TestSimilarTopicsHint(TopicDockTestCase):
    """M2：base_hop=null 新建 save 附 similar_topics 非阻断提示（fail-open）。"""

    def test_near_same_title_new_topic_hits_existing(self):
        """① 近同题 title 新建 → 含既有题 alpha（score ≥ 35，字段齐全）。"""
        store = make_store()
        code, first, _ = _new_topic(
            store, self.tmp_root, topic_id="alpha", title="auth jwt refactor plan"
        )
        self.assertEqual(code, 0)
        code, data, _ = _new_topic(
            store, self.tmp_root, topic_id="beta",
            title="auth jwt refactor plan review",
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [
            {"topic_id": "alpha", "title": "auth jwt refactor plan", "score": 60},
        ])

    def test_unrelated_title_returns_empty_list(self):
        """② 与既有题零共享词组的无关 title → []。"""
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data, _ = _new_topic(
            store, self.tmp_root, topic_id="beta", title="zzz qqq 777"
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [])

    def test_empty_store_first_creation_self_excluded(self):
        """③ 空库首次新建 → [] 且不含自身（自排除回归钉）。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [])

    def test_non_new_paths_omit_key(self):
        """④ 常规续跳 / 对既有题带有效 base_hop 的 --amend / --fork-as → 无键。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-02", "mutations": [
                {"op": "create", "track": "C", "statement": "续跳结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        self.assertNotIn("similar_topics", data)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": data["base_sha"],
            "session_id": "ses-02", "mutations": [
                {"op": "create", "track": "C", "statement": "amend 结论"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertNotIn("similar_topics", data)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": data["base_sha"],
            "session_id": "ses-03", "mutations": []},
            self.tmp_root, "--fork-as", "gamma")
        self.assertEqual(code, 0)
        self.assertNotIn("similar_topics", data)

    def test_match_topics_failure_fails_open(self):
        """⑤ match_topics 注入 RuntimeError → 退出码 0、字段完整、键缺省。"""
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        with mock.patch.object(
            topicdock, "match_topics",
            side_effect=RuntimeError("injected"),
        ):
            code, data, _ = _new_topic(
                store, self.tmp_root, topic_id="beta",
                title="auth jwt refactor plan",
            )
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["topic_id"], "beta")
        self.assertEqual(data["head_hop"], "H002")
        self.assertRegex(data["base_sha"], r"^[0-9a-f]{64}$")
        self.assertIn("echo", data)
        self.assertNotIn("similar_topics", data)
        self.assertTrue((Path(store) / "topics" / "beta.md").is_file())

    def test_key_present_existing_fields_unchanged(self):
        """⑥ 带键成功新建 → 既有字段与退出码逐一不变（显式断言）。"""
        store = make_store()
        code, first, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, data, _ = _new_topic(
            store, self.tmp_root, topic_id="beta",
            title="auth jwt refactor plan",
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["topic_id"], "beta")
        self.assertEqual(data["head_hop"], "H002")
        self.assertFalse(data["amend"])
        self.assertFalse(data["degraded"])
        self.assertEqual(data["created"], {"C": 1, "R": 1, "Q": 1, "A": 1})
        self.assertRegex(
            data["echo"], r"^⚓ TopicDock: beta \(H002@[0-9a-f]{6} 新建 \| "
            r"\+1\[C\] \+1\[R\] \+1\[Q\] \+1\[A\]\)$"
        )
        self.assertRegex(data["base_sha"], r"^[0-9a-f]{64}$")
        self.assertEqual(data["conflicts"], [])
        self.assertEqual(data["overwritten"], [])
        self.assertIn("similar_topics", data)

    def test_chinese_title_hit(self):
        """⑦ 中文 title 命中路径（bigram 覆盖率打分）。"""
        store = make_store()
        code, first, _ = _new_topic(
            store, self.tmp_root, topic_id="alpha", title="网关鉴权设计"
        )
        self.assertEqual(code, 0)
        code, data, _ = _new_topic(
            store, self.tmp_root, topic_id="beta", title="网关鉴权设计评审"
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [
            {"topic_id": "alpha", "title": "网关鉴权设计", "score": 54},
        ])


class TestSaveAdvance(TopicDockTestCase):
    def test_advance_creates_h002_with_evolved_tags(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        base_sha = data["base_sha"]
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": base_sha,
            "session_id": "ses-02", "actor": "tester", "summary": "推进",
            "mutations": [
                {"op": "update", "item_id": "A1", "state": "x",
                 "statement": "行动丁已完成"},
                {"op": "create", "track": "C", "statement": "次级结论",
                 "evidence_status": "inferred", "basis": "推导"},
            ]}, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(data["head_hop"], "H002")
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [x] [alpha:A1] 行动丁已完成 — 断点: 待细化 (H001→H002)", md)
        self.assertIn("- [alpha:C2] 次级结论 — 依据(inferred): 推导 (H002)", md)
        self.assertIn("- [alpha:C1] 核心结论甲 — 依据(verified): 基准证据 (H001)", md)
        conn = _conn(store)
        try:
            self.assertEqual(live_refs(conn, "H001"), 1)  # 仅 H001→H002 边
            self.assertEqual(live_refs(conn, "H002"), 1)  # heads
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM undo_stack").fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_missing_base_hop_rejected(self):
        store = make_store()
        _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "session_id": "s", "mutations": []},
            self.tmp_root)
        self.assertEqual(code, 7)

    def test_update_missing_item_not_found(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s", "mutations": [
                {"op": "update", "item_id": "C9", "statement": "不存在"}]},
            self.tmp_root)
        self.assertEqual(code, 5)
        self.assertEqual(data["status"], "ITEM_NOT_FOUND")


class TestAmend(TopicDockTestCase):
    def test_amend_merges_in_place(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "summary": "原地合并新增", "mutations": [
                {"op": "create", "track": "C", "statement": "amend 新结论",
                 "evidence_status": "verified", "basis": "amend 依据"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(data["amend"])
        self.assertFalse(data["degraded"])
        self.assertEqual(data["head_hop"], "H001")
        self.assertFalse((Path(store) / "hops" / "hop-002.md").exists())
        self.assertFalse((Path(store) / ".topicdock-amend-pending.json").exists())
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C2] amend 新结论 — 依据(verified): amend 依据 (H001)", md)
        conn = _conn(store)
        try:
            row = conn.execute(
                "SELECT updated_at FROM topics WHERE topic_id='alpha'"
            ).fetchone()
            hop = topicdock.parse_hop_md(
                (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
            )
            self.assertEqual(row["updated_at"], hop.header["created_at"])  # D-P10 唯一来源
            self.assertEqual(
                conn.execute(
                    "SELECT op_type FROM undo_stack ORDER BY undo_id DESC LIMIT 1"
                ).fetchone()["op_type"], "amend"
            )
        finally:
            conn.close()

    def test_amend_merge_rules_per_op(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        # create + update → 保持 create（合并字段）
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "create", "track": "C", "statement": "先创建",
                 "evidence_status": "verified"},
                {"op": "update", "item_id": "C2", "statement": "后更新"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        sha = d["base_sha"]  # amend 落盘后 md 变化，须取新 sha
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        )
        c2 = [b for b in hop.blocks if b.item_id == "C2"]
        self.assertEqual(len(c2), 1)
        self.assertEqual(c2[0].op, "create")
        self.assertEqual(c2[0].fields["结论正文"], "后更新")
        # create + overturn → 替换为 R<n>(op=create)
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "create", "track": "C", "statement": "短命结论"},
                {"op": "overturn", "from_item": "C3", "statement": "推翻改判",
                 "basis": "证据不足"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        sha = d["base_sha"]
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        )
        self.assertEqual([b for b in hop.blocks if b.item_id == "C3"], [])
        r2 = [b for b in hop.blocks if b.item_id == "R2"]
        self.assertEqual(len(r2), 1)
        self.assertEqual(r2[0].op, "create")
        self.assertEqual(r2[0].from_items, [])
        # create + resolve(→C) → 收敛为结果条目（分配号=下一可用：C3）
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "create", "track": "Q", "statement": "当跳即解决"},
                {"op": "resolve", "from_item": "Q2", "into_track": "C",
                 "statement": "解决结论", "evidence_status": "verified"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        sha = d["base_sha"]
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        )
        self.assertEqual([b for b in hop.blocks if b.item_id == "Q2"], [])
        c3 = [b for b in hop.blocks if b.item_id == "C3"]
        self.assertEqual(len(c3), 1)
        self.assertEqual(c3[0].op, "create")
        # 刚建的条目在下一轮 amend 中 retract → 从分片抵消删除
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "retract", "item_id": "C3"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        )
        self.assertEqual([b for b in hop.blocks if b.item_id == "C3"], [])

    def test_amend_session_mismatch_degrades(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "OTHER-session", "mutations": [
                {"op": "create", "track": "C", "statement": "降级结论"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertFalse(data["amend"])
        self.assertTrue(data["degraded"])
        self.assertEqual(data["head_hop"], "H002")
        self.assertIn("H001 已封板 → 自动新建 H002@", data["echo"])

    def test_amend_sealed_head_degrades(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        # 另一窗口 fork 走 head → live_refs(H001) 变 2
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "mutations": []},
            self.tmp_root, "--fork-as", "beta")
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "mutations": [
                {"op": "create", "track": "C", "statement": "封板后合并"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(data["degraded"])
        self.assertEqual(data["head_hop"], "H003")  # H002 已被 fork 占用


class TestCrossActorAmend(TopicDockTestCase):
    """D7：同 session 跨 actor 主体不一致 → amend 自动降级；空 actor 回填。"""

    _OMIT = object()

    def _amend(self, store, data, actor=_OMIT, session="ses-01"):
        payload = {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": session,
            "mutations": [{"op": "create", "track": "C", "statement": "增量结论"}],
        }
        if actor is not self._OMIT:
            payload["actor"] = actor
        return run_save(store, payload, self.tmp_root, "--amend")

    def test_same_session_different_actor_degrades(self):
        store = make_store()
        code, data, _ = _new_topic(
            store, self.tmp_root, actor="opencode/tech-lead"
        )
        code, data = self._amend(store, data, "opencode/architect")
        self.assertEqual(code, 0)
        self.assertFalse(data["amend"])
        self.assertTrue(data["degraded"])
        self.assertEqual(data["head_hop"], "H002")
        self.assertIn("H001 已封板 → 自动新建 H002@", data["echo"])
        self.assertEqual(_hop_header(store, "H001")["actor"], "opencode/tech-lead")
        self.assertEqual(_hop_header(store, "H002")["actor"], "opencode/architect")

    def test_same_actor_subject_merges_in_place(self):
        cases = [
            ("opencode/tech-lead", "opencode/tech-lead"),  # 完全一致
            ("opencode/tech-lead@legacy-model", "opencode/tech-lead"),  # 剥离旧后缀
            ("opencode/tech-lead", "opencode/tech-lead@new-model"),
        ]
        for index, (head_actor, payload_actor) in enumerate(cases):
            store = _init_store_at(self.tmp_root / f"same-actor-{index}")
            code, data, _ = _new_topic(store, self.tmp_root, actor=head_actor)
            self.assertEqual(code, 0)
            code, data = self._amend(store, data, payload_actor)
            self.assertEqual(code, 0, msg=f"{head_actor!r} -> {payload_actor!r}")
            self.assertTrue(data["amend"])
            self.assertFalse(data["degraded"])
            self.assertEqual(data["head_hop"], "H001")

    def test_null_actor_does_not_degrade_or_write_none(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, actor=None)
        code, data = self._amend(store, data, None)  # 载荷显式 null
        self.assertEqual(code, 0)
        self.assertTrue(data["amend"])
        self.assertFalse(data["degraded"])
        self.assertEqual(_hop_header(store, "H001")["actor"], "")
        hop_text = (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        self.assertNotIn('"actor":"None"', hop_text)

    def test_empty_head_actor_backfilled_and_rebuild_identity(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, actor=None)
        code, data = self._amend(store, data, "opencode/architect")
        self.assertEqual(code, 0)
        self.assertTrue(data["amend"])
        self.assertFalse(data["degraded"])
        self.assertEqual(data["head_hop"], "H001")
        self.assertEqual(_hop_header(store, "H001")["actor"], "opencode/architect")
        conn = _conn(store)
        before = dump_tables(conn)
        incremental_actor = conn.execute(
            "SELECT actor FROM hops WHERE hop_id='H001'"
        ).fetchone()["actor"]
        conn.close()
        self.assertEqual(incremental_actor, "opencode/architect")  # 追平同步写 actor
        code, _out = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = _conn(store)
        after = dump_tables(conn)
        rebuilt_actor = conn.execute(
            "SELECT actor FROM hops WHERE hop_id='H001'"
        ).fetchone()["actor"]
        conn.close()
        self.assertEqual(rebuilt_actor, incremental_actor)  # QS-1
        self.assertEqual(
            {k: v for k, v in before.items() if k != "meta"},
            {k: v for k, v in after.items() if k != "meta"},
        )


class TestStaleBase(TopicDockTestCase):
    def _advanced(self, store):
        code, data, _ = _new_topic(store, self.tmp_root)
        old_base = data["base_sha"]
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_base,
            "session_id": "ses-02", "summary": "窗口 B 推进", "mutations": [
                {"op": "create", "track": "C", "statement": "窗口 B 的新结论"}]},
            self.tmp_root)
        return old_base

    def test_stale_absorbable_then_retry(self):
        store = make_store()
        old_sha = self._advanced(store)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "mutations": [
                {"op": "create", "track": "C", "statement": "窗口 A 的新结论"}]},
            self.tmp_root)
        self.assertEqual(code, 4)
        self.assertEqual(data["status"], "STALE_BASE")
        self.assertEqual(data["current_head"], "H002")
        self.assertEqual(data["hops_after_base"], ["H002"])
        self.assertEqual(data["changed_items"], ["C2"])
        self.assertEqual(data["conflicts"], [])
        self.assertTrue(data["auto_absorbable"])
        # 换新 base 重发即吸收
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": data["current_sha"],
            "session_id": "ses-A", "mutations": [
                {"op": "create", "track": "C", "statement": "窗口 A 的新结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(data["head_hop"], "H003")

    def test_stale_conflict_detected(self):
        store = make_store()
        old_sha = self._advanced(store)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "mutations": [
                {"op": "update", "item_id": "C2", "statement": "触碰冲突条目"}]},
            self.tmp_root)
        self.assertEqual(code, 4)
        self.assertEqual(data["conflicts"], ["C2"])
        self.assertFalse(data["auto_absorbable"])

    def test_base_not_on_chain_invalid(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H099", "base_sha": data["base_sha"],
            "session_id": "s", "mutations": []}, self.tmp_root)
        self.assertEqual(code, 7)

    def test_base_sha_prefix_rule(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, ok = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"][:12],
            "session_id": "s", "mutations": [
                {"op": "create", "track": "Q", "statement": "前缀通过"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        code, stale = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": ok["base_sha"][:11],
            "session_id": "s", "mutations": []}, self.tmp_root)
        self.assertEqual(code, 4)  # <12 位不构成合法前缀

    def test_force_merge_last_write_wins(self):
        store = make_store()
        old_sha = self._advanced(store)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "mutations": [
                {"op": "update", "item_id": "C2", "statement": "强制覆盖"}]},
            self.tmp_root, "--force-merge")
        self.assertEqual(code, 0)
        self.assertEqual(
            data["overwritten"], [{"item_id": "C2", "by_hop": "H002"}]
        )
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("强制覆盖", md)

    def test_force_merge_vanished_reference_rejected(self):
        store = make_store()
        old_sha = self._advanced(store)
        conn = _conn(store)
        # 窗口 B 在 base 之后 retract 掉 C1
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H002",
            "base_sha": conn.execute(
                "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
            ).fetchone()["md_sha256"],
            "session_id": "ses-02", "mutations": [
                {"op": "retract", "item_id": "C1"}]}, self.tmp_root)
        conn.close()
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-A", "mutations": [
                {"op": "update", "item_id": "C1", "statement": "引用已消失条目"}]},
            self.tmp_root, "--force-merge")
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertEqual(data["vanished_items"], ["C1"])


class TestFork(TopicDockTestCase):
    def test_fork_from_head_with_cas(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "summary": "分叉 beta", "mutations": [
                {"op": "create", "track": "C", "statement": "分叉侧新结论"}]},
            self.tmp_root, "--fork-as", "beta")
        self.assertEqual(code, 0)
        self.assertEqual(data["topic_id"], "beta")
        self.assertEqual(data["head_hop"], "H002")
        conn = _conn(store)
        try:
            self.assertEqual(live_refs(conn, "H001"), 2)  # 自动封板
            beta = conn.execute(
                "SELECT * FROM topics WHERE topic_id='beta'"
            ).fetchone()
            self.assertEqual(beta["head_hop"], "H002")
        finally:
            conn.close()
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-002.md").read_text(encoding="utf-8")
        )
        self.assertEqual(hop.header["parents"], ["H001"])
        self.assertEqual(hop.header["topic_origin"], "beta")
        inherits = [b for b in hop.blocks if b.op == "inherit"]
        self.assertEqual(len(inherits), 4)  # C1/R1/Q1/A1 全量拷入（同 id）
        c1 = [b for b in inherits if b.item_id == "C1"][0]
        self.assertEqual(c1.origin, "alpha:C1@H001")  # origin=源条目初始创建跳
        md = (Path(store) / "topics" / "beta.md").read_text(encoding="utf-8")
        self.assertIn("- [beta:C1] 核心结论甲 — 依据(verified): 基准证据 (H002)", md)
        self.assertIn("- [beta:C2] 分叉侧新结论 (H002)", md)  # 继承后续编：C1 已占 → C2

    def test_fork_from_ancestor_without_sha(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        first_sha = data["base_sha"]
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": first_sha,
            "session_id": "ses-02", "summary": "先推进", "mutations": [
                {"op": "create", "track": "C", "statement": "主链后续结论"}]},
            self.tmp_root)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": "ignored",
            "session_id": "ses-03", "mutations": []},
            self.tmp_root, "--fork-as", "gamma")
        self.assertEqual(code, 0)  # 祖先 base 免 sha 校验（D-P25）
        md = (Path(store) / "topics" / "gamma.md").read_text(encoding="utf-8")
        self.assertIn("- [gamma:C1] 核心结论甲", md)
        self.assertNotIn("主链后续结论", md)  # 源 = base_hop 处的状态
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-003.md").read_text(encoding="utf-8")
        )
        self.assertEqual(hop.header["parents"], ["H001"])
        self.assertEqual(hop.header["title"], "alpha 主题标题")  # 继承 base_hop 头

    def test_fork_validations(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        base = {"topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
                "session_id": "s", "mutations": []}
        code, data = run_save(store, dict(base), self.tmp_root, "--fork-as", "Bad_Id")
        self.assertEqual(code, 7)
        code, data = run_save(store, dict(base), self.tmp_root, "--fork-as", "alpha")
        self.assertEqual(code, 7)  # 新 id 已存在
        code, data = run_save(store, dict(base), self.tmp_root,
                              "--fork-as", "beta", "--amend")
        self.assertEqual(code, 7)  # 互斥


class TestOpsMatrix(TopicDockTestCase):
    def _base_payload(self, base_hop, sha, mutations):
        return {
            "topic_id": "alpha", "base_hop": base_hop, "base_sha": sha,
            "session_id": "ses-02", "summary": "op 推进", "mutations": mutations,
        }

    def test_overturn_resolve_fold_retract(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        code, ok = run_save(store, self._base_payload("H001", sha, [
            {"op": "update", "item_id": "A1", "state": "x"},
            {"op": "create", "track": "A", "state": "x", "statement": "行动二"},
            {"op": "create", "track": "A", "state": "x", "statement": "行动三"},
        ]), self.tmp_root)
        self.assertEqual(code, 0)
        code, ok = run_save(store, self._base_payload("H002", ok["base_sha"], [
            {"op": "overturn", "from_item": "C1", "statement": "核心结论被推翻",
             "basis": "新证据推翻"},
            {"op": "resolve", "from_item": "Q1", "into_track": "C",
             "statement": "问题解决为新结论", "evidence_status": "verified",
             "basis": "解决依据"},
            {"op": "fold", "from_items": ["A1", "A2", "A3"],
             "statement": "三段行动归并为一"},
            {"op": "retract", "item_id": "R1"},
        ]), self.tmp_root)
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("- [alpha:C1]", md)      # 已推翻移出
        self.assertNotIn("- [alpha:Q1]", md)      # 已解决移出
        self.assertNotIn("- [alpha:R1]", md)      # 已撤回移出
        self.assertIn("(原立H001→推翻H003)", md)   # R2 短标
        self.assertIn("- [x] [alpha:A4] 三段行动归并为一 (H003)", md)
        conn = _conn(store)
        try:
            links = {
                (r["item_id"], r["link_type"], r["src_item_id"])
                for r in conn.execute("SELECT * FROM item_links")
            }
            self.assertIn(("R2", "overturn", "C1"), links)
            self.assertIn(("C2", "resolve", "Q1"), links)
            for src in ("A1", "A2", "A3"):
                self.assertIn(("A4", "fold", src), links)
        finally:
            conn.close()

    def test_op_validations(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        code, data = run_save(store, self._base_payload("H001", sha, [
            {"op": "overturn", "from_item": "A1", "statement": "非 C 轨"}]),
            self.tmp_root)
        self.assertEqual(code, 7)
        code, data = run_save(store, self._base_payload("H001", sha, [
            {"op": "resolve", "from_item": "C1", "into_track": "C"}]),
            self.tmp_root)
        self.assertEqual(code, 7)
        code, data = run_save(store, self._base_payload("H001", sha, [
            {"op": "fold", "from_items": ["A1"], "statement": "未完成不可折叠"}]),
            self.tmp_root)
        self.assertEqual(code, 7)  # A1 是 [~] 不是 [x]
        code, data = run_save(store, self._base_payload("H001", sha, [
            {"op": "create", "track": "C", "statement": "x",
             "evidence_status": "bogus"}]), self.tmp_root)
        self.assertEqual(code, 7)
        code, data = run_save(store, self._base_payload("H001", sha, [
            {"op": "wat", "item_id": "C1"}]), self.tmp_root)
        self.assertEqual(code, 7)


class TestArtifactsFlow(TopicDockTestCase):
    def test_inherit_null_clear_and_prune(self):
        store = make_store()
        real = self.tmp_root / "real-artifact.md"
        real.write_text("真实关联文件", encoding="utf-8")
        code, data, _ = _new_topic(store, self.tmp_root, artifacts=[
            {"name": "首个关联", "path": str(real), "note": "首轮"},
        ])
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- 首个关联: ", md)
        # 省略 → 继承 base_hop 头快照
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s", "mutations": []}, self.tmp_root)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- 首个关联: ", md)
        # null → 同省略
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": data["base_sha"],
            "session_id": "s", "artifacts": None, "mutations": []}, self.tmp_root)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- 首个关联: ", md)
        # 新列表 → 更新并剔除死链（死路径不在 md）
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H003", "base_sha": data["base_sha"],
            "session_id": "s", "artifacts": [
                {"name": "死链", "path": "/gone/nowhere.md", "note": ""},
                {"name": "活链", "path": str(real), "note": "仍在"},
            ], "mutations": []}, self.tmp_root)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("死链", md)
        self.assertIn("- 活链: ", md)
        # [] → 清空
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H004", "base_sha": data["base_sha"],
            "session_id": "s", "artifacts": [], "mutations": []}, self.tmp_root)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("活链", md)

    def test_amend_title_change_and_rebuild_identity(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "title": "修改后的新标题", "mutations": []},
            self.tmp_root, "--amend")
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn('title: "修改后的新标题"', md)
        conn = _conn(store)
        before = dump_tables(conn)
        conn.close()
        rebuild_db_from_markdown(_ctx(store))
        conn = _conn(store)
        after = dump_tables(conn)
        conn.close()
        self.assertEqual(before, after)


class TestSaveRebuildIdentity(TopicDockTestCase):
    def test_forward_sequence_matches_rebuild(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, artifacts=[
            {"name": "设计记录", "path": str(self.tmp_root / "design-x.md"),
             "note": "注"}
        ])
        (self.tmp_root / "design-x.md").write_text("x", encoding="utf-8")
        sha = data["base_sha"]
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "summary": "amend 推进", "mutations": [
                {"op": "update", "item_id": "A1", "state": "x"},
                {"op": "create", "track": "A", "state": "x", "statement": "行动二"},
                {"op": "create", "track": "A", "state": "x", "statement": "行动三"},
            ]}, self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        code, d3 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": d2["base_sha"],
            "session_id": "ses-03", "summary": "分叉", "mutations": []},
            self.tmp_root, "--fork-as", "beta")
        self.assertEqual(code, 0)
        code, d4 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": d2["base_sha"],
            "session_id": "ses-04", "summary": "推翻/解决/归并", "mutations": [
                {"op": "overturn", "from_item": "C1", "statement": "推翻改判",
                 "basis": "证据反转"},
                {"op": "resolve", "from_item": "Q1", "into_track": "R",
                 "statement": "问题被否定", "basis": "否定依据"},
                {"op": "fold", "from_items": ["A1", "A2", "A3"],
                 "statement": "归并行动"},
            ]}, self.tmp_root)
        self.assertEqual(code, 0)
        conn = _conn(store)
        forward = dump_tables(conn)
        conn.close()
        rebuild_db_from_markdown(_ctx(store))
        conn = _conn(store)
        rebuilt = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "meta"},
            {k: v for k, v in forward.items() if k != "meta"},
        )
        self.assertGreaterEqual(
            int(rebuilt["meta"]["next_hop_seq"]),
            int(forward["meta"]["next_hop_seq"]),
        )


class TestConcurrentSave(TopicDockTestCase):
    def test_two_processes_serialize_to_one_ok_one_stale(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        payload = json.dumps({
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "race", "summary": "并发写入", "mutations": [
                {"op": "create", "track": "C", "statement": "并发结论"}],
        }, ensure_ascii=False)
        paths = []
        for index in range(2):
            payload_path = self.tmp_root / f"race-{index}.json"
            payload_path.write_text(payload, encoding="utf-8")
            paths.append(payload_path)
        artifact = Path(__file__).resolve().parent.parent / "scripts" / "topicdock.py"
        procs = [
            subprocess.Popen(
                [
                    "/opt/homebrew/bin/python3.13", str(artifact),
                    "save", "--json-file", str(path),
                    "--store-dir", str(store), "--json",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for path in paths
        ]
        outputs = [proc.communicate() for proc in procs]
        codes = sorted(proc.returncode for proc in procs)
        self.assertEqual(codes, [0, 4])  # 排他锁串行化：一个 OK 一个 STALE
        conn = _conn(store)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE topic_id='alpha'"
                ).fetchone()[0], 1
            )
            head = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id='alpha'"
            ).fetchone()["head_hop"]
            self.assertIn(head, ("H002",))
        finally:
            conn.close()


class TestSaveHealGate(TopicDockTestCase):
    def test_sealed_ancestor_tamper_blocks_save(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s", "mutations": [
                {"op": "create", "track": "C", "statement": "封板前的推进"}]},
            self.tmp_root)
        # 篡改已封板祖先 H001 → 启动 heal 在 save 门内级联该主题
        hop_path = Path(store) / "hops" / "hop-001.md"
        hop_path.write_text(
            hop_path.read_text(encoding="utf-8").replace(
                '"summary":"首轮建档"', '"summary":"被篡改的摘要"'),
            encoding="utf-8",
        )
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002",
            "base_sha": "0" * 64, "session_id": "s", "mutations": []},
            self.tmp_root)
        self.assertEqual(code, 5)  # heal 级联后主题消失 → TOPIC_NOT_FOUND
        self.assertEqual(data["status"], "TOPIC_NOT_FOUND")


class TestCrossHopIdentity(TopicDockTestCase):
    def test_cross_hop_update_retract_rebuild_identity(self):
        """既有条目的跨跳 update/retract：seq_order 连续（全链基座）+ rebuild 恒等。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "s2", "summary": "推进", "mutations": [
                {"op": "update", "item_id": "A1", "state": "x"},
                {"op": "create", "track": "C", "statement": "次级结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        code, d3 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": d2["base_sha"],
            "session_id": "s3", "summary": "再推进", "mutations": [
                {"op": "update", "item_id": "A1", "statement": "行动丁推进到收尾"},
                {"op": "retract", "item_id": "Q1"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        conn = _conn(store)
        forward = dump_tables(conn)
        conn.close()
        self.assertIn(("alpha", "A1", "H002", "update", 2), forward["item_hops"])
        self.assertIn(("alpha", "A1", "H003", "update", 3), forward["item_hops"])
        self.assertIn(("alpha", "Q1", "H003", "retract", 2), forward["item_hops"])
        rebuild_db_from_markdown(_ctx(store))
        conn = _conn(store)
        rebuilt = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "meta"},
            {k: v for k, v in forward.items() if k != "meta"},
        )


class TestCrashInjection(TopicDockTestCase):
    """QS-2 崩溃注入：四步落盘各步间真实崩溃 → 下次自愈 100% 追平/回退。"""

    def _patch_atomic_crash(self, n, write_first):
        crash = {"count": 0}
        original = topicdock.atomic_write_text

        def wrapper(path, content):
            crash["count"] += 1
            if crash["count"] == n:
                if write_first:
                    original(path, content)
                raise RuntimeError(f"crash injected at write #{n}")
            original(path, content)

        topicdock.atomic_write_text = wrapper
        return original

    def test_new_topic_3_to_4_converges_via_adoption(self):
        store = make_store()
        original = self._patch_atomic_crash(3, write_first=True)  # meta=1 hop=2 md=3
        try:
            code, data, _ = _new_topic(store, self.tmp_root)
            self.assertEqual(code, 1)  # 崩溃被 L6 兜底映射为 ERROR
            self.assertEqual(data["status"], "ERROR")
        finally:
            topicdock.atomic_write_text = original
        # 崩溃后：md+分片在盘、DB 未提交 → 下次命令自愈纳管追平
        conn, _ = ensure_store_healthy(_ctx(store))
        first = dump_tables(conn)
        conn.close()
        clean_store = make_store()
        _new_topic(clean_store, self.tmp_root)
        conn = _conn(clean_store)
        clean = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in first.items() if k != "meta"},
            {k: v for k, v in clean.items() if k != "meta"},
        )

    def test_amend_2_to_3_converges_via_rollforward(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        original = self._patch_atomic_crash(3, write_first=False)  # pending=1 hop=2 md=3 不写
        try:
            code, crashed = run_save(store, {
                "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
                "session_id": "ses-01", "summary": "amend 后结论", "mutations": [
                    {"op": "create", "track": "C", "statement": "amend 后结论"}]},
                self.tmp_root, "--amend")
            self.assertEqual(code, 1)  # md 写入前崩：md 仍旧态
            self.assertEqual(crashed["status"], "ERROR")
        finally:
            topicdock.atomic_write_text = original
        conn, reports = ensure_store_healthy(_ctx(store))
        try:
            self.assertIn("amend_rollforward", [r["name"] for r in reports])
            md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("- [alpha:C2] amend 后结论", md)  # roll-forward 补完 md
            self.assertIn("(H001)", md)                     # 短标仍是 H001（原地合并）
            row = conn.execute(
                "SELECT md_sha256 FROM hops WHERE hop_id='H001'"
            ).fetchone()
            self.assertEqual(
                row["md_sha256"],
                topicdock.file_sha256(Path(store) / "hops" / "hop-001.md"),
            )
            self.assertFalse((Path(store) / ".topicdock-amend-pending.json").exists())
        finally:
            conn.close()

    def test_amend_3_to_4_converges_via_repair_then_rollforward(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        original = self._patch_atomic_crash(3, write_first=True)  # md 写后崩
        try:
            code, crashed = run_save(store, {
                "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
                "session_id": "ses-01", "summary": "3→4 窗口结论", "mutations": [
                    {"op": "create", "track": "C", "statement": "3→4 窗口结论"}]},
                self.tmp_root, "--amend")
            self.assertEqual(code, 1)  # DB 提交前崩：md/分片已新、DB 旧态
            self.assertEqual(crashed["status"], "ERROR")
        finally:
            topicdock.atomic_write_text = original
        conn, reports = ensure_store_healthy(_ctx(store))
        try:
            self.assertIn(
                "amend_rollforward", [r["name"] for r in reports]
            )
            md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("- [alpha:C2] 3→4 窗口结论", md)
            self.assertIn("(H001)", md)
            alpha = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id='alpha'"
            ).fetchone()
            self.assertEqual(alpha["head_hop"], "H001")
        finally:
            conn.close()


class TestMissingHeadDegrade(TopicDockTestCase):
    def test_missing_head_degrades_and_topic_stays_writable(self):
        """head 分片缺失（D-P20 断链跳过、md 完好）→ 自动降级新建，主题可写。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        (Path(store) / "hops" / "hop-001.md").unlink()
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s2", "summary": "降级推进", "mutations": [
                {"op": "create", "track": "C", "statement": "降级后的新结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        self.assertTrue(d2["degraded"])
        self.assertEqual(d2["head_hop"], "H002")
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] 核心结论甲", md)   # 基态条目保留（md=最后提交点）
        self.assertIn("- [alpha:C2] 降级后的新结论 (H002)", md)
        conn = _conn(store)
        try:
            row = conn.execute(
                "SELECT md_sha256 FROM hops WHERE hop_id='H001'"
            ).fetchone()
            self.assertEqual(row["md_sha256"], "MISSING")  # H001 置 MISSING 不级联
        finally:
            conn.close()

    def test_missing_head_two_consecutive_saves(self):
        """链上缺失跳（含 head 之下的祖先）连续两跳都可写：引擎自身状态不得自锁。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        (Path(store) / "hops" / "hop-001.md").unlink()
        payload = {
            "topic_id": "alpha", "base_hop": "H001", "session_id": "s2",
        }
        code, d2 = run_save(store, dict(payload, base_sha=sha, summary="第一跳", mutations=[
            {"op": "create", "track": "C", "statement": "第一跳结论"}]), self.tmp_root)
        self.assertEqual(code, 0)
        code, d3 = run_save(store, dict(
            payload, base_hop="H002", base_sha=d2["base_sha"],
            summary="第二跳", mutations=[
                {"op": "create", "track": "C", "statement": "第二跳结论"}]), self.tmp_root)
        self.assertEqual(code, 0)  # H002 之下的 H001 仍缺失 → 降级继续可写
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C2] 第一跳结论", md)
        self.assertIn("- [alpha:C3] 第二跳结论", md)

    def test_missing_head_fork_rejected_as_invalid(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        (Path(store) / "hops" / "hop-001.md").unlink()
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s", "mutations": []},
            self.tmp_root, "--fork-as", "beta")
        self.assertEqual(code, 7)  # 链上缺失 → INVALID_INPUT（7）而非 ValueError 抢跑
        self.assertEqual(d2["status"], "INVALID_INPUT")


class TestRegressionGuards(TopicDockTestCase):
    """N1/N3 回归锁定：重放先行零落盘、fold 合并守卫、载荷加固、计数口径。"""

    def test_amend_illegal_order_zero_disk_change(self):
        """[retract C1, update C1]：合并后重放失败 → INVALID_INPUT 且磁盘零改动。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        hop_path = Path(store) / "hops" / "hop-001.md"
        md_path = Path(store) / "topics" / "alpha.md"
        hop_before = hop_path.read_bytes()
        md_before = md_path.read_bytes()
        code, rejected = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "retract", "item_id": "C1"},
                {"op": "update", "item_id": "C1", "statement": "撤后更新"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 7)
        self.assertEqual(rejected["status"], "INVALID_INPUT")
        self.assertEqual(hop_path.read_bytes(), hop_before)      # 零改动
        self.assertEqual(md_path.read_bytes(), md_before)
        self.assertFalse((Path(store) / ".topicdock-amend-pending.json").exists())
        conn, _ = ensure_store_healthy(_ctx(store))             # 主题完好存活
        try:
            self.assertEqual(_conn_row(conn, "alpha")["head_hop"], "H001")
        finally:
            conn.close()

    def test_amend_fold_self_reference_succeeds_with_identity(self):
        """同跳已有 fold 的结果 A4，amend 再 fold from [A4]：另起新块，重放成功。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, mutations=[
            {"op": "create", "track": "A", "state": "x", "statement": "行动一"},
            {"op": "create", "track": "A", "state": "x", "statement": "行动二"},
            {"op": "fold", "from_items": ["A1", "A2"], "statement": "首轮归并"},
        ])
        sha = data["base_sha"]
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "summary": "再归并", "mutations": [
                {"op": "create", "track": "A", "state": "x", "statement": "新行动"},
                {"op": "fold", "from_items": ["A3", "A4"],
                 "statement": "二轮归并"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        conn = _conn(store)
        forward = dump_tables(conn)
        conn.close()
        rebuild_db_from_markdown(_ctx(store))
        conn = _conn(store)
        rebuilt = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "meta"},
            {k: v for k, v in forward.items() if k != "meta"},
        )

    def test_amend_on_missing_ancestor_degrades_and_preserves_state(self):
        """N4：own 链祖先缺失（head 完好）→ amend 禁止（残缺链重放会丢基态条目），
        同会话 amend 自动降级新建，md 基态条目全部保留。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        (Path(store) / "hops" / "hop-001.md").unlink()
        # 先降级保存一跳：head=H002、祖先 H001 缺失、md 完好
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-x", "summary": "降级推进", "mutations": [
                {"op": "create", "track": "C", "statement": "降级后的新结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        # 同会话 amend（H002 完好、session 匹配、live_refs==1）→ 必须仍降级新建
        code, d3 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": d2["base_sha"],
            "session_id": "ses-x", "summary": "再次原地合并", "mutations": [
                {"op": "create", "track": "C", "statement": "amend 侧新结论"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(d3["degraded"])
        self.assertEqual(d3["head_hop"], "H003")
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] 核心结论甲", md)  # 基态条目不丢（md=唯一提交点）
        self.assertIn("- [alpha:C2] 降级后的新结论", md)
        self.assertIn("- [alpha:C3] amend 侧新结论", md)

    def test_amend_fold_all_created_collapses_to_create(self):
        """from 全为本跳刚建条目：收敛为 create x（汇总正文不丢、不并入既有 fold）。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-01", "mutations": [
                {"op": "create", "track": "A", "state": "x", "statement": "短命一"},
                {"op": "create", "track": "A", "state": "x", "statement": "短命二"},
                {"op": "fold", "from_items": ["A2", "A3"],
                 "statement": "同跳折叠汇总"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertEqual(d["created"], {"A": 1})  # 净新增恰好 1 个 A
        hop = topicdock.parse_hop_md(
            (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        )
        collapsed = [
            b for b in hop.blocks
            if b.op == "create" and b.a_state == "x" and b.fields.get("结论正文") == "同跳折叠汇总"
        ]
        self.assertEqual(len(collapsed), 1)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn(f"- [x] [alpha:{collapsed[0].item_id}] 同跳折叠汇总", md)

    def test_amend_creates_counts_exclude_old_creating_ops(self):
        """N3：旧块中的 overturn/fold 结果不计入净新增（summary-only amend created={}）。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        sha = data["base_sha"]
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "s2", "summary": "推翻", "mutations": [
                {"op": "overturn", "from_item": "C1", "statement": "被推翻",
                 "basis": "证据"}]}, self.tmp_root)
        sha2 = None
        conn = _conn(store)
        try:
            sha2 = conn.execute(
                "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
            ).fetchone()["md_sha256"]
        finally:
            conn.close()
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": sha2,
            "session_id": "s2", "summary": "仅改摘要"}, self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertEqual(d["created"], {})  # R2 已在旧块中 → 不算净新增

    def test_payload_hardening_rejections(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        current_sha = data["base_sha"]
        # 非 dict mutation → 7（STALE 路径不 traceback）
        code, rejected = run_save(store, {
            "topic_id": "alpha", "base_hop": "H099", "base_sha": "0" * 64,
            "session_id": "s", "mutations": ["not-a-dict"]}, self.tmp_root)
        self.assertEqual(code, 7)
        # parent_hops 非 list → 7
        code, rejected = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": current_sha,
            "session_id": "s", "parent_hops": 5, "mutations": []}, self.tmp_root)
        self.assertEqual(code, 7)
        # --json-file 不存在 → 7
        argv = [
            "save", "--json-file", "/nonexistent/payload-x.json",
            "--store-dir", str(store), "--json",
        ]
        from tests.util import run_cli_json
        code, data = run_cli_json(argv)
        self.assertEqual(code, 7)

    def test_empty_title_keeps_current(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        code, d = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "s", "title": "", "mutations": []}, self.tmp_root)
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn('title: "alpha 主题标题"', md)  # 空串=保持，不改标题


def _conn_row(conn, topic_id):
    return conn.execute(
        "SELECT * FROM topics WHERE topic_id=?", (topic_id,)
    ).fetchone()


class TestSameHeadMismatchSemantics(TopicDockTestCase):
    def test_amend_sha_mismatch_not_auto_absorbable_then_force_merge(self):
        """同 head 失配=原地变化（amend）：不自动吸收（防静默覆盖），--force-merge 兜底。"""
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        old_sha = data["base_sha"]
        # 窗口 B amend 改变 md（sha 变化）
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "ses-01", "summary": "B 的原地合并", "mutations": [
                {"op": "update", "item_id": "C1", "statement": "B 改过的结论"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        # 窗口 A 持旧 sha 保存 → STALE 且不可自动吸收
        code, stale = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "A", "mutations": [
                {"op": "update", "item_id": "C1", "statement": "A 的结论"}]},
            self.tmp_root)
        self.assertEqual(code, 4)
        self.assertFalse(stale["auto_absorbable"])
        self.assertEqual(stale["reason"], "BASE_SHA_MISMATCH_HEAD")
        # --force-merge 以当前 head 为基重放（最后写入胜）
        code, merged = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": old_sha,
            "session_id": "A", "mutations": [
                {"op": "update", "item_id": "C1", "statement": "A 的结论"}]},
            self.tmp_root, "--force-merge")
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("A 的结论", md)


class TestStage2Guards(TopicDockTestCase):
    """Stage-2：多行载荷压平、frontmatter 守卫区间、install 链接接管语义。"""

    def test_multiline_statement_flattened_in_md(self):
        """statement/basis 含换行 → md 压平为单行（design §4.2(1)），roundtrip 成立。"""
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "1", "mutations": [
                {"op": "create", "track": "C", "statement": "第一行\n第二行",
                 "evidence_status": "verified", "basis": "依\n据"},
            ]}, self.tmp_root)
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] 第一行 第二行 — 依据(verified): 依 据 (H001)", md)
        # roundtrip：parse(render) == doc（单行无歧义）
        import topicdock as td
        doc = td.parse_topic_md(md)
        rendered = td.render_topic_md(doc, {"C1": "(H001)"})
        self.assertEqual(td.parse_topic_md(rendered).items["C"][0].statement, "第一行 第二行")

    def test_schema_version_line_in_body_does_not_lock_store(self):
        """正文行含 `schema_version: 2` 不触发版本守卫（只扫 frontmatter）。"""
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "about schema_version: 2",
            "mutations": [
                {"op": "create", "track": "C",
                 "statement": "讨论 TopicDock 的版本字段 schema_version: 2 的语义"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        # 后续命令正常（不被锁死 exit 6）
        code, listing = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(listing["total"], 1)

    def test_install_does_not_take_over_foreign_td_symlink(self):
        """install 不接管用户自有 td 符号链接（指向非 topicdock）；指向 topicdock 的更新。"""
        import subprocess
        bin_dir = Path.home() / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        foreign = bin_dir / "td"
        foreign_target = self.tmp_root / "my-own-td.sh"
        foreign_target.write_text("#!/bin/sh\necho mine\n")
        had_link = foreign.exists() or foreign.is_symlink()
        prev_target = foreign.resolve() if had_link else None
        if not had_link:
            foreign.symlink_to(foreign_target)
        try:
            install_path = (
                Path(__file__).resolve().parent.parent / "scripts" / "install.py"
            )
            proc = subprocess.run(
                ["/opt/homebrew/bin/python3.13", str(install_path),
                 "--client", "opencode", "--dest", str(self.tmp_root / "skills")],
                capture_output=True, text=True,
            )
            self.assertIn(proc.returncode, (0, 1))
            if not had_link:
                # 用户链接未被接管（仍指向自己的目标）
                self.assertTrue(foreign.is_symlink())
                self.assertEqual(foreign.resolve(), foreign_target.resolve())
            else:
                # 原有链接保留原目标（本轮未接管）
                self.assertEqual(foreign.resolve(), prev_target)
        finally:
            if not had_link:
                foreign.unlink(missing_ok=True)


class TestModelTraceField(TopicDockTestCase):
    """D6：跳头可选 model 的类型校验、strip 落盘、缺省零漂移、流转与冷重建。"""

    def test_model_non_str_rejected(self):
        store = make_store()
        for bad in (123, 1.5, True, [], {}, ["x"]):
            code, data, _ = _new_topic(
                store, self.tmp_root, topic_id=f"bad-{type(bad).__name__.lower()}",
                model=bad,
            )
            self.assertEqual(code, 7, msg=f"model={bad!r}")
            self.assertEqual(data["status"], "INVALID_INPUT")
        self.assertFalse((Path(store) / "hops" / "hop-001.md").exists())

    def test_model_stored_stripped(self):
        store = make_store()
        code, _data, _ = _new_topic(
            store, self.tmp_root, model="  opencode-go/gemini-4-argon  "
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            _hop_header(store, "H001")["model"], "opencode-go/gemini-4-argon"
        )

    def test_absent_model_bytes_and_sha_identical(self):
        store_a = _init_store_at(self.tmp_root / "store-a")
        store_b = _init_store_at(self.tmp_root / "store-b")
        store_c = _init_store_at(self.tmp_root / "store-c")
        payload = {
            "topic_id": "alpha", "title": "alpha 主题标题", "base_hop": None,
            "session_id": "ses-01", "actor": "tester", "summary": "首轮建档",
            "next_focus": "推进下一阶段",
            "mutations": [
                {"op": "create", "track": "C", "statement": "核心结论甲",
                 "evidence_status": "verified", "basis": "基准证据"},
            ],
        }
        with mock.patch.object(
            topicdock, "_utc_now", return_value="2026-11-01T00:00:00Z"
        ), mock.patch.object(
            topicdock.uuid, "uuid4", return_value=topicdock.uuid.UUID(int=0)
        ):
            self.assertEqual(run_save(store_a, dict(payload), self.tmp_root)[0], 0)
            self.assertEqual(
                run_save(store_b, {**payload, "model": None}, self.tmp_root)[0], 0
            )
            self.assertEqual(
                run_save(store_c, {**payload, "model": "   "}, self.tmp_root)[0], 0
            )
        hop_a = store_a / "hops" / "hop-001.md"
        hop_b = store_b / "hops" / "hop-001.md"
        hop_c = store_c / "hops" / "hop-001.md"
        self.assertNotIn('"model"', hop_a.read_text(encoding="utf-8"))
        self.assertEqual(hop_a.read_bytes(), hop_b.read_bytes())
        self.assertEqual(hop_a.read_bytes(), hop_c.read_bytes())
        self.assertEqual(
            topicdock.file_sha256(hop_a), topicdock.file_sha256(hop_b)
        )
        self.assertEqual(
            topicdock.file_sha256(hop_a), topicdock.file_sha256(hop_c)
        )

    def test_actor_null_or_blank_normalized_empty(self):
        store = make_store()
        code, _data, _ = _new_topic(store, self.tmp_root, topic_id="alpha", actor=None)
        self.assertEqual(code, 0)
        code, _data, _ = _new_topic(
            store, self.tmp_root, topic_id="beta", actor="   "
        )
        self.assertEqual(code, 0)
        for hop_id in ("H001", "H002"):
            self.assertEqual(_hop_header(store, hop_id)["actor"], "")
        hop_text = (Path(store) / "hops" / "hop-001.md").read_text(encoding="utf-8")
        self.assertNotIn('"actor":"None"', hop_text)

    def test_amend_updates_or_keeps_model(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, model="m1")
        self.assertEqual(code, 0)
        base = {
            "topic_id": "alpha", "base_hop": "H001", "session_id": "ses-01",
            "actor": "tester", "mutations": [],
        }
        code, data = run_save(
            store, {**base, "base_sha": data["base_sha"], "model": "  m2  "},
            self.tmp_root, "--amend",
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["amend"])
        self.assertEqual(_hop_header(store, "H001")["model"], "m2")
        code, data = run_save(
            store, {**base, "base_sha": data["base_sha"], "summary": "保持原值"},
            self.tmp_root, "--amend",
        )
        self.assertEqual(code, 0)
        self.assertEqual(_hop_header(store, "H001")["model"], "m2")
        code, data = run_save(
            store, {**base, "base_sha": data["base_sha"], "model": "   "},
            self.tmp_root, "--amend",
        )
        self.assertEqual(code, 0)
        self.assertEqual(_hop_header(store, "H001")["model"], "m2")

    def test_new_hops_do_not_inherit_model(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, model="m1")
        self.assertEqual(code, 0)
        h1_sha = data["base_sha"]
        # 常规新跳：父 H001 有 model，H002 缺省不继承
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": h1_sha,
            "session_id": "ses-02", "actor": "tester",
            "mutations": [{"op": "create", "track": "C", "statement": "次级结论"}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertNotIn("model", _hop_header(store, "H002"))
        # H003 显式带 model，供 fork / force-merge 验证「不继承父跳」
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": data["base_sha"],
            "session_id": "ses-03", "actor": "tester", "model": "m3",
            "mutations": [],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(_hop_header(store, "H003")["model"], "m3")
        # fork：父 H003 有 model，新主题首跳 H004 不继承
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H003", "base_sha": data["base_sha"],
            "session_id": "ses-04", "actor": "tester", "mutations": [],
        }, self.tmp_root, "--fork-as", "beta")
        self.assertEqual(code, 0)
        self.assertNotIn("model", _hop_header(store, "H004"))
        # force-merge：以旧 base H001 触发，新跳 H005 父 H003 有 model 但不继承
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": h1_sha,
            "session_id": "ses-05", "actor": "tester", "mutations": [],
        }, self.tmp_root, "--force-merge")
        self.assertEqual(code, 0)
        self.assertEqual(data["head_hop"], "H005")
        self.assertNotIn("model", _hop_header(store, "H005"))

    def test_model_store_doctor_rebuild_identity(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root, model="m1")
        self.assertEqual(code, 0)
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-02", "actor": "tester", "model": "m2",
            "mutations": [{"op": "create", "track": "C", "statement": "结论二"}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        conn = _conn(store)
        before = dump_tables(conn)
        conn.close()
        code, _out = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = _conn(store)
        after = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in before.items() if k != "meta"},
            {k: v for k, v in after.items() if k != "meta"},
        )


class TestMerkleHeader(TopicDockTestCase):
    """D8：六写点收口后的跳头 4 字段（topic_uid/parent_oids/origin_oids/hop_oid）。"""

    def _hop(self, store, hop_id):
        path = Path(store) / "hops" / topicdock.hop_file_name(hop_id)
        return topicdock.parse_hop_md(path.read_text(encoding="utf-8"))

    def _legacy_alpha(self):
        """建 rich 语料（H001–H006 全为 pre-D8 旧跳）并纳管入库。"""
        store = make_store()
        build_rich_store(store)
        ctx = StoreContext(Path(store))
        conn, _ = ensure_store_healthy(ctx)
        try:
            alpha = conn.execute(
                "SELECT head_hop, md_sha256 FROM topics WHERE topic_id='alpha'"
            ).fetchone()
        finally:
            conn.close()
        return store, alpha

    def test_new_topic_has_merkle_header(self):
        store = make_store()
        code, _data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        hop = self._hop(store, "H001")
        self.assertRegex(hop.header["topic_uid"], r"^[0-9a-f]{32}$")
        self.assertEqual(hop.header["parent_oids"], [])
        self.assertRegex(hop.header["hop_oid"], r"^v1:[0-9a-f]{64}$")
        self.assertEqual(hop.header["hop_oid"], topicdock.compute_hop_oid(hop))
        self.assertNotIn("origin_oids", hop.header)

    def test_second_hop_inherits_uid_and_parent_oids(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        uid = _hop_header(store, "H001")["topic_uid"]
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-02", "mutations": [
                {"op": "create", "track": "C", "statement": "第二跳结论"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(d2["head_hop"], "H002")
        h1, h2 = self._hop(store, "H001"), self._hop(store, "H002")
        self.assertEqual(h2.header["topic_uid"], uid)
        self.assertEqual(h2.header["parent_oids"], [topicdock.compute_hop_oid(h1)])
        self.assertEqual(h2.header["hop_oid"], topicdock.compute_hop_oid(h2))
        self.assertNotIn("origin_oids", h2.header)

    def test_amend_keeps_uid_and_refreshes_hop_oid(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        before = self._hop(store, "H001")
        uid = before.header["topic_uid"]
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-01", "summary": "amend 后",
            "mutations": [{"op": "create", "track": "C",
                           "statement": "amend 新结论"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(d2["amend"])
        after = self._hop(store, "H001")
        self.assertEqual(after.header["topic_uid"], uid)
        self.assertNotEqual(after.header["hop_oid"], before.header["hop_oid"])
        self.assertEqual(after.header["hop_oid"], topicdock.compute_hop_oid(after))
        self.assertEqual(after.header["parent_oids"], [])

    def test_legacy_chain_new_hop_derives_deterministic_uid(self):
        store, alpha = self._legacy_alpha()
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": alpha["head_hop"],
            "base_sha": alpha["md_sha256"], "session_id": "ses-new",
            "mutations": [{"op": "create", "track": "C",
                           "statement": "派生 uid 新跳"}]},
            self.tmp_root)
        self.assertEqual(code, 0)
        earliest = self._hop(store, "H001")
        expected = hashlib.sha256(
            b"topicdock-topic-uid:v1\0"
            + topicdock.compute_hop_oid(earliest).encode("utf-8")
        ).hexdigest()[:32]
        header = self._hop(store, data["head_hop"]).header
        self.assertEqual(header["topic_uid"], expected)
        self.assertEqual(header["parent_oids"], [topicdock.compute_hop_oid(
            self._hop(store, "H004"))])
        # 存量旧跳零后台改盘：4 字段均未被补写
        for hop_id in ("H001", "H002", "H003", "H004"):
            self.assertNotIn("topic_uid", self._hop(store, hop_id).header)

    def test_fork_from_legacy_source_fresh_uid_and_empty_origin_slot(self):
        store, alpha = self._legacy_alpha()
        code, data = run_save(store, {
            "topic_id": "alpha", "base_hop": alpha["head_hop"],
            "base_sha": alpha["md_sha256"], "session_id": "ses-fork",
            "mutations": []},
            self.tmp_root, "--fork-as", "delta")
        self.assertEqual(code, 0)
        fork = self._hop(store, data["head_hop"])
        self.assertRegex(fork.header["topic_uid"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            fork.header["parent_oids"],
            [topicdock.compute_hop_oid(self._hop(store, "H004"))],
        )
        # 源旧链无 uid → 外源槽位 ""；origin_hop_oid 照常实时重算
        self.assertEqual(
            fork.header["origin_oids"]["C2"],
            ":C2@" + topicdock.compute_hop_oid(self._hop(store, "H002")),
        )
        self.assertEqual(
            fork.header["origin_oids"]["C4"],
            ":C4@" + topicdock.compute_hop_oid(self._hop(store, "H004")),
        )
        self.assertEqual(fork.header["hop_oid"], topicdock.compute_hop_oid(fork))

    def test_amend_legacy_single_hop_derives_uid_from_pre_amend_state(self):
        store = make_store()
        builder = ChainBuilder(store, "legacy", "legacy 标题")
        builder.add_hop("H001", [], [
            topicdock.HopBlock(
                item_id="C1", op="create", status="verified",
                fields={"结论正文": "旧结论", "一句话依据": "旧依据"},
            ),
        ])
        builder.write_md()
        ctx = StoreContext(Path(store))
        conn, _ = ensure_store_healthy(ctx)
        try:
            row = conn.execute(
                "SELECT head_hop, md_sha256 FROM topics WHERE topic_id='legacy'"
            ).fetchone()
        finally:
            conn.close()
        old_hop = self._hop(store, "H001")
        self.assertNotIn("topic_uid", old_hop.header)
        expected = hashlib.sha256(
            b"topicdock-topic-uid:v1\0"
            + topicdock.compute_hop_oid(old_hop).encode("utf-8")
        ).hexdigest()[:32]
        code, data = run_save(store, {
            "topic_id": "legacy", "base_hop": "H001",
            "base_sha": row["md_sha256"], "session_id": "ses-01",
            "actor": "tester", "summary": "amend 旧跳",
            "mutations": [{"op": "create", "track": "C",
                           "statement": "amend 新增"}]},
            self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(data["amend"])
        after = self._hop(store, "H001")
        self.assertEqual(after.header["topic_uid"], expected)
        self.assertEqual(after.header["parent_oids"], [])
        self.assertEqual(after.header["hop_oid"], topicdock.compute_hop_oid(after))
        # 若在改后态才派生会得到不同 uid（锁定「先取旧态」顺序）
        post_derive = hashlib.sha256(
            b"topicdock-topic-uid:v1\0"
            + topicdock.compute_hop_oid(after).encode("utf-8")
        ).hexdigest()[:32]
        self.assertNotEqual(after.header["topic_uid"], post_derive)


class TestPublishedLedger(TopicDockTestCase):
    """D8：独立封板台账 —— amend 降级与封板零扰动（QS-6 度量）。"""

    def _oid(self, store, hop_id):
        path = Path(store) / "hops" / topicdock.hop_file_name(hop_id)
        hop = topicdock.parse_hop_md(path.read_text(encoding="utf-8"))
        return topicdock.compute_hop_oid(hop)

    def _seal(self, store, *oids):
        ctx = StoreContext(Path(store))
        with ctx.exclusive_lock():
            topicdock.mark_published_oids(ctx, list(oids))

    def _amend(self, store, data, statement):
        return run_save(store, {
            "topic_id": "alpha", "base_hop": data["head_hop"],
            "base_sha": data["base_sha"], "session_id": "ses-01",
            "mutations": [{"op": "create", "track": "C", "statement": statement}],
        }, self.tmp_root, "--amend")

    def test_unsealed_amend_in_place_then_sealed_degrades(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        code, in_place = self._amend(store, data, "未封板原地")
        self.assertEqual(code, 0)
        self.assertTrue(in_place["amend"])
        self.assertFalse(in_place["degraded"])
        self.assertEqual(in_place["head_hop"], "H001")
        self.assertTrue((Path(store) / "hops" / "hop-001.md").is_file())
        # 封板当前 head 的权威 oid 后再 amend → 自动降级新建下一跳
        self._seal(store, self._oid(store, "H001"))
        code, degraded = self._amend(store, in_place, "封板后降级")
        self.assertEqual(code, 0)
        self.assertFalse(degraded["amend"])
        self.assertTrue(degraded["degraded"])
        self.assertEqual(degraded["head_hop"], "H002")
        self.assertIn("H001 已封板 → 自动新建 H002@", degraded["echo"])

    def test_sealing_is_zero_byte_disturbance_and_no_cas_conflict(self):
        store = make_store()
        code, data, _ = _new_topic(store, self.tmp_root)
        self.assertEqual(code, 0)
        hop_path = Path(store) / "hops" / "hop-001.md"
        md_path = Path(store) / "topics" / "alpha.md"
        hop_before = hop_path.read_bytes()
        md_before = md_path.read_bytes()
        oid = self._oid(store, "H001")
        self._seal(store, oid)
        # 发布动作对 hop 分片与顶层 md 0 字节改写
        self.assertEqual(hop_path.read_bytes(), hop_before)
        self.assertEqual(md_path.read_bytes(), md_before)
        self.assertEqual(
            topicdock.read_published_oids(StoreContext(Path(store))),
            ({oid}, False),
        )
        # 同主题另一窗口持未变 head 的旧 base_sha 正常保存（0 CAS 伪冲突）
        code, advanced = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": data["base_sha"],
            "session_id": "ses-other", "mutations": [
                {"op": "create", "track": "C", "statement": "他窗推进"}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        self.assertEqual(advanced["head_hop"], "H002")

    def test_mark_merges_existing_and_writes_sorted_v1(self):
        store = make_store()
        ctx = StoreContext(Path(store))
        oid_a = "v1:" + "a" * 64
        oid_b = "v1:" + "b" * 64
        with ctx.exclusive_lock():
            topicdock.mark_published_oids(ctx, [oid_b])
            topicdock.mark_published_oids(ctx, [oid_a, oid_b])
        self.assertEqual(
            topicdock.read_published_oids(ctx), ({oid_a, oid_b}, False)
        )
        ledger = json.loads(
            (Path(store) / ".topicdock-published.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            ledger, {"v": 1, "published_oids": sorted([oid_a, oid_b])}
        )
