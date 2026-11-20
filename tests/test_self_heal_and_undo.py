"""M4 测试 —— 七步自愈序 ⑦⑥③①②⑤④ / 冷重建 / amend roll-forward / 关联探测。

undo（LIMIT 3 / journal 回放）由 M7 里程碑补齐；本文件覆盖 L4 自愈面：
D-P20 断链跳过与窄 roll-forward 窗口、D-P21 顺序与纳管判定、D-P33 快照还原、
D-P35 amend-pending 记录。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from unittest import mock

from tests.util import (
    ALPHA_TITLE,
    TopicDockTestCase,
    build_big_corpus,
    build_merged_store,
    build_rich_store,
    dump_tables,
    make_store,
    run_cli_json,
    run_save,
    write_hop,
)

import topicdock
from topicdock import (
    StoreContext,
    ensure_store_healthy,
    get_or_create_db,
    live_refs,
    pop_undo,
    probe_artifact_links,
)


def _ctx(store_root) -> StoreContext:
    return StoreContext(Path(store_root))


def _head_row(conn, topic_id):
    return conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()


def _rich_live_store():
    """rich 语料 + 已自愈 DB；返回 (store, builders)。"""
    store = make_store()
    builders = build_rich_store(store)
    conn, _ = ensure_store_healthy(_ctx(store))
    conn.close()
    return store, builders


def _delete_gamma_rows(conn) -> None:
    """模拟「3→4 崩溃窗口」后状态：md/分片在盘、DB 行整体缺失。"""
    conn.execute("DELETE FROM topics WHERE topic_id='gamma'")
    conn.execute("DELETE FROM hops WHERE hop_id='H006'")
    conn.execute("DELETE FROM item_hops WHERE topic_id='gamma'")
    conn.execute("DELETE FROM item_links WHERE topic_id='gamma'")
    conn.execute("DELETE FROM inverted_index WHERE topic_id='gamma'")
    conn.commit()


def _delete_beta_rows(conn) -> None:
    """模拟「fork 新建主题 md 已写、DB 未提交」：beta 全部 DB 行缺失。"""
    conn.execute("DELETE FROM topics WHERE topic_id='beta'")
    conn.execute("DELETE FROM hops WHERE hop_id='H005'")
    conn.execute("DELETE FROM hop_edges WHERE child_hop='H005'")
    conn.execute("DELETE FROM item_hops WHERE topic_id='beta'")
    conn.execute("DELETE FROM item_links WHERE topic_id='beta'")
    conn.execute("DELETE FROM inverted_index WHERE topic_id='beta'")
    topicdock.refresh_ref_counts(conn, ["H002"])
    conn.commit()


class TestHealRebuild(TopicDockTestCase):
    def test_missing_db_cold_rebuild(self):
        store = make_store()
        build_rich_store(store)
        conn, reports = ensure_store_healthy(_ctx(store))
        try:
            self.assertEqual(
                {r["topic_id"] for r in conn.execute("SELECT topic_id FROM topics")},
                {"alpha", "beta", "gamma"},
            )
            self.assertEqual(_head_row(conn, "alpha")["head_hop"], "H004")
            self.assertEqual(_head_row(conn, "beta")["head_hop"], "H005")
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")
            self.assertIn("rebuild_db", [r["name"] for r in reports])
        finally:
            conn.close()

    def test_corrupt_db_rebuild(self):
        store = make_store()
        build_rich_store(store)
        _ctx(store).db_path.write_bytes(b"this is not a sqlite database")
        conn, reports = ensure_store_healthy(_ctx(store))
        try:
            self.assertIn("rebuild_db", [r["name"] for r in reports])
            self.assertEqual(_head_row(conn, "alpha")["head_hop"], "H004")
        finally:
            conn.close()

    def test_empty_store_heal_ok(self):
        store = make_store()
        conn, _ = ensure_store_healthy(_ctx(store))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 0)
        finally:
            conn.close()


class TestMetaRepair(TopicDockTestCase):
    def test_stale_max_hop_seq_repaired(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        meta = ctx.meta_json()
        meta["max_hop_seq"] = 0
        ctx.write_meta(meta)
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertEqual(ctx.meta_json()["max_hop_seq"], 6)
            entry = [r for r in reports if r["name"] == "meta_json"][0]
            self.assertEqual(entry["status"], "repaired")
        finally:
            conn.close()

    def test_corrupt_meta_repaired(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        ctx.meta_path.write_text("{ broken json", encoding="utf-8")
        conn, _ = ensure_store_healthy(ctx)
        try:
            meta = ctx.meta_json()
            self.assertEqual(meta["schema_version"], 1)
            self.assertEqual(meta["max_hop_seq"], 6)
        finally:
            conn.close()


class TestAdoption(TopicDockTestCase):
    def test_crash_new_topic_adopted_with_identity(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        conn = get_or_create_db(ctx)
        first = dump_tables(conn)
        conn.close()
        conn = get_or_create_db(ctx)
        _delete_gamma_rows(conn)
        conn.close()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIn("adopt_crashed", [r["name"] for r in reports])
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")
            self.assertEqual(dump_tables(conn), first)  # 追平后与崩溃前完全一致
        finally:
            conn.close()

    def test_catch_up_adoption_matches_rebuild(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        # 模拟 3→4 崩溃窗口：新跳 H008 文件与 md 已写、DB 事务未提交
        builders["alpha"].add_hop("H008", ["H004"], [
            topicdock.HopBlock(item_id="C5", op="create", status="verified",
                fields={"结论正文": "中间件灰度策略定稿", "一句话依据": "灰度无回滚"})],
            created_at="2026-11-01T00:07:00Z", summary="H008 新增结论")
        builders["alpha"].write_md()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha")["head_hop"], "H008")
            self.assertEqual(live_refs(conn, "H004"), 1)  # heads 已让位，仅剩 H004→H008 边
            adoption = dump_tables(conn)
        finally:
            conn.close()
        topicdock.rebuild_db_from_markdown(ctx)
        conn = get_or_create_db(ctx)
        try:
            rebuilt = dump_tables(conn)
        finally:
            conn.close()
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "meta"},
            {k: v for k, v in adoption.items() if k != "meta"},
        )
        self.assertGreaterEqual(
            int(rebuilt["meta"]["next_hop_seq"]), int(adoption["meta"]["next_hop_seq"])
        )

    def test_tampered_md_not_adopted(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        conn = get_or_create_db(ctx)
        _delete_gamma_rows(conn)
        conn.close()
        gamma_md = builders["gamma"].md_path()
        gamma_md.write_text(
            gamma_md.read_text(encoding="utf-8").replace("压测脚本基准建立", "被篡改的正文"),
            encoding="utf-8",
        )
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "gamma"), None)  # 与重放不一致 = 篡改，绝不纳管
            self.assertFalse(gamma_md.exists())               # ① 清理野生破坏文件
            self.assertFalse((ctx.hops_dir / "hop-006.md").exists())  # ④ 孤儿分片清理
        finally:
            conn.close()

    def test_merge_completion_on_adoption(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        # 合流提交序 ①② 已完成（合流跳文件 + dst md 已写）、③ DB 未提交、④ src.md 未删
        builders["alpha"].add_hop("H007", ["H004", "H006"], [
            topicdock.HopBlock(item_id="C5", op="inherit", origin="gamma:C1@H006",
                fields={"结论正文": "压测脚本基准建立", "一句话依据": "bench 结果落库"}),
            topicdock.HopBlock(item_id="Q2", op="inherit", origin="gamma:Q1@H006",
                fields={"结论正文": "压测是否纳入 CI 门槛"})],
            created_at="2026-11-01T00:06:00Z", summary="并入 gamma 压测基准子题")
        builders["alpha"].write_md()
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha")["head_hop"], "H007")
            self.assertEqual(_head_row(conn, "gamma"), None)        # 补删 src 行
            self.assertFalse(builders["gamma"].md_path().exists())  # 补删 src.md
            self.assertEqual(live_refs(conn, "H006"), 1)           # 合流边引用，分片保留
            rows = conn.execute(
                "SELECT COUNT(*) FROM item_hops WHERE topic_id='gamma'"
            ).fetchone()[0]
            self.assertEqual(rows, 2)  # src 派生行保留（trace 递归依赖，复审 #8）
        finally:
            conn.close()


class TestWildCleanup(TopicDockTestCase):
    def test_wild_md_variants(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        topics_dir = ctx.topics_dir
        delta_md = topics_dir / "delta.md"
        delta_md.write_text(
            builders["gamma"].md_text().replace("topic_id: gamma", "topic_id: epsilon"),
            encoding="utf-8",
        )
        (topics_dir / "zombie.md").write_text("---\nnot closed", encoding="utf-8")
        odd = topics_dir / "Topic-Backup.md"  # 非规范命名（编辑器临时文件）→ 保留
        odd.write_text("editor temp", encoding="utf-8")
        (topics_dir / "x.md.tmp").write_text("tmp", encoding="utf-8")
        (ctx.hops_dir / "hop-999.md.tmp").write_text("tmp", encoding="utf-8")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertFalse(delta_md.exists())   # frontmatter id ≠ 文件名 → 删除
            self.assertFalse((topics_dir / "zombie.md").exists())  # 不可解析 → 删除
            self.assertTrue(odd.exists())
            self.assertFalse((topics_dir / "x.md.tmp").exists())   # 崩溃残留清扫
            self.assertFalse((ctx.hops_dir / "hop-999.md.tmp").exists())
        finally:
            conn.close()

    def test_merged_src_leftover_md_cleaned(self):
        store = make_store()
        builders = build_merged_store(store)
        ctx = _ctx(store)
        conn, _ = ensure_store_healthy(ctx)
        conn.close()
        # 合流 ③④ 窗口残留：src.md 回到盘上（DB 无 gamma 行、H006 被合流边引用）
        (ctx.topics_dir / "gamma.md").write_text(builders["gamma"].md_text(), encoding="utf-8")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "gamma"), None)  # live_refs≥1，绝不误纳管
            self.assertFalse((ctx.topics_dir / "gamma.md").exists())  # ① 清理残留
        finally:
            conn.close()


class TestMdRepair(TopicDockTestCase):
    def test_tampered_md_restored_including_snapshots(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        alpha_md = builders["alpha"].md_path()
        alpha_md.write_text(
            alpha_md.read_text(encoding="utf-8")
            .replace(ALPHA_TITLE, "被篡改的标题")
            .replace("网关架构设计记录", "被篡改的链接")
            .replace("Ed25519 公钥轮换接口定稿", "被篡改的结论"),
            encoding="utf-8",
        )
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIn("md_repair", [r["name"] for r in reports])
            expected = builders["alpha"].md_text()  # title/artifacts/## 3 从 head 快照还原（D-P33）
            self.assertEqual(alpha_md.read_text(encoding="utf-8"), expected)
            self.assertEqual(
                _head_row(conn, "alpha")["md_sha256"], topicdock.sha256_text(expected)
            )
        finally:
            conn.close()

    def test_missing_md_restored(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        alpha_md = builders["alpha"].md_path()
        alpha_md.unlink()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIn("md_repair", [r["name"] for r in reports])
            self.assertEqual(alpha_md.read_text(encoding="utf-8"), builders["alpha"].md_text())
        finally:
            conn.close()

    def test_broken_chain_with_broken_md_cascades(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        (ctx.hops_dir / "hop-003.md").unlink()  # own 链断裂
        alpha_md = builders["alpha"].md_path()
        alpha_md.write_text(
            alpha_md.read_text(encoding="utf-8").replace("公钥轮换接口定稿", "篡改轮换结论"),
            encoding="utf-8",
        )
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)  # 链不完整且 md 已坏 → 级联
            self.assertFalse(alpha_md.exists())
            self.assertFalse((ctx.hops_dir / "hop-004.md").exists())  # alpha 独占死跳
            self.assertNotEqual(_head_row(conn, "beta"), None)        # 共享祖先存活
            self.assertTrue((ctx.hops_dir / "hop-001.md").exists())
            self.assertTrue((ctx.hops_dir / "hop-002.md").exists())
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")  # 坏主题不阻塞好主题
        finally:
            conn.close()

    def test_missing_hop_with_intact_md_survives(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        (ctx.hops_dir / "hop-004.md").unlink()  # head 分片缺失，md 完好
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIsNotNone(_head_row(conn, "alpha"))  # D-P20 断链跳过，不级联
            sha = conn.execute(
                "SELECT md_sha256 FROM hops WHERE hop_id='H004'"
            ).fetchone()["md_sha256"]
            self.assertEqual(sha, "MISSING")
            self.assertTrue(builders["alpha"].md_path().exists())
        finally:
            conn.close()


class TestHopClassification(TopicDockTestCase):
    def test_sealed_ancestor_tamper_cascades_all_dependents(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        # 篡改共享祖先 H001：语法合法的追加块（内容失配 = 损坏，D-P20）
        write_hop(store, "H001", "alpha", [], [
            topicdock.HopBlock(item_id="C1", op="create", status="verified",
                fields={"结论正文": "外部网关统一签发 EdDSA 短效 JWT", "一句话依据": "压测 P99 延迟降至 1.2ms"}),
            topicdock.HopBlock(item_id="A1", op="create", a_state="~",
                fields={"结论正文": "完成网关鉴权原型压测", "断点": "待敲定回调签名头格式"}),
            topicdock.HopBlock(item_id="Q9", op="create", fields={"结论正文": "篡改追加的问题"}),
        ], title=ALPHA_TITLE, created_at="2026-11-01T00:00:00Z")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)  # own 链含 H001
            self.assertEqual(_head_row(conn, "beta"), None)   # 可达集含 H001（fork 后代）
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")  # 独立主题不受牵连
            self.assertFalse((ctx.hops_dir / "hop-001.md").exists())  # 损坏文件随级联清理
            self.assertFalse(builders["alpha"].md_path().exists())
            self.assertFalse(builders["beta"].md_path().exists())
            self.assertTrue(builders["gamma"].md_path().exists())
        finally:
            conn.close()

    def test_head_tamper_without_pending_cascades(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        write_hop(store, "H004", "alpha", ["H003"], [
            topicdock.HopBlock(item_id="A3", op="fold", from_items=["A1", "A2"],
                fields={"结论正文": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}),
            topicdock.HopBlock(item_id="C4", op="create", status="verified",
                fields={"结论正文": "被篡改的公钥轮换结论", "一句话依据": "篡改依据"}),
        ], title=ALPHA_TITLE, created_at="2026-11-01T00:03:00Z",
            artifacts=[("网关架构设计记录", "/abs/path/design.md", "定义 JWT 载荷字段与公钥轮换接口")],
            summary="篡改 head", next_focus="推进中间件开发")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)  # 无 pending 记录的失配 = 损坏（D-P35）
            self.assertFalse((ctx.hops_dir / "hop-004.md").exists())
            self.assertNotEqual(_head_row(conn, "beta"), None)
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")
        finally:
            conn.close()

    def test_amend_window_rollforward(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        # amend 原地合并语义：合并规则下刚 create 的 update 仍保持 op=create（全量快照更新）
        path, _ = write_hop(store, "H004", "alpha", ["H003"], [
            topicdock.HopBlock(item_id="A3", op="fold", from_items=["A1", "A2"],
                fields={"结论正文": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}),
            topicdock.HopBlock(item_id="C4", op="create", status="verified",
                fields={"结论正文": "Ed25519 公钥轮换接口定稿并评审通过", "一句话依据": "评审通过"}),
        ], title=ALPHA_TITLE, created_at="2026-11-01T00:09:00Z",
            artifacts=[("网关架构设计记录", "/abs/path/design.md", "定义 JWT 载荷字段与公钥轮换接口")],
            summary="amend 合并更新", next_focus="推进中间件开发")
        new_sha = topicdock.file_sha256(path)
        (ctx.root / ".topicdock-amend-pending.json").write_text(
            json.dumps({"hop_id": "H004", "expected_sha": new_sha}), encoding="utf-8"
        )
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIn("amend_rollforward", [r["name"] for r in reports])
            row = conn.execute("SELECT md_sha256 FROM hops WHERE hop_id='H004'").fetchone()
            self.assertEqual(row["md_sha256"], new_sha)   # 以文件为准
            alpha = _head_row(conn, "alpha")
            self.assertEqual(alpha["head_hop"], "H004")
            self.assertEqual(alpha["updated_at"], "2026-11-01T00:09:00Z")  # D-P10 唯一来源
            md_text = builders["alpha"].md_path().read_text(encoding="utf-8")
            self.assertIn("定稿并评审通过", md_text)
            self.assertIn(f"sha: `{new_sha[:12]}`", md_text)
            self.assertEqual(alpha["md_sha256"], topicdock.sha256_text(md_text))
            self.assertFalse((ctx.root / ".topicdock-amend-pending.json").exists())
        finally:
            conn.close()


class TestOrphanCleanup(TopicDockTestCase):
    def test_unreferenced_hop_file_deleted(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        write_hop(store, "H999", "ghost", [], [
            topicdock.HopBlock(item_id="C1", op="create",
                fields={"结论正文": "孤魂分片"})], created_at="2026-11-01T00:08:00Z")
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertFalse((ctx.hops_dir / "hop-999.md").exists())
            self.assertIn("orphan_cleanup", [r["name"] for r in reports])
        finally:
            conn.close()


class TestProbeArtifactLinks(TopicDockTestCase):
    def test_probe_ok_moved_missing(self):
        ws = self.workspace_dir
        (ws / "docs").mkdir()
        (ws / "docs" / "spec.md").write_text("x", encoding="utf-8")
        real = ws / "real.md"
        real.write_text("y", encoding="utf-8")
        doc = topicdock.TopicDoc(topic_id="t", title="t")
        doc.artifacts = [
            ("存在", str(real), None),
            ("移走", "/gone/path/spec.md", "note"),
            ("全无", "/gone/path/nothing.md", ""),
        ]
        result = probe_artifact_links(doc, ws)
        self.assertEqual(result[0]["status"], "ok")
        self.assertEqual(result[1]["status"], "moved")
        self.assertEqual(result[1]["new_path"], str(ws / "docs" / "spec.md"))
        self.assertEqual(result[2]["status"], "missing")

    def test_probe_without_workspace(self):
        doc = topicdock.TopicDoc(topic_id="t", title="t")
        doc.artifacts = [("x", "/gone/path/x.md", "")]
        result = probe_artifact_links(doc, None)
        self.assertEqual(result[0]["status"], "missing")


class TestForkCrashAdoption(TopicDockTestCase):
    def test_fork_crashed_new_adoption_keeps_source(self):
        """原缺陷场景复现：fork 新建主题 md 已写、DB 未提交（beta 全部行缺失）。

        head 即 fork 首跳 H005（单父 H002、origin=alpha）——旧实现会把
        fork 基点当合流 src 误删 alpha；双重守卫下必须保留源主题。
        """
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        conn = get_or_create_db(ctx)
        first = dump_tables(conn)
        conn.close()
        conn = get_or_create_db(ctx)
        _delete_beta_rows(conn)
        conn.close()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertIn("adopt_crashed", [r["name"] for r in reports])
            self.assertEqual(_head_row(conn, "beta")["head_hop"], "H005")   # 纳管恢复
            self.assertNotEqual(_head_row(conn, "alpha"), None)             # fork 基点存活
            self.assertTrue(builders["alpha"].md_path().exists())
            self.assertTrue((ctx.hops_dir / "hop-003.md").exists())
            self.assertTrue((ctx.hops_dir / "hop-004.md").exists())
            self.assertEqual(dump_tables(conn), first)                     # 六表完全复原
        finally:
            conn.close()

    def test_fork_catch_up_adoption_keeps_source(self):
        """fork 主题自身 origin 上的追平纳管同样不触碰源主题。"""
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        builders["beta"].add_hop("H008", ["H005"], [
            topicdock.HopBlock(item_id="Q1", op="create",
                fields={"结论正文": "分支侧新问题"})],
            created_at="2026-11-01T00:10:00Z")
        builders["beta"].write_md()
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "beta")["head_hop"], "H008")
            self.assertNotEqual(_head_row(conn, "alpha"), None)  # fork 源存活
            self.assertTrue(builders["alpha"].md_path().exists())
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")
        finally:
            conn.close()


class TestTamperedHeaders(TopicDockTestCase):
    """守卫遇到非 dict 头 / parents 非列表：走损坏路径，heal 不崩（R3）。"""

    def test_array_header_hop_cascades_without_crash(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        (ctx.hops_dir / "hop-004.md").write_text(
            "<!-- topicdock-hop: [1,2,3] -->\n\n# Hop H004 — alpha\n",
            encoding="utf-8",
        )
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)  # 不可解析 = 损坏级联
            self.assertNotEqual(_head_row(conn, "beta"), None)
            self.assertNotEqual(_head_row(conn, "gamma"), None)
        finally:
            conn.close()

    def test_non_list_parents_hop_cascades_without_crash(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        hop_path = ctx.hops_dir / "hop-004.md"
        hop_path.write_text(
            hop_path.read_text(encoding="utf-8").replace('"parents":["H003"]', '"parents":5'),
            encoding="utf-8",
        )
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)
            self.assertNotEqual(_head_row(conn, "beta"), None)
            self.assertNotEqual(_head_row(conn, "gamma"), None)
        finally:
            conn.close()


class TestPendingGuards(TopicDockTestCase):
    def _tampered_h004(self, store):
        return write_hop(store, "H004", "alpha", ["H003"], [
            topicdock.HopBlock(item_id="A3", op="fold", from_items=["A1", "A2"],
                fields={"结论正文": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}),
            topicdock.HopBlock(item_id="C4", op="create", status="verified",
                fields={"结论正文": "Ed25519 公钥轮换接口定稿并评审通过", "一句话依据": "评审通过"}),
        ], title=ALPHA_TITLE, created_at="2026-11-01T00:09:00Z",
            artifacts=[("网关架构设计记录", "/abs/path/design.md", "定义 JWT 载荷字段与公钥轮换接口")],
            summary="amend 合并更新", next_focus="推进中间件开发")

    def test_pending_mismatch_cascades(self):
        """pending 存在但 expected_sha 与文件哈希失配 → 一律损坏（D-P35 窄残余收口）。"""
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        self._tampered_h004(store)
        (ctx.root / ".topicdock-amend-pending.json").write_text(
            json.dumps({"hop_id": "H004", "expected_sha": "deadbeef"}), encoding="utf-8"
        )
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)
            self.assertFalse((ctx.hops_dir / "hop-004.md").exists())
            self.assertNotEqual(_head_row(conn, "beta"), None)
        finally:
            conn.close()

    def test_amend_window_md_ahead(self):
        """amend 3→4 窗口（md 已按新内容写、DB 未提交）：② 重放收口 md，⑤ 凭 pending 收口派生行。"""
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        path, _ = self._tampered_h004(store)
        new_sha = topicdock.file_sha256(path)
        md_path = builders["alpha"].md_path()
        md_ahead = (
            md_path.read_text(encoding="utf-8")
            .replace("Ed25519 公钥轮换接口定稿", "Ed25519 公钥轮换接口定稿并评审通过")
            .replace("2026-11-01T00:03:00Z", "2026-11-01T00:09:00Z")
        )
        md_ahead = re.sub(r"sha: `[0-9a-f]{12}`", f"sha: `{new_sha[:12]}`", md_ahead)
        md_path.write_text(md_ahead, encoding="utf-8")
        (ctx.root / ".topicdock-amend-pending.json").write_text(
            json.dumps({"hop_id": "H004", "expected_sha": new_sha}), encoding="utf-8"
        )
        conn, reports = ensure_store_healthy(ctx)
        try:
            names = [r["name"] for r in reports]
            self.assertIn("amend_rollforward", names)  # ⑤ 凭 pending 记录收口
            self.assertNotEqual(_head_row(conn, "alpha"), None)
            alpha = _head_row(conn, "alpha")
            self.assertEqual(alpha["updated_at"], "2026-11-01T00:09:00Z")
            hop_sha = conn.execute(
                "SELECT md_sha256 FROM hops WHERE hop_id='H004'"
            ).fetchone()["md_sha256"]
            self.assertEqual(hop_sha, new_sha)
            final_md = md_path.read_text(encoding="utf-8")
            self.assertIn("amend 合并更新", final_md)  # md 已按新 head 规范化
            self.assertEqual(alpha["md_sha256"], topicdock.sha256_text(final_md))
            self.assertFalse((ctx.root / ".topicdock-amend-pending.json").exists())
        finally:
            conn.close()

    def test_pending_residue_swept(self):
        """amend 完全持久化、仅 pending 删除崩溃 → 自愈清扫残留记录。"""
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        current_sha = topicdock.file_sha256(ctx.hops_dir / "hop-004.md")
        (ctx.root / ".topicdock-amend-pending.json").write_text(
            json.dumps({"hop_id": "H004", "expected_sha": current_sha}), encoding="utf-8"
        )
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertFalse((ctx.root / ".topicdock-amend-pending.json").exists())
            self.assertIn("pending_sweep", [r["name"] for r in reports])
            self.assertNotEqual(_head_row(conn, "alpha"), None)
        finally:
            conn.close()


class TestSchemaGuard(TopicDockTestCase):
    def test_newer_md_schema_rejected_without_destruction(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        md_path = builders["alpha"].md_path()
        md_path.write_text(
            md_path.read_text(encoding="utf-8").replace(
                "schema_version: 1", "schema_version: 2"),
            encoding="utf-8",
        )
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            ensure_store_healthy(ctx)
        self.assertTrue(md_path.exists())  # 高版本数据绝不被自愈销毁

    def test_newer_hop_version_rejected(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        hop_path = ctx.hops_dir / "hop-004.md"
        hop_path.write_text(
            hop_path.read_text(encoding="utf-8").replace('"v":1', '"v":2'),
            encoding="utf-8",
        )
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            ensure_store_healthy(ctx)
        self.assertTrue(hop_path.exists())

    def test_binary_md_wild_deleted_without_crash(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        (ctx.topics_dir / "zombie2.md").write_bytes(b"\xff\xfe\x00 binary garbage")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertFalse((ctx.topics_dir / "zombie2.md").exists())
            self.assertNotEqual(_head_row(conn, "alpha"), None)
        finally:
            conn.close()


class TestDamagedClosure(TopicDockTestCase):
    def test_garbage_shared_ancestor_cascades_both_dependents(self):
        """共享祖先文件为乱码：own 链视角会漏判 fork 后代，DB 闭包须把两者都级联。"""
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        (ctx.hops_dir / "hop-001.md").write_bytes(b"\x00\x01\x02 not utf8 \xff\xfe")
        conn, _ = ensure_store_healthy(ctx)
        try:
            self.assertEqual(_head_row(conn, "alpha"), None)
            self.assertEqual(_head_row(conn, "beta"), None)
            self.assertEqual(_head_row(conn, "gamma")["head_hop"], "H006")
            self.assertFalse((ctx.hops_dir / "hop-001.md").exists())
        finally:
            conn.close()


class TestHealIdempotence(TopicDockTestCase):
    def test_second_pass_is_noop(self):
        store, _ = _rich_live_store()
        ctx = _ctx(store)
        conn, _ = ensure_store_healthy(ctx)
        first = dump_tables(conn)
        conn.close()
        conn, reports = ensure_store_healthy(ctx)
        try:
            self.assertEqual(dump_tables(conn), first)
            repair_names = {
                r["name"] for r in reports
                if not (r["name"] == "meta_json" and r["status"] == "ok")
            }
            self.assertEqual(repair_names, set())  # 第二轮纯巡检，零修复零删除
        finally:
            conn.close()


# ===================== M6：L5 读路径命令（use / list / match / view） =====================


class TestUseCommand(TopicDockTestCase):
    def _rich(self):
        store = make_store()
        build_rich_store(store)
        return store

    def test_use_mounts_topic_with_credentials(self):
        """2C 凭证重挂载一致性：(head_hop, base_sha) 与 md frontmatter、DB 行完全一致。"""
        store = self._rich()
        code, data = run_cli_json(["use", "alpha", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["topic_id"], "alpha")
        md_text = (store / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertEqual(data["text"], md_text)
        self.assertEqual(data["head_hop"], "H004")
        doc = topicdock.parse_topic_md(md_text)
        self.assertEqual(data["head_hop"], doc.head_hop)  # 与 md frontmatter 一致
        self.assertRegex(data["base_sha"], r"^[0-9a-f]{64}$")  # 完整 64 hex
        conn = get_or_create_db(StoreContext(store))
        try:
            row = conn.execute(
                "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
            ).fetchone()
            self.assertEqual(data["base_sha"], row["md_sha256"])  # 与 DB 行一致
        finally:
            conn.close()
        # artifacts 三态之一：语料工件路径不存在且工作区无同名 → missing
        self.assertEqual(data["artifacts"], [
            {
                "name": "网关架构设计记录", "path": "/abs/path/design.md",
                "note": "定义 JWT 载荷字段与公钥轮换接口", "status": "missing",
            },
        ])

    def test_use_artifacts_ok_moved_missing(self):
        store = make_store()
        ws = Path.cwd()
        (ws / "keep.md").write_text("k", encoding="utf-8")
        (ws / "vanish.md").write_text("v", encoding="utf-8")
        sub = ws / "sub"
        sub.mkdir()
        (sub / "old.md").write_text("o", encoding="utf-8")
        payload = {
            "topic_id": "delta", "title": "工件探测", "base_hop": None,
            "mutations": [
                {"op": "create", "track": "C", "statement": "结论X", "basis": "依据Y"},
            ],
            "artifacts": [
                {"name": "keep", "path": str(ws / "keep.md"), "note": "n1"},
                {"name": "vanish", "path": str(ws / "vanish.md"), "note": ""},
                {"name": "moved", "path": str(sub / "old.md"), "note": ""},
            ],
        }
        code, saved = run_save(store, payload, self.tmp_root)
        self.assertEqual((code, saved["status"]), (0, "OK"))
        (ws / "vanish.md").unlink()
        (sub / "old.md").rename(ws / "old.md")
        code, data = run_cli_json(["use", "delta", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        arts = {a["name"]: a for a in data["artifacts"]}
        self.assertEqual(arts["keep"]["status"], "ok")
        self.assertEqual(arts["vanish"]["status"], "missing")
        self.assertEqual(arts["moved"]["status"], "moved")
        self.assertEqual(arts["moved"]["new_path"], str(ws / "old.md"))

    def test_use_index_mount_follows_list_order(self):
        store = self._rich()
        code, data = run_cli_json(["use", "1", "--store-dir", str(store), "--json"])
        self.assertEqual(data["topic_id"], "gamma")  # updated_at 倒序第 1 位
        code, data = run_cli_json(["use", "3", "--store-dir", str(store), "--json"])
        self.assertEqual(data["topic_id"], "alpha")
        code, data = run_cli_json(["use", "99", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (5, "TOPIC_NOT_FOUND"))
        code, data = run_cli_json(["use", "nosuch", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (5, "TOPIC_NOT_FOUND"))

    def test_use_no_selector_empty_store(self):
        store = make_store()
        code, data = run_cli_json(["use", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            data,
            {
                "status": "OK", "topic_id": None,
                "note": "store 尚无主题：先调 td init / 首次 save 建档",
            },
        )

    def test_use_no_selector_single_topic_mounts_directly(self):
        store = make_store()
        payload = {
            "topic_id": "delta", "title": "单主题直通", "base_hop": None,
            "mutations": [
                {"op": "create", "track": "C", "statement": "结论X", "basis": "依据Y"},
            ],
        }
        code, saved = run_save(store, payload, self.tmp_root)
        self.assertEqual((code, saved["status"]), (0, "OK"))
        code, data = run_cli_json(["use", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(data["topic_id"], "delta")  # 单主题直通挂载
        self.assertIn("text", data)

    def test_use_no_selector_multi_topics_lists_first_page(self):
        store = self._rich()  # ≥2 主题 → 第一页列表 + hint
        code, data = run_cli_json(["use", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(data["total"], 3)
        self.assertEqual([i["index"] for i in data["items"]], [1, 2, 3])
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 10)
        self.assertIn("序号", data["hint"])
        self.assertIn("--page", data["hint"])
        self.assertIn("--filter", data["hint"])


class TestListCommand(TopicDockTestCase):
    def _rich(self):
        store = make_store()
        build_rich_store(store)
        return store

    def test_list_default_page_order_and_indexes(self):
        store = self._rich()
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "OK")
        self.assertEqual((data["page"], data["page_size"], data["total"]), (1, 10, 3))
        self.assertEqual(
            [i["topic_id"] for i in data["items"]], ["gamma", "beta", "alpha"]
        )
        self.assertEqual([i["index"] for i in data["items"]], [1, 2, 3])
        updated = [i["updated_at"] for i in data["items"]]
        self.assertEqual(updated, sorted(updated, reverse=True))  # updated_at 倒序

    def test_list_page2_indexes_continue_from_11(self):
        store = make_store()
        build_big_corpus(store, n_topics=12, hops_per_topic=1)
        code, data = run_cli_json(
            ["list", "--page", "2", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["total"], 12)
        self.assertEqual([i["index"] for i in data["items"]], [11, 12])
        self.assertEqual([i["topic_id"] for i in data["items"]], ["t002", "t001"])

    def test_list_all_flag_expands(self):
        store = self._rich()
        code, data = run_cli_json(["list", "--all", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual((data["page"], data["page_size"], data["total"]), (1, 3, 3))
        self.assertEqual(len(data["items"]), 3)

    def test_list_filter_case_insensitive_substring(self):
        store = self._rich()
        code, data = run_cli_json(
            ["list", "--filter", "GAM", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(data["total"], 1)  # topic_id 大小写不敏感
        self.assertEqual(data["items"][0]["topic_id"], "gamma")
        code, data = run_cli_json(
            ["list", "--filter", "jwt", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(data["total"], 1)  # title 大小写不敏感（JWT → jwt）
        self.assertEqual(data["items"][0]["topic_id"], "alpha")

    def test_list_empty_store(self):
        store = make_store()
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(
            data,
            {"status": "OK", "page": 1, "page_size": 10, "total": 0, "items": []},
        )


class TestMatchCommand(TopicDockTestCase):
    def test_match_recall_full_text_cap_five(self):
        store = make_store()
        build_big_corpus(store, n_topics=12, hops_per_topic=1)
        code, data = run_cli_json(
            ["match", "--topic", "核心结论", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(data["results"]), 12)  # 12 个同构主题全部 ≥35 召回
        self.assertEqual({r["score"] for r in data["results"]}, {50})
        self.assertEqual(
            [r["topic_id"] for r in data["results"][:5]],
            ["t012", "t011", "t010", "t009", "t008"],  # 平分时 updated_at 倒序
        )
        for offset, entry in enumerate(data["results"]):
            if offset < 5:
                self.assertTrue(entry["full"])
                self.assertEqual(
                    entry["text"],
                    (store / "topics" / f"{entry['topic_id']}.md").read_text(
                        encoding="utf-8"
                    ),
                )
            else:
                self.assertFalse(entry["full"])
                self.assertNotIn("text", entry)

    def test_match_title_substring_and_no_result(self):
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["match", "--topic", "JWT", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        # alpha title 含 JWT（短语子串 3.0 → 75）；beta 继承的 C1 语句也含 jwt（token 2.0 → 50）
        self.assertEqual(
            [(r["topic_id"], r["score"]) for r in data["results"]],
            [("alpha", 75), ("beta", 50)],
        )
        self.assertTrue(data["results"][0]["full"])
        self.assertIn("text", data["results"][0])
        code, data = run_cli_json(
            ["match", "--topic", "zzznomatch", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(data, {"status": "OK", "results": []})  # 无结果非错误

    def test_match_recent_takes_latest_five(self):
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["match", "--topic", "x", "--recent", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["topic_id"] for r in data["results"]], ["gamma", "beta", "alpha"]
        )
        for entry in data["results"]:
            self.assertIsNone(entry["score"])
            self.assertTrue(entry["full"])
            self.assertIn("text", entry)

    def test_match_recent_without_topic(self):
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["match", "--recent", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["topic_id"] for r in data["results"]], ["gamma", "beta", "alpha"]
        )

    def test_match_tie_orders_topic_id_ascending(self):
        store = make_store()
        build_big_corpus(store, n_topics=6, hops_per_topic=1)
        conn, _ = ensure_store_healthy(_ctx(store))
        conn.execute("UPDATE topics SET updated_at='2026-11-09T00:00:00Z'")
        conn.commit()
        conn.close()
        expected = [f"t{index:03d}" for index in range(1, 7)]
        code, data = run_cli_json(
            ["match", "--topic", "核心结论", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual([r["topic_id"] for r in data["results"]], expected)
        code, data = run_cli_json(
            ["match", "--recent", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual([r["topic_id"] for r in data["results"]], expected[:5])

    def test_match_without_topic_or_recent_is_invalid(self):
        store = make_store()  # 必须在已 init 的库上跑（否则 UNINITIALIZED_STORE）
        code, data = run_cli_json(["match", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")


class TestViewCommand(TopicDockTestCase):
    def _rich(self):
        store = make_store()
        build_rich_store(store)
        return store

    def test_view_text_without_history(self):
        store = self._rich()
        code, data = run_cli_json(["view", "alpha", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(
            data["text"], (store / "topics" / "alpha.md").read_text(encoding="utf-8")
        )

    def test_view_history_head_first_time_descending(self):
        store = self._rich()
        code, data = run_cli_json(
            ["view", "alpha", "--history", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(
            [h["hop_id"] for h in data["history"]], ["H004", "H003", "H002", "H001"]
        )
        created = [h["created_at"] for h in data["history"]]
        self.assertEqual(created, sorted(created, reverse=True))  # 时间倒序
        self.assertEqual(data["history"][0]["summary"], "折叠 A1/A2，定稿公钥轮换")
        self.assertTrue(all(h["actor"] == "tester" for h in data["history"]))
        self.assertFalse(any("missing" in h for h in data["history"]))

    def test_view_fork_boundary_chain_is_complete(self):
        store = self._rich()
        code, data = run_cli_json(
            ["view", "beta", "--history", "--store-dir", str(store), "--json"]
        )
        # fork 边界：beta own 链仅 H005（父 H002 属 alpha，不在链内）
        self.assertEqual([h["hop_id"] for h in data["history"]], ["H005"])

    def test_view_missing_head_hop_break_marked(self):
        store = self._rich()
        conn, _ = ensure_store_healthy(StoreContext(store))  # 先建 DB（冷重建需完整链）
        conn.close()
        (store / "hops" / "hop-004.md").unlink()  # head 分片缺失（md 完好，D-P20 跳过）
        code, data = run_cli_json(
            ["view", "alpha", "--history", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))  # 缺失仍可列出
        self.assertEqual(
            data["history"], [{"hop_id": "H004", "missing": True, "note": "链中断"}]
        )

    def _save_with_model(self, model):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "alpha 主题标题", "base_hop": None,
            "session_id": "ses-01", "actor": "tester", "model": model,
            "mutations": [{"op": "create", "track": "C", "statement": "核心结论"}],
        }, self.tmp_root)
        self.assertEqual(code, 0)
        return store

    def test_view_history_outputs_model_when_present(self):
        store = self._save_with_model("opencode-go/gemini-4-argon")
        code, data = run_cli_json(
            ["view", "alpha", "--history", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(
            data["history"][0]["model"], "opencode-go/gemini-4-argon"
        )

    def test_view_history_omits_model_when_absent(self):
        store = self._rich()
        code, data = run_cli_json(
            ["view", "alpha", "--history", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertTrue(all("model" not in entry for entry in data["history"]))

    def test_view_history_tolerates_non_str_model(self):
        store = self._save_with_model("m1")
        hop_path = Path(store) / "hops" / "hop-001.md"
        text = hop_path.read_text(encoding="utf-8")
        self.assertIn('"model":"m1"', text)
        hop_path.write_text(text.replace('"model":"m1"', '"model":123'),
                            encoding="utf-8")
        # 删 DB 触发冷重建（不再按 sha 判篡改级联），验证 view 对非 str model 容错
        for suffix in ("", "-wal", "-shm"):
            db_path = Path(store) / f"topicdock.db{suffix}"
            if db_path.exists():
                db_path.unlink()
        code, data = run_cli_json(
            ["view", "alpha", "--history", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(data["history"][0]["hop_id"], "H001")
        self.assertNotIn("model", data["history"][0])


class TestUndoStackSemantics(TopicDockTestCase):
    """M7 undo：LIMIT 3、save/amend 还原、UNDO_BLOCKED 子跳检查、--force 边界、doctor。"""

    def _live(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "alpha 主题标题", "base_hop": None,
            "base_sha": None, "session_id": "ses-1", "actor": "t",
            "summary": "首轮", "mutations": [
                {"op": "create", "track": "C", "statement": "结论一",
                 "evidence_status": "verified", "basis": "依据一"},
                {"op": "create", "track": "A", "state": "x", "statement": "行动一"},
            ]}, self.tmp_root)
        return store, code, data

    def _conn(self, store):
        return get_or_create_db(_ctx(store))

    def test_undo_empty_stack_is_note(self):
        store = make_store()  # 全新库：无任何写入 → 栈空
        code, out = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertIn("回滚栈为空", out["note"])

    def test_undo_save_restores_prior_state_and_watermark(self):
        store, _, d1 = self._live()
        conn = self._conn(store)
        base_state = dump_tables(conn)  # H001 态快照
        conn.close()
        # 推进一跳（create C2）
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": d1["base_sha"],
            "session_id": "ses-2", "summary": "推进", "mutations": [
                {"op": "create", "track": "C", "statement": "结论二"}]},
            self.tmp_root)
        code, undone = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(undone["op_type"], "save")
        conn = self._conn(store)
        after = dump_tables(conn)
        conn.close()
        # 六表恒等口径（D-P23）：undo 是重建路径——meta 单调允许；
        # topics 的 next_seq_json/md_sha256 受水位 bump 影响单独断言
        self.assertEqual(after["hops"], base_state["hops"])          # H002 行已撤
        self.assertEqual(after["hop_edges"], base_state["hop_edges"])
        self.assertEqual(after["item_hops"], base_state["item_hops"])
        self.assertEqual(after["item_links"], base_state["item_links"])
        self.assertEqual(after["inverted_index"], base_state["inverted_index"])
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] 结论一", md)
        self.assertNotIn("结论二", md)  # C2 条目撤销
        # 撤销后再分配不复用 C2（水位不回退——D-P23 单调）
        conn = self._conn(store)
        sha = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
        ).fetchone()["md_sha256"]
        conn.close()
        code3, d3 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
            "session_id": "ses-3", "summary": "再推进", "mutations": [
                {"op": "create", "track": "C", "statement": "结论三"}]},
            self.tmp_root)
        self.assertEqual(code3, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C3] 结论三", md)  # 分配 C3（C2 已被撤销前保存用过，永不复用）

    def test_undo_blocked_by_child_reference(self):
        """D-P27：跳有后续存活子跳引用 → UNDO_BLOCKED（--force 也不放宽子跳检查）。"""
        store, _, d1 = self._live()
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": d1["base_sha"],
            "session_id": "ses-2", "summary": "推进", "mutations": [
                {"op": "create", "track": "C", "statement": "结论二"}]},
            self.tmp_root)  # → H002
        # H002 上再推一跳 H003（子跳）
        conn = self._conn(store)
        sha2 = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
        ).fetchone()["md_sha256"]
        conn.close()
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": sha2,
            "session_id": "ses-2", "summary": "再推进", "mutations": [
                {"op": "create", "track": "C", "statement": "结论三"}]},
            self.tmp_root)  # → H003
        # 撤掉栈顶（H003 安全——无子跳）
        run_cli_json(["undo", "--store-dir", str(store), "--json"])
        # 栈手术：直接把「save H002」条目提升为栈顶（模拟 H003 的 undo 记录丢失——
        # 此时 H003 已撤，改为在 H002 下重建一个存活子跳后验证拦截）
        # 实际构造：H003 撤销后 alpha 回 H002；fork beta 从 H002 → beta H004(子跳)；
        # 然后**删掉 fork 的栈条目**（模拟丢失）→ 下一条栈顶 = save H002，
        # 而 H004 是 H002 的存活子跳 → UNDO_BLOCKED
        conn = self._conn(store)
        sha_now = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
        ).fetchone()["md_sha256"]
        conn.close()
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H002", "base_sha": sha_now,
            "session_id": "ses-f", "summary": "分叉", "mutations": []},
            self.tmp_root, "--fork-as", "beta")
        conn = self._conn(store)
        fork_entry = conn.execute(
            "SELECT undo_id FROM undo_stack ORDER BY undo_id DESC LIMIT 1"
        ).fetchone()
        conn.execute("DELETE FROM undo_stack WHERE undo_id=?", (fork_entry["undo_id"],))
        conn.commit()
        conn.close()
        # 现在栈顶 = save H002（其跳有存活子跳 H004/beta 首跳）→ UNDO_BLOCKED
        code, blocked = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 7)
        self.assertEqual(blocked["status"], "INVALID_INPUT")
        self.assertIn("UNDO_BLOCKED", json.dumps(blocked, ensure_ascii=False))
        # --force 也不放宽子跳检查（D-P27）
        code, forced = run_cli_json(
            ["undo", "--force", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)
        self.assertIn("UNDO_BLOCKED", json.dumps(forced, ensure_ascii=False))

    def test_undo_limit_three(self):
        """LIMIT 3：1 save + 4 同会话 amend = 5 压栈 → 栈只留最近 3 条。"""
        store, _, d1 = self._live()
        sha = d1["base_sha"]
        for index in range(4):
            code, d = run_save(store, {
                "topic_id": "alpha", "base_hop": "H001", "base_sha": sha,
                "session_id": "ses-1", "summary": f"原地 {index+1}",
                "mutations": [
                    {"op": "create", "track": "Q",
                     "statement": f"问题{index+1}"}]},
                self.tmp_root, "--amend")
            self.assertEqual(code, 0)
            sha = d["base_sha"]  # amend 后 md 变化须取新 sha
        conn = self._conn(store)
        count = conn.execute("SELECT COUNT(*) FROM undo_stack").fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)  # 5 压栈 → LIMIT 3 留最近 3

    def test_doctor_reports_and_rebuild(self):
        store, _, _ = self._live()
        code, report = run_cli_json(
            ["doctor", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertIn("checks", report)
        # 健康库：除 meta_json 与 doctor 双巡检（须 ok）外零修复
        for check in report["checks"]:
            if check["name"] == "meta_json":
                self.assertEqual(check["status"], "ok")
            elif check["name"] in ("published_ledger", "hop_oid_drift"):
                self.assertEqual(check["status"], "ok")
            else:
                self.fail(f"健康库不应有修复项: {check}")
        conn = self._conn(store)
        before = dump_tables(conn)
        conn.close()
        code, rebuilt = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = self._conn(store)
        after = dump_tables(conn)
        conn.close()
        self.assertEqual(
            {k: v for k, v in after.items() if k != "meta"},
            {k: v for k, v in before.items() if k != "meta"},
        )

    def test_doctor_repairs_tampered_md(self):
        store, _, _ = self._live()
        md_path = Path(store) / "topics" / "alpha.md"
        original = md_path.read_text(encoding="utf-8")
        md_path.write_text(
            original.replace("结论一", "被篡改"), encoding="utf-8"
        )
        code, report = run_cli_json(
            ["doctor", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            md_path.read_text(encoding="utf-8"), original  # 重放修复还原
        )
        self.assertIn("md_repair", [r["name"] for r in report["checks"]])


class TestM7RegressionGuards(TopicDockTestCase):
    """M7 R1–R5 回归锁定：dry-run 预览、merge 撤号不复用、amend 子跳防御、journal 拒绝。"""

    def _conn(self, store):
        return get_or_create_db(_ctx(store))

    def _alpha_with(self, store, mutations, base="H001", sha=None, **kw):
        payload = {"topic_id": "alpha", "session_id": "s", "actor": "t",
                    "summary": kw.get("summary", "推进"), "mutations": mutations}
        if base is None:
            payload.update(base_hop=None, base_sha=None, title="alpha 主题标题")
        else:
            payload.update(base_hop=base, base_sha=sha)
        return run_save(store, payload, self.tmp_root, *kw.get("flags", ()))

    def test_delete_dry_run_is_preview_even_with_yes(self):
        """R1：--dry-run 一律预览（即便 --yes 也不执行）。"""
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(
            ["delete", "alpha", "--yes", "--dry-run",
             "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertIn("topics", data)      # 预览载荷
        self.assertNotIn("deleted", data)  # 未执行
        conn = self._conn(store)
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT * FROM topics WHERE topic_id='alpha'"
            ).fetchone())  # alpha 仍存活
        finally:
            conn.close()

    def test_undo_then_merge_does_not_reuse_revoked_number(self):
        """R2：undo 撤号后 merge 的续编分配不复用被撤号、md 水位不回退。"""
        store = make_store()
        code, d1 = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "C-one",
             "evidence_status": "verified", "basis": "b"}], base=None)
        # 建第二个主题
        code, d2 = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "g-one",
             "evidence_status": "verified", "basis": "b"}], base=None) if False else (None, None)
        run_save(store, {
            "topic_id": "gamma", "title": "gamma", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "gamma 建档", "mutations": [
                {"op": "create", "track": "C", "statement": "压测脚本基准建立",
                 "evidence_status": "verified", "basis": "bench 结果落库"},
            ]}, self.tmp_root)
        # alpha 推进（C2）
        conn = self._conn(store)
        sha = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
        ).fetchone()["md_sha256"]
        conn.close()
        code2, d2 = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "C-two"}], sha=sha)
        # 撤掉该跳（alpha 的 C2 编号被撤）
        run_cli_json(["undo", "--store-dir", str(store), "--json"])
        conn = self._conn(store)
        sha = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='alpha'"
        ).fetchone()["md_sha256"]
        gamma_sha = conn.execute(
            "SELECT md_sha256 FROM topics WHERE topic_id='gamma'"
        ).fetchone()["md_sha256"]
        conn.close()
        # merge gamma → alpha：继承块的续编不得复用 C2
        code3, d3 = run_cli_json(
            ["merge", "gamma", "alpha", "--store-dir", str(store), "--json",
             "--summary", "并入 gamma"]
        )
        self.assertEqual(code3, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("压测脚本基准建立", md)
        # 合流跳的继承块：新分配号（非 C2）
        for line in md.splitlines():
            if "压测脚本基准建立" in line:
                self.assertNotIn("[alpha:C2]", line)
        # md 水位不回退（≥ 撤前 C:3…至少不小于 bump 后值）
        conn = self._conn(store)
        try:
            watermarks = json.loads(conn.execute(
                "SELECT next_seq_json FROM topics WHERE topic_id='alpha'"
            ).fetchone()["next_seq_json"])
            self.assertGreaterEqual(int(watermarks.get("C", 0)), 3)
        finally:
            conn.close()

    def test_undo_amend_blocked_by_external_child(self):
        """R4：amend 的 head 跳被外部子跳引用（栈异常态防御）→ UNDO_BLOCKED。"""
        store = make_store()
        code, d1 = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "C-one"}], base=None)
        # 同会话 amend H001
        code, da = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "amended"}],
            sha=d1["base_sha"], summary="amend", flags=("--amend",))
        # fork beta from H001（beta 首跳=H001 的外部子跳）
        run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": da["base_sha"],
            "session_id": "sf", "summary": "分叉", "mutations": []},
            self.tmp_root, "--fork-as", "beta")
        # 栈手术：删掉 fork 条目 → 栈顶 = amend（head H001 有存活子跳）
        conn = self._conn(store)
        fork_id = conn.execute(
            "SELECT undo_id FROM undo_stack ORDER BY undo_id DESC LIMIT 1"
        ).fetchone()["undo_id"]
        conn.execute("DELETE FROM undo_stack WHERE undo_id=?", (fork_id,))
        conn.commit()
        conn.close()
        code, blocked = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 7)
        self.assertIn("UNDO_BLOCKED", json.dumps(blocked, ensure_ascii=False))
        # --force 也不放宽
        code, forced = run_cli_json(
            ["undo", "--force", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 7)
        self.assertIn("UNDO_BLOCKED", json.dumps(forced, ensure_ascii=False))

    def test_undo_journal_residual_rejected(self):
        """R3：journal 残留 → 入口拒绝（不静默覆盖未完成的回滚）。"""
        store = make_store()
        code, d1 = self._alpha_with(store, [
            {"op": "create", "track": "C", "statement": "C-one"}], base=None)
        (Path(store) / ".topicdock-undo-pending.json").write_text(
            json.dumps({"undo_id": 99, "op_type": "save", "payload": {}}),
            encoding="utf-8",
        )
        code, rejected = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 7)
        self.assertIn("journal", str(rejected))


class TestUndoRecoveryGuards(TopicDockTestCase):
    """M7 accept 附带三项：恢复段异常回压、keep 越界、post_hashes 失配/--force 放宽。"""

    def test_undo_restore_failure_pushes_entry_back(self):
        """恢复段（rebuild）抛错 → 条目回压栈顶 + journal 清理，不丢步骤。"""
        store = make_store()
        code, d1 = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "1", "mutations": [
                {"op": "create", "track": "C", "statement": "C1"}]}, self.tmp_root)
        conn = get_or_create_db(_ctx(store))
        before_count = conn.execute(
            "SELECT COUNT(*) FROM undo_stack").fetchone()[0]
        conn.close()
        original = topicdock.rebuild_db_from_markdown
        def boom(store_arg):
            raise RuntimeError("rebuild 崩溃注入")
        topicdock.rebuild_db_from_markdown = boom
        try:
            code, out = run_cli_json(["undo", "--store-dir", str(store), "--json"])
        finally:
            topicdock.rebuild_db_from_markdown = original
        # 异常被 L6 兜底映射为 ERROR(1)——条目已回压
        conn = get_or_create_db(_ctx(store))
        after_count = conn.execute(
            "SELECT COUNT(*) FROM undo_stack").fetchone()[0]
        conn.close()
        self.assertEqual(after_count, before_count)  # 条目不丢
        self.assertFalse((_ctx(store)).root.joinpath(
            ".topicdock-undo-pending.json").exists())  # journal 已清理

    def test_delete_keep_out_of_range_rejected(self):
        store = make_store()
        build_rich_store(store)
        code, out = run_cli_json(
            ["delete", "keep 99", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)
        code, out = run_cli_json(
            ["delete", "keep 0", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)

    def test_undo_post_hash_mismatch_blocked_and_force_relaxed(self):
        """post_hashes 失配 → UNDO_BLOCKED；--force 放宽哈希后完成还原。

        真实命令下 heal 先于 undo 修复篡改——此测试 monkeypatch heal 为透传，
        隔离出 undo 守卫自身的判定单元。
        """
        store = make_store()
        code, d1 = run_save(store, {
            "topic_id": "alpha", "title": "t", "base_hop": None, "base_sha": None,
            "session_id": "s", "actor": "t", "summary": "1", "mutations": [
                {"op": "create", "track": "C", "statement": "C1"}]}, self.tmp_root)
        code, d2 = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001", "base_sha": d1["base_sha"],
            "session_id": "s", "summary": "2", "mutations": [
                {"op": "create", "track": "C", "statement": "C2"}]}, self.tmp_root)
        # 篡改 md（post_hashes 失配）
        md_path = Path(store) / "topics" / "alpha.md"
        md_path.write_text(
            md_path.read_text(encoding="utf-8").replace("C2", "篡改C2"),
            encoding="utf-8",
        )
        original_heal = topicdock.ensure_store_healthy
        def passthrough(store_arg):
            conn = get_or_create_db(_ctx(store))
            return conn, []
        topicdock.ensure_store_healthy = passthrough
        try:
            code, blocked = run_cli_json(
                ["undo", "--store-dir", str(store), "--json"])
        finally:
            topicdock.ensure_store_healthy = original_heal
        self.assertEqual(code, 7)
        self.assertIn("哈希", json.dumps(blocked, ensure_ascii=False))
        # --force 放宽哈希后完成还原（恢复段把 md 重写为 pre 状态——篡改丢弃）
        topicdock.ensure_store_healthy = passthrough
        try:
            code, forced = run_cli_json(
                ["undo", "--force", "--store-dir", str(store), "--json"])
        finally:
            topicdock.ensure_store_healthy = original_heal
        self.assertEqual(code, 0)
        md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("- [alpha:C1] C1", md)
        self.assertNotIn("C2", md)


class TestPublishedLedgerGuards(TopicDockTestCase):
    """D8：封板台账 undo 恒拦截 / 损坏失败关闭 / doctor 双巡检。"""

    def _live(self):
        store = make_store()
        code, data = run_save(store, {
            "topic_id": "alpha", "title": "alpha 主题标题", "base_hop": None,
            "base_sha": None, "session_id": "ses-1", "actor": "t",
            "summary": "首轮", "mutations": [
                {"op": "create", "track": "C", "statement": "结论一",
                 "evidence_status": "verified", "basis": "依据一"},
            ]}, self.tmp_root)
        self.assertEqual(code, 0)
        return store, data

    def _oid(self, store, hop_id):
        path = Path(store) / "hops" / topicdock.hop_file_name(hop_id)
        return topicdock.compute_hop_oid(
            topicdock.parse_hop_md(path.read_text(encoding="utf-8"))
        )

    def _seal(self, store, *oids):
        ctx = _ctx(store)
        with ctx.exclusive_lock():
            topicdock.mark_published_oids(ctx, list(oids))

    def _checks(self, store):
        code, report = run_cli_json(
            ["doctor", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        return {check["name"]: check for check in report["checks"]}

    def test_undo_sealed_blocked_even_force_then_unsealed_ok(self):
        store, _data = self._live()
        self._seal(store, self._oid(store, "H001"))
        for flags in ([], ["--force"]):
            code, blocked = run_cli_json(
                ["undo", *flags, "--store-dir", str(store), "--json"]
            )
            self.assertEqual(code, 7)
            self.assertEqual(blocked["status"], "INVALID_INPUT")
            text = json.dumps(blocked, ensure_ascii=False)
            self.assertIn("UNDO_BLOCKED", text)
            self.assertIn("已发布封板跳不可回滚", text)
        # 拦截发生在 journal 写之前，栈弹出随事务回滚恢复 → 解封后照常回滚
        (Path(store) / ".topicdock-published.json").unlink()
        code, undone = run_cli_json(
            ["undo", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(undone["op_type"], "save")

    def test_corrupt_ledger_fails_closed_for_amend_undo_and_doctor(self):
        variants = [
            "{not-json",
            json.dumps({"v": 2, "published_oids": []}),
            json.dumps({"v": True, "published_oids": []}),
            json.dumps({"v": 1, "published_oids": "v1:" + "0" * 64}),
            json.dumps({"v": 1, "published_oids": [1]}),
        ]
        for index, content in enumerate(variants):
            with self.subTest(variant=index):
                code, init = run_cli_json([
                    "init", "--scope", "local", "--path",
                    str(self.tmp_root / f"ledger-{index}"), "--json",
                ])
                self.assertEqual(code, 0)
                store = Path(init["store_dir"])
                code, data = run_save(store, {
                    "topic_id": "alpha", "title": "alpha 主题标题",
                    "base_hop": None, "base_sha": None, "session_id": "ses-1",
                    "actor": "t", "summary": "首轮", "mutations": [
                        {"op": "create", "track": "C", "statement": "结论一",
                         "evidence_status": "verified", "basis": "依据一"},
                    ]}, self.tmp_root)
                self.assertEqual(code, 0)
                (store / ".topicdock-published.json").write_text(
                    content, encoding="utf-8"
                )
                self.assertEqual(
                    topicdock.read_published_oids(_ctx(store)), (set(), True)
                )
                # 台账损坏 → amend 降级新建
                code, degraded = run_save(store, {
                    "topic_id": "alpha", "base_hop": "H001",
                    "base_sha": data["base_sha"], "session_id": "ses-1",
                    "mutations": [
                        {"op": "create", "track": "C", "statement": "降级"}],
                }, self.tmp_root, "--amend")
                self.assertEqual(code, 0)
                self.assertFalse(degraded["amend"])
                self.assertTrue(degraded["degraded"])
                self.assertEqual(degraded["head_hop"], "H002")
                # doctor 报 invalid
                checks = self._checks(store)
                self.assertEqual(checks["published_ledger"]["status"], "invalid")
                # delete 型栈项（无 new_hop）同样恒拦截
                code, deleted = run_cli_json(
                    ["delete", "alpha", "--yes", "--store-dir", str(store),
                     "--json"]
                )
                self.assertEqual(code, 0)
                for flags in ([], ["--force"]):
                    code, blocked = run_cli_json(
                        ["undo", *flags, "--store-dir", str(store), "--json"]
                    )
                    self.assertEqual(code, 7)
                    self.assertIn(
                        "UNDO_BLOCKED", json.dumps(blocked, ensure_ascii=False)
                    )

    def test_doctor_ledger_missing_and_valid_are_ok(self):
        store, _data = self._live()
        checks = self._checks(store)
        self.assertEqual(checks["published_ledger"]["status"], "ok")
        self._seal(store, self._oid(store, "H001"))
        checks = self._checks(store)
        self.assertEqual(checks["published_ledger"]["status"], "ok")
        self.assertIn("1", checks["published_ledger"]["detail"])

    def test_hop_oid_drift_reported_without_cascade(self):
        store, _data = self._live()
        hop_path = Path(store) / "hops" / "hop-001.md"
        old_sha = topicdock.file_sha256(hop_path)
        hop = topicdock.parse_hop_md(hop_path.read_text(encoding="utf-8"))
        hop.header["hop_oid"] = "v1:" + "0" * 64
        topicdock.atomic_write_text(hop_path, topicdock.render_hop_md(hop))
        new_sha = topicdock.file_sha256(hop_path)
        # 旧客户端 amend 未刷新 hop_oid 的真实面：DB 与顶层 md 同步到新文件
        md_path = Path(store) / "topics" / "alpha.md"
        md_text = md_path.read_text(encoding="utf-8")
        new_md = md_text.replace(
            f"sha: `{old_sha[:12]}`", f"sha: `{new_sha[:12]}`"
        )
        self.assertNotEqual(new_md, md_text)
        topicdock.atomic_write_text(md_path, new_md)
        conn = get_or_create_db(_ctx(store))
        conn.execute(
            "UPDATE hops SET md_sha256=? WHERE hop_id='H001'", (new_sha,)
        )
        conn.execute(
            "UPDATE topics SET md_sha256=? WHERE topic_id='alpha'",
            (topicdock.sha256_text(new_md),),
        )
        conn.commit()
        conn.close()
        # 非破坏性：经自愈后主题仍存活（不判 damaged 级联删）
        conn, _reports = ensure_store_healthy(_ctx(store))
        try:
            row = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id='alpha'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        checks = self._checks(store)
        self.assertEqual(checks["hop_oid_drift"]["status"], "drift")
        self.assertIn("H001", checks["hop_oid_drift"]["detail"])

    def test_directory_ledger_path_fails_closed(self):
        """台账路径被目录占用 = 读取失败（非「不存在」）→ 失败关闭。"""
        store, data = self._live()
        (Path(store) / ".topicdock-published.json").mkdir()
        self.assertEqual(
            topicdock.read_published_oids(_ctx(store)), (set(), True)
        )
        code, degraded = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001",
            "base_sha": data["base_sha"], "session_id": "ses-1",
            "mutations": [
                {"op": "create", "track": "C", "statement": "降级"}],
        }, self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertFalse(degraded["amend"])
        self.assertTrue(degraded["degraded"])
        self.assertEqual(degraded["head_hop"], "H002")
        checks = self._checks(store)
        self.assertEqual(checks["published_ledger"]["status"], "invalid")
        for flags in ([], ["--force"]):
            code, blocked = run_cli_json(
                ["undo", *flags, "--store-dir", str(store), "--json"]
            )
            self.assertEqual(code, 7)
            self.assertIn(
                "UNDO_BLOCKED", json.dumps(blocked, ensure_ascii=False)
            )

    def test_mark_published_oids_refuses_corrupt_ledger(self):
        """台账损坏 → 拒绝覆写（字节不变），既有封板记录不被静默遗忘。"""
        store, data = self._live()
        ledger = Path(store) / ".topicdock-published.json"
        ledger.write_text(
            json.dumps({"v": 2, "published_oids": ["v1:" + "a" * 64]}),
            encoding="utf-8",
        )
        before = ledger.read_bytes()
        ctx = _ctx(store)
        with self.assertRaises(topicdock.InvalidInput):
            with ctx.exclusive_lock():
                topicdock.mark_published_oids(ctx, ["v1:" + "b" * 64])
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(
            topicdock.read_published_oids(ctx), (set(), True)
        )
        # 失败关闭面保持不变：amend 仍降级、undo 仍拦截
        code, degraded = run_save(store, {
            "topic_id": "alpha", "base_hop": "H001",
            "base_sha": data["base_sha"], "session_id": "ses-1",
            "mutations": [
                {"op": "create", "track": "C", "statement": "降级"}],
        }, self.tmp_root, "--amend")
        self.assertEqual(code, 0)
        self.assertTrue(degraded["degraded"])
        code, blocked = run_cli_json(
            ["undo", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)
        self.assertIn(
            "UNDO_BLOCKED", json.dumps(blocked, ensure_ascii=False)
        )

    def test_doctor_rebuild_keeps_published_ledger(self):
        store, _data = self._live()
        oid = self._oid(store, "H001")
        self._seal(store, oid)
        ledger = Path(store) / ".topicdock-published.json"
        before = ledger.read_bytes()
        code, _report = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(
            topicdock.read_published_oids(_ctx(store)), ({oid}, False)
        )


# ===================== M3/D9：版本守卫短读 / 探测连接复用 / match·use 单轮复合 =====================


class _CountingReader:
    """包裹二进制文件对象，累计实际 read 的字节数（断言短读不吞全文件）。"""

    def __init__(self, handle):
        self._handle = handle
        self.total = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._handle.close()
        return False

    def read(self, size=-1):
        data = self._handle.read(size)
        self.total += len(data)
        return data

    def readline(self, size=-1):
        data = self._handle.readline(size)
        self.total += len(data)
        return data


class TestSchemaGuardShortRead(TopicDockTestCase):
    """D9 短读守卫：frontmatter 界内命中、超上限未闭合不判定、CRLF 跳头仍拦截。"""

    def test_long_frontmatter_high_version_still_rejected(self):
        store = make_store()
        ctx = _ctx(store)
        path = ctx.topics_dir / "big.md"
        path.write_text(
            "---\nschema_version: 2\ntopic_id: big\ntitle: "
            + ("x" * 20000)
            + "\n---\nbody\n",
            encoding="utf-8",
        )
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            topicdock._raise_on_newer_schema(ctx)

    def test_unclosed_oversized_frontmatter_not_read_fully(self):
        store = make_store()
        ctx = _ctx(store)
        path = ctx.topics_dir / "broken.md"
        payload = b"---\n" + b"schema_version: 2\n" + (b"x" * (80 * 1024))
        path.write_bytes(payload)
        real_open = Path.open
        readers: list[_CountingReader] = []

        def _counting_open(self, *args, **kwargs):
            reader = _CountingReader(real_open(self, *args, **kwargs))
            readers.append(reader)
            return reader

        with mock.patch.object(Path, "open", _counting_open):
            topicdock._raise_on_newer_schema(ctx)  # 未闭合 → 不判定，不抛
        self.assertTrue(readers)
        self.assertLessEqual(readers[0].total, 64 * 1024)
        self.assertLess(readers[0].total, len(payload))

    def test_cr_only_topic_schema_rejected_via_cli(self):
        store = make_store()
        ctx = _ctx(store)
        text = (
            "---\nschema_version: 2\ntopic_id: cr-only\ntitle: CR 主题\n"
            "---\nbody\n"
        )
        (ctx.topics_dir / "cr-only.md").write_bytes(
            text.replace("\n", "\r").encode("utf-8")
        )
        code, data = run_cli_json(
            ["list", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 6)
        self.assertEqual(data["status"], "UNSUPPORTED_SCHEMA_VERSION")

    def test_cr_only_cross_chunk_boundary_still_rejected(self):
        store = make_store()
        ctx = _ctx(store)
        # 首行 `---\r\n` 后填 8186 字节，使行尾 `\r` 落在第 8192 字节、
        # `\n` 落在下一 chunk 首字节（跨 chunk 的 CRLF 必须仍算一个行尾）。
        payload = (
            b"---\r\n"
            + (b"x" * (8191 - len(b"---\r\n")))
            + b"\r\n"
            + b"schema_version: 2\r\n"
            + b"---\r\n"
        )
        self.assertGreater(len(payload), 8192)
        (ctx.topics_dir / "boundary.md").write_bytes(payload)
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            topicdock._raise_on_newer_schema(ctx)

    def test_lf_frontmatter_low_version_unaffected(self):
        store = make_store()
        ctx = _ctx(store)
        (ctx.topics_dir / "ok.md").write_text(
            "---\nschema_version: 1\ntopic_id: ok\ntitle: ok\n---\n",
            encoding="utf-8",
        )
        topicdock._raise_on_newer_schema(ctx)  # 合法 LF v1：零误判

    def test_crlf_hop_header_high_version_still_rejected(self):
        store = make_store()
        ctx = _ctx(store)
        path, _header = write_hop(store, "H001", "alpha", [], [], title="alpha")
        text = path.read_text(encoding="utf-8").replace('"v":1', '"v":2')
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            topicdock._raise_on_newer_schema(ctx)


class TestProbeConnectionReuse(TopicDockTestCase):
    """D9：健康库探测连接直接升格；任何异常路径 connect 计数 == close 计数。"""

    def _patch_connect(self):
        counts = {"connect": 0, "close": 0}
        real_connect = sqlite3.connect

        class _CountingConnection(sqlite3.Connection):
            def close(self):
                counts["close"] += 1
                return super().close()

        def _fake_connect(*args, **kwargs):
            counts["connect"] += 1
            kwargs["factory"] = _CountingConnection
            return real_connect(*args, **kwargs)

        return counts, mock.patch.object(sqlite3, "connect", _fake_connect)

    def test_healthy_store_connects_once_and_reused_conn_initialized(self):
        store, _builders = _rich_live_store()
        ctx = _ctx(store)
        counts, patch = self._patch_connect()
        with patch:
            conn, _reports = ensure_store_healthy(ctx)
            try:
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                meta = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(counts["connect"], 1)  # 基线探测 + 工作 = 2
        self.assertEqual(counts["close"], 1)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(meta, "1")

    def test_list_command_connects_once(self):
        store, _builders = _rich_live_store()
        counts, patch = self._patch_connect()
        with patch:
            code, data = run_cli_json(
                ["list", "--store-dir", str(store), "--json"]
            )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(counts["connect"], 1)  # 基线探测 + 工作 = 2

    def test_db_high_version_zero_leak(self):
        store, _builders = _rich_live_store()
        ctx = _ctx(store)
        raw = sqlite3.connect(str(ctx.db_path))
        raw.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        raw.commit()
        raw.close()
        counts, patch = self._patch_connect()
        with patch:
            with self.assertRaises(topicdock.UnsupportedSchemaVersion):
                ensure_store_healthy(ctx)
        self.assertEqual(counts["connect"], counts["close"])
        self.assertEqual(counts["connect"], 1)

    def test_md_high_version_zero_leak(self):
        store, builders = _rich_live_store()
        ctx = _ctx(store)
        md_path = builders["alpha"].md_path()
        md_path.write_text(
            md_path.read_text(encoding="utf-8").replace(
                "schema_version: 1", "schema_version: 2"
            ),
            encoding="utf-8",
        )
        counts, patch = self._patch_connect()
        with patch:
            with self.assertRaises(topicdock.UnsupportedSchemaVersion):
                ensure_store_healthy(ctx)
        self.assertEqual(counts["connect"], counts["close"])
        self.assertEqual(counts["connect"], 1)

    def test_hop_high_version_zero_leak(self):
        store, _builders = _rich_live_store()
        ctx = _ctx(store)
        hop_path = ctx.hops_dir / "hop-004.md"
        hop_path.write_text(
            hop_path.read_text(encoding="utf-8").replace('"v":1', '"v":2'),
            encoding="utf-8",
        )
        counts, patch = self._patch_connect()
        with patch:
            with self.assertRaises(topicdock.UnsupportedSchemaVersion):
                ensure_store_healthy(ctx)
        self.assertEqual(counts["connect"], counts["close"])
        self.assertEqual(counts["connect"], 1)


class TestMatchMountCredentials(TopicDockTestCase):
    """D9：match 前 ≤5 命中直返完整挂载凭证；第 6 项起 full:false 不附。"""

    def test_top5_credentials_and_sixth_plain(self):
        store = make_store()
        build_big_corpus(store, n_topics=6, hops_per_topic=1)
        code, data = run_cli_json(
            ["match", "--topic", "核心结论", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        results = data["results"]
        self.assertEqual(len(results), 6)
        for entry in results[:5]:
            self.assertTrue(entry["full"])
            self.assertRegex(entry["head_hop"], r"^H\d+$")
            self.assertRegex(entry["base_sha"], r"^[0-9a-f]{64}$")
            self.assertIsInstance(entry["artifacts"], list)
            self.assertIn("text", entry)
        for entry in results[5:]:
            self.assertFalse(entry["full"])
            for key in ("head_hop", "base_sha", "artifacts", "text"):
                self.assertNotIn(key, entry)

    def test_recent_top5_credentials_same_shape(self):
        store = make_store()
        build_big_corpus(store, n_topics=6, hops_per_topic=1)
        code, data = run_cli_json(
            ["match", "--recent", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(data["results"]), 5)
        for entry in data["results"]:
            self.assertTrue(entry["full"])
            self.assertIsNone(entry["score"])
            self.assertRegex(entry["head_hop"], r"^H\d+$")
            self.assertRegex(entry["base_sha"], r"^[0-9a-f]{64}$")
            self.assertIn("artifacts", entry)


TRACE_PAYLOAD = {
    "topic_id": "trace-topic",
    "title": "追踪主题",
    "base_hop": None,
    "base_sha": None,
    "session_id": "ses-1",
    "actor": "tester",
    "mutations": [
        {"op": "create", "track": "C", "statement": "追踪唯一关键词 zzqtrace"},
        {"op": "create", "track": "Q", "statement": "未决问题一"},
        {"op": "create", "track": "Q", "statement": "未决问题二"},
        {"op": "create", "track": "A", "state": "~", "statement": "断点行动"},
        {"op": "create", "track": "A", "state": "x", "statement": "完成行动"},
    ],
}


class TestMatchAndUseTrace(TopicDockTestCase):
    """D9：use/match `--trace <auto|items>` 同轮内联；空集 exit 7。"""

    def _trace_store(self):
        store = make_store()
        code, saved = run_save(store, dict(TRACE_PAYLOAD), self.tmp_root)
        self.assertEqual((code, saved["status"]), (0, "OK"))
        return store

    def test_match_trace_auto_and_explicit_and_missing(self):
        store = self._trace_store()
        code, data = run_cli_json(
            ["match", "--topic", "zzqtrace", "--trace", "auto",
             "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        entry = data["results"][0]
        self.assertEqual(list(entry["traces"]), ["Q1", "Q2", "A1"])
        for trace in entry["traces"].values():
            self.assertEqual(trace["status"], "OK")
            self.assertIn("successor_notes", trace)

        code, data = run_cli_json(
            ["match", "--topic", "zzqtrace", "--trace", "C1,Q9",
             "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        traces = data["results"][0]["traces"]
        self.assertIn("blocks", traces["C1"])
        self.assertEqual(traces["Q9"], {"missing_item": True})

    def test_use_trace_auto_excludes_completed_actions(self):
        store = self._trace_store()
        code, data = run_cli_json(
            ["use", "trace-topic", "--trace", "auto",
             "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(list(data["traces"]), ["Q1", "Q2", "A1"])
        self.assertNotIn("A2", data["traces"])

    def test_use_trace_explicit_item_shape_and_missing(self):
        store = self._trace_store()
        code, data = run_cli_json(
            ["use", "trace-topic", "--trace", "C1,Q9",
             "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        trace = data["traces"]["C1"]
        self.assertEqual(
            set(trace),
            {"status", "topic_id", "item_id", "blocks", "links",
             "successor", "successor_notes"},
        )
        self.assertTrue(trace["blocks"])
        self.assertEqual(data["traces"]["Q9"], {"missing_item": True})

    def test_use_single_topic_direct_mount_applies_trace(self):
        store = self._trace_store()
        code, data = run_cli_json(
            ["use", "--trace", "auto", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["topic_id"], "trace-topic")
        self.assertEqual(list(data["traces"]), ["Q1", "Q2", "A1"])

    def test_use_list_branch_ignores_trace(self):
        store = self._trace_store()
        second = dict(TRACE_PAYLOAD)
        second["topic_id"] = "trace-topic-2"
        second["title"] = "第二主题"
        code, saved = run_save(store, second, self.tmp_root)
        self.assertEqual((code, saved["status"]), (0, "OK"))
        code, data = run_cli_json(
            ["use", "--trace", "auto", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["total"], 2)
        self.assertNotIn("traces", data)

    def test_use_empty_store_ignores_trace(self):
        store = make_store()
        code, data = run_cli_json(
            ["use", "--trace", "auto", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(data["topic_id"])
        self.assertNotIn("traces", data)

    def test_use_trace_empty_set_is_invalid(self):
        store = self._trace_store()
        code, data = run_cli_json(
            ["use", "trace-topic", "--trace", ",", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 7)
        self.assertEqual(data["status"], "INVALID_INPUT")
