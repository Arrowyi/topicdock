"""FTS5 匹配索引：能力探测/迁移/自愈/降级/等价/性能 + 分层召回/score/hint/similar。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from unittest import mock

from tests.fixtures.match_corpus import (
    QUERIES,
    SCORE_DEGENERATE_BEST,
    SCORE_MIN_SPREAD,
    SIMILAR_NEGATIVE,
    SIMILAR_POSITIVE,
    build_topic_docs,
    materialize_store,
)
from tests.util import (
    TopicDockTestCase,
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
    StoreContext,
    delete_match_index,
    match_recall,
    match_topics,
    ensure_match_index,
    ensure_store_healthy,
    get_or_create_db,
    probe_fts5,
    refresh_match_index,
    similar_topics,
    tokenize,
    tokenize_terms,
)


def _ctx(store) -> StoreContext:
    return StoreContext(Path(store))


def _connect(store) -> sqlite3.Connection:
    return get_or_create_db(_ctx(store))


def _fts_pairs(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT topic_id, md_sha256 FROM topic_fts ORDER BY topic_id, md_sha256"
        )
    ]


def _topics_pairs(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT topic_id, md_sha256 FROM topics ORDER BY topic_id"
        )
    ]


def _rich_live_store() -> Path:
    """rich 语料 + 已冷重建落库（索引经正常命令路径构建）。"""
    store = make_store()
    build_rich_store(store)
    code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
    if code != 0:
        raise AssertionError(f"list 失败: exit={code} payload={data!r}")
    return store


def _init_at(path) -> Path:
    code, data = run_cli_json(
        ["init", "--scope", "local", "--path", str(path), "--json"]
    )
    if code != 0:
        raise AssertionError(f"init 失败: exit={code} payload={data!r}")
    return Path(path)


def _patch_connect():
    """包装 sqlite3.connect 计数（connect/close 配对）。"""
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


def _corrupt_topic_fts(store) -> None:
    """用错列普通表替换 `topic_fts`（兼容 3.9.6 DEFENSIVE：不直接改影子表）。"""
    raw = sqlite3.connect(str(_ctx(store).db_path))
    try:
        raw.execute("DROP TABLE IF EXISTS topic_fts")
        raw.execute("CREATE TABLE topic_fts(bad_col INT)")
        raw.execute("INSERT INTO topic_fts(bad_col) VALUES(1)")
        raw.commit()
    finally:
        raw.close()


def _corrupt_topic_fts_columns(store, columns: str) -> None:
    """用指定列的普通表替换 `topic_fts`（可构造「健康检查可过、迁移读取失败」的缝）。"""
    raw = sqlite3.connect(str(_ctx(store).db_path))
    try:
        raw.execute("DROP TABLE IF EXISTS topic_fts")
        raw.execute(f"CREATE TABLE topic_fts({columns})")
        raw.commit()
    finally:
        raw.close()


# ---------- 能力探测：temp schema、零写主库、零新连接 ----------


class TestProbeAndCapability(TopicDockTestCase):
    def test_probe_uses_temp_schema_and_no_new_connection(self):
        store = make_store()
        conn = _connect(store)
        self.addCleanup(conn.close)
        before = dump_tables(conn)
        topicdock._FTS5_AVAILABLE = None
        counts, patch = _patch_connect()
        with patch:
            self.assertTrue(probe_fts5(conn))
            self.assertTrue(ensure_match_index(conn))
        self.assertEqual(counts["connect"], 0)  # 不新建连接
        self.assertIsNone(
            conn.execute(
                "SELECT name FROM temp.sqlite_master WHERE name='_fts5_probe'"
            ).fetchone()
        )
        self.assertEqual(dump_tables(conn), before)  # 不写主库

    def test_healthy_store_connects_once_and_zero_logical_write(self):
        store = _rich_live_store()
        conn = _connect(store)
        before = dump_tables(conn)
        conn.close()
        counts, patch = _patch_connect()
        with patch:
            conn, reports = ensure_store_healthy(_ctx(store))
            try:
                after = dump_tables(conn)
            finally:
                conn.close()
        self.assertEqual(counts["connect"], 1)
        self.assertEqual(after, before)
        self.assertNotIn("rebuild_db", [r["name"] for r in reports])
        # 索引 ok → ensure_store_healthy 零追加（doctor 的固定巡检项不在此路径）
        self.assertEqual([r for r in reports if r["name"] == "match_index"], [])

    def test_existing_index_survives_unavailable_module(self):
        """已有 topic_fts + 缺模块解释器：不 DROP 已有表、命令照常可用。"""
        store = _rich_live_store()
        conn = _connect(store)
        before = conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]
        conn.close()
        self.assertGreater(before, 0)
        with mock.patch.object(topicdock, "_FTS5_AVAILABLE", False):
            code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
            self.assertEqual((code, data["status"]), (0, "OK"))
            conn = _connect(store)
            after = conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]
            conn.close()
        self.assertEqual(after, before)

    def test_get_or_create_db_high_version_does_not_touch_match_index(self):
        """高版本库：get_or_create_db 自身守卫，不建/不修 topic_fts、不写 FTS。"""
        store = _rich_live_store()
        _corrupt_topic_fts(store)
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            raw.commit()
            before_tables = {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            raw.close()
        with self.assertRaises(topicdock.UnsupportedSchemaVersion):
            get_or_create_db(_ctx(store))
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            after_tables = {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = [row[1] for row in raw.execute("PRAGMA table_info(topic_fts)")]
            version = raw.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            topics = raw.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        finally:
            raw.close()
        self.assertEqual(after_tables, before_tables)  # 未新建/未修复任何表
        self.assertEqual(columns, ["bad_col"])  # 损坏表原样保留
        self.assertEqual(version, "2")
        self.assertEqual(topics, 3)


# ---------- 降级矩阵：FTS5 不可用时其余命令照常 ----------


class TestDegradedMode(TopicDockTestCase):
    def test_core_commands_work_and_doctor_reports_unavailable(self):
        store = make_store()
        build_rich_store(store)
        code, _ = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        code, used = run_cli_json(["use", "alpha", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        base_hop, base_sha = used["head_hop"], used["base_sha"]

        with mock.patch.object(topicdock, "_FTS5_AVAILABLE", False):
            for argv in (
                ["list"],
                ["use", "alpha"],
            ):
                code, data = run_cli_json([*argv, "--store-dir", str(store), "--json"])
                self.assertEqual((code, data["status"]), (0, "OK"), argv)

            code, saved = run_save(
                store,
                {
                    "topic_id": "alpha",
                    "base_hop": base_hop,
                    "base_sha": base_sha,
                    "session_id": "ses-degraded",
                    "summary": "降级推进",
                    "mutations": [
                        {"op": "create", "track": "Q", "statement": "降级下的新问题"}
                    ],
                },
                self.tmp_root,
            )
            self.assertEqual(code, 0)

            for argv in (
                ["delete", "gamma", "--yes"],
                ["merge", "beta", "alpha", "--summary", "并入 beta"],
            ):
                code, data = run_cli_json([*argv, "--store-dir", str(store), "--json"])
                self.assertEqual((code, data["status"]), (0, "OK"), argv)

            code, doctor = run_cli_json(
                ["doctor", "--store-dir", str(store), "--json"]
            )
            self.assertEqual(code, 0)
        entries = [c for c in doctor["checks"] if c["name"] == "match_index"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "unavailable")

    def test_missing_module_message_does_not_trigger_cold_rebuild(self):
        store = _rich_live_store()

        def missing_module(_conn):
            topicdock._FTS5_AVAILABLE = False
            raise sqlite3.DatabaseError("no such module: fts5")

        with mock.patch.object(topicdock, "ensure_match_index", missing_module):
            conn, reports = ensure_store_healthy(_ctx(store))
            try:
                count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 3)  # 现有 DB 继续使用，未整库重建
        self.assertNotIn("rebuild_db", [r["name"] for r in reports])
        entries = [r for r in reports if r["name"] == "match_index"]
        self.assertEqual([entry["status"] for entry in entries], ["unavailable"])

    def test_missing_module_from_quick_check_reports_unavailable(self):
        """缺模块错误由 `PRAGMA quick_check` 自身抛出（design §5.4 声明场景）。

        断言不触发整库冷重建、沿用现有 DB，且迁移报告 `match_index: unavailable`。
        """
        store = _rich_live_store()
        real_connect = sqlite3.connect

        class _MissingFTS5Connection(sqlite3.Connection):
            def execute(self, sql, *args):
                if " ".join(sql.split()).upper() == "PRAGMA QUICK_CHECK":
                    raise sqlite3.DatabaseError("no such module: fts5")
                return super().execute(sql, *args)

        def _fake_connect(*args, **kwargs):
            kwargs["factory"] = _MissingFTS5Connection
            return real_connect(*args, **kwargs)

        with mock.patch.object(sqlite3, "connect", _fake_connect):
            conn, reports = ensure_store_healthy(_ctx(store))
            try:
                count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 3)  # 现有 DB 继续使用，未整库重建
        self.assertNotIn("rebuild_db", [r["name"] for r in reports])
        entries = [r for r in reports if r["name"] == "match_index"]
        self.assertEqual([entry["status"] for entry in entries], ["unavailable"])
        self.assertFalse(topicdock._FTS5_AVAILABLE)

    def test_other_database_error_triggers_cold_rebuild(self):
        """宽泛 `fts5` 子串不得吞掉真实损坏（如 `fts5: missing row`）。"""
        store = _rich_live_store()
        real = topicdock.ensure_match_index
        calls = {"n": 0}

        def flaky(conn):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.DatabaseError("fts5: missing row 3")
            return real(conn)

        with mock.patch.object(topicdock, "ensure_match_index", flaky):
            conn, reports = ensure_store_healthy(_ctx(store))
            try:
                count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 3)
        self.assertIn("rebuild_db", [r["name"] for r in reports])


# ---------- 损坏自愈 ----------


class TestCorruptIndexSelfHeal(TopicDockTestCase):
    def test_wrong_column_table_rebuilt_on_open(self):
        store = _rich_live_store()
        conn = _connect(store)
        n_topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        conn.close()
        _corrupt_topic_fts(store)
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        conn = _connect(store)
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(topic_fts)")}
            self.assertIn("md_sha256", columns)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0], n_topics
            )
            self.assertEqual(_fts_pairs(conn), _topics_pairs(conn))
            conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('integrity-check')")
        finally:
            conn.close()

    def test_refresh_and_delete_raise_match_index_error(self):
        store = _rich_live_store()
        _corrupt_topic_fts(store)
        raw = sqlite3.connect(str(_ctx(store).db_path))
        raw.row_factory = sqlite3.Row
        try:
            topicdock._FTS5_AVAILABLE = True
            doc = topicdock.TopicDoc(topic_id="alpha", title="alpha")
            with self.assertRaises(topicdock.MatchIndexError):
                refresh_match_index(raw, "alpha", doc, "sha-alpha")
            with self.assertRaises(topicdock.MatchIndexError):
                delete_match_index(raw, ["alpha"])
        finally:
            raw.close()

    def test_save_rolls_back_when_index_write_fails(self):
        store = _rich_live_store()
        code, used = run_cli_json(["use", "alpha", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        conn = _connect(store)
        before_topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        before_hops = conn.execute("SELECT COUNT(*) FROM hops").fetchone()[0]
        conn.close()
        payload = {
            "topic_id": "alpha",
            "base_hop": used["head_hop"],
            "base_sha": used["base_sha"],
            "session_id": "ses-fail",
            "summary": "索引写失败",
            "mutations": [{"op": "create", "track": "Q", "statement": "索引失败问题"}],
        }
        with mock.patch.object(
            topicdock, "refresh_match_index",
            side_effect=topicdock.MatchIndexError("boom"),
        ):
            code, out = run_save(store, payload, self.tmp_root)
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "ERROR")
        conn = _connect(store)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], before_topics
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM hops").fetchone()[0], before_hops
            )
        finally:
            conn.close()

    def test_corruption_during_crash_adoption_recovers(self):
        store = _rich_live_store()
        conn = _connect(store)
        try:
            # 模拟 ③ 崩溃窗口：gamma 的 DB 行整体缺失（md/分片在盘）
            conn.execute("DELETE FROM topics WHERE topic_id='gamma'")
            conn.execute("DELETE FROM hops WHERE hop_id='H006'")
            conn.execute("DELETE FROM item_hops WHERE topic_id='gamma'")
            conn.execute("DELETE FROM item_links WHERE topic_id='gamma'")
            conn.execute("DELETE FROM topic_fts WHERE topic_id='gamma'")
            conn.commit()
        finally:
            conn.close()
        _corrupt_topic_fts(store)
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        conn = _connect(store)
        try:
            ids = {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")}
            self.assertEqual(ids, {"alpha", "beta", "gamma"})
            self.assertEqual(_fts_pairs(conn), _topics_pairs(conn))
        finally:
            conn.close()

    def test_doctor_reports_mismatch_after_corruption_recovery(self):
        """损坏恢复 + 不可解析 md 并存：doctor 报 mismatch 并列出跳过项（非 rebuilt）。"""
        store = _rich_live_store()
        # 只留 md_sha256 列的普通表：ensure_match_index 健康检查可过，迁移读取 topic_id 失败
        _corrupt_topic_fts_columns(store, "md_sha256 TEXT")
        real_loader = topicdock._load_topic_doc_for_index
        real_recover = topicdock._recover_match_index
        recover_calls = {"n": 0}

        def loader(store_ctx, topic_id):
            if topic_id == "gamma":  # 模拟七步自愈后仍不可解析的存活主题
                return None
            return real_loader(store_ctx, topic_id)

        def recover(store_ctx, conn, cause):
            recover_calls["n"] += 1
            return real_recover(store_ctx, conn, cause)

        with mock.patch.object(topicdock, "_load_topic_doc_for_index", loader), \
                mock.patch.object(topicdock, "_recover_match_index", recover):
            code, report = run_cli_json(
                ["doctor", "--store-dir", str(store), "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(recover_calls["n"], 1)  # 确实走了损坏恢复分支
        entries = [c for c in report["checks"] if c["name"] == "match_index"]
        self.assertEqual(len(entries), 1)  # 恰一条，且不吞明细
        self.assertEqual(entries[0]["status"], "mismatch")
        self.assertIn("gamma", entries[0]["detail"])
        conn = _connect(store)
        try:
            skipped = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='match_index_skipped'"
                ).fetchone()["value"]
            )
            self.assertIn("gamma", skipped)
            self.assertNotIn(
                "gamma",
                {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")},
            )
        finally:
            conn.close()
        # 主题删除后 skip 集合清理
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("DELETE FROM topics WHERE topic_id='gamma'")
            raw.commit()
        finally:
            raw.close()
        conn = _connect(store)
        try:
            topicdock.migrate_match_index(_ctx(store), conn)
            conn.commit()
            skipped = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='match_index_skipped'"
                ).fetchone()["value"]
            )
        finally:
            conn.close()
        self.assertEqual(skipped, {})

    def test_persistent_recovery_failure_degrades_to_unavailable(self):
        """drop/重建/全量重索引持续失败 → 置不可用，doctor 报 unavailable，其余命令可用。"""
        store = _rich_live_store()
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("DELETE FROM meta WHERE key='match_index_schema'")
            raw.commit()
        finally:
            raw.close()
        boom = topicdock.MatchIndexError("persistent boom")
        with mock.patch.object(topicdock, "refresh_match_index", side_effect=boom):
            code, report = run_cli_json(
                ["doctor", "--store-dir", str(store), "--json"]
            )
            self.assertEqual(code, 0)
            entries = [c for c in report["checks"] if c["name"] == "match_index"]
            self.assertEqual([entry["status"] for entry in entries], ["unavailable"])
            self.assertFalse(topicdock._FTS5_AVAILABLE)
            for argv in (["list"], ["use", "alpha"]):
                code, data = run_cli_json(
                    [*argv, "--store-dir", str(store), "--json"]
                )
                self.assertEqual((code, data["status"]), (0, "OK"), argv)
        # 本进程后续 refresh/delete 走 no-op
        conn = sqlite3.connect(str(_ctx(store).db_path))
        try:
            before = conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]
            doc = topicdock.TopicDoc(topic_id="alpha", title="alpha")
            topicdock.refresh_match_index(conn, "alpha", doc, "sha-x")
            topicdock.delete_match_index(conn, ["alpha"])
            after = conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after)


# ---------- 迁移等价与清理 ----------


class TestMigrationEquivalence(TopicDockTestCase):
    def _snapshot(self, store):
        conn = _connect(store)
        try:
            snapshot = dump_tables(conn)
            pairs = _fts_pairs(conn)
            topics = _topics_pairs(conn)
            conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('integrity-check')")
        finally:
            conn.close()
        return snapshot, pairs, topics

    def test_three_paths_equivalent(self):
        # 增量路径：schema 库 + 七步自愈纳管（_upsert_topic_row 刷索引）
        store_inc = make_store()
        materialize_store(store_inc, limit=10)
        _connect(store_inc).close()
        code, _ = run_cli_json(["list", "--store-dir", str(store_inc), "--json"])
        self.assertEqual(code, 0)
        inc_snap, inc_pairs, inc_topics = self._snapshot(store_inc)
        self.assertEqual(len(inc_pairs), 10)
        self.assertEqual(inc_pairs, inc_topics)
        self.assertEqual(inc_snap["meta"]["match_index_schema"], "fts5-perfield-v1")

        # 迁移路径：清空索引与 meta 键后由 migrate_match_index 全量重建
        store_mig = Path(self.tmp_root) / "mig-store"
        shutil.copytree(store_inc, store_mig)
        raw = sqlite3.connect(str(_ctx(store_mig).db_path))
        try:
            raw.execute("DELETE FROM topic_fts")
            raw.execute(
                "DELETE FROM meta WHERE key IN"
                " ('match_index_schema','match_index_skipped')"
            )
            raw.commit()
        finally:
            raw.close()
        code, _ = run_cli_json(["list", "--store-dir", str(store_mig), "--json"])
        self.assertEqual(code, 0)
        mig_snap, mig_pairs, mig_topics = self._snapshot(store_mig)

        # 冷重建路径
        store_reb = Path(self.tmp_root) / "reb-store"
        shutil.copytree(store_inc, store_reb)
        code, _ = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store_reb), "--json"]
        )
        self.assertEqual(code, 0)
        reb_snap, reb_pairs, reb_topics = self._snapshot(store_reb)

        self.assertEqual(mig_snap["topic_fts"], inc_snap["topic_fts"])
        self.assertEqual(reb_snap["topic_fts"], inc_snap["topic_fts"])
        self.assertEqual(mig_pairs, inc_pairs)
        self.assertEqual(reb_pairs, inc_pairs)
        self.assertEqual(mig_pairs, mig_topics)
        self.assertEqual(reb_pairs, reb_topics)

    def test_migration_cleans_ghost_duplicate_stale_rows(self):
        store = _rich_live_store()
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            row = raw.execute(
                "SELECT topic_id, md_sha256, topic_slug, title, focus, c, q, a, r"
                " FROM topic_fts WHERE topic_id='alpha'"
            ).fetchone()
            raw.execute(
                "INSERT INTO topic_fts(topic_id, md_sha256, topic_slug, title, focus,"
                " c, q, a, r) VALUES('ghost','x','','','','','','','')"
            )
            raw.execute(
                "INSERT INTO topic_fts(topic_id, md_sha256, topic_slug, title, focus,"
                " c, q, a, r) VALUES(?,?,?,?,?,?,?,?,?)",
                row,
            )
            raw.execute(
                "INSERT INTO topic_fts(topic_id, md_sha256) VALUES('alpha','deadbeef')"
            )
            raw.commit()
        finally:
            raw.close()
        code, _ = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        conn = _connect(store)
        try:
            ids = [row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")]
            self.assertEqual(sorted(ids), sorted(set(ids)))  # 无重复
            self.assertNotIn("ghost", ids)  # 幽灵行清除
            self.assertEqual(_fts_pairs(conn), _topics_pairs(conn))  # 陈旧行重索引
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topic_fts WHERE topic_id NOT IN"
                    " (SELECT topic_id FROM topics)"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_broken_chain_with_intact_md_migrates(self):
        """D-P20 断链但顶层 md 完好：迁移用 parse_topic_md，不走 _replay_chain。"""
        store = _rich_live_store()
        (_ctx(store).hops_dir / "hop-002.md").unlink()
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("DELETE FROM topic_fts")
            raw.execute(
                "DELETE FROM meta WHERE key IN"
                " ('match_index_schema','match_index_skipped')"
            )
            raw.commit()
        finally:
            raw.close()
        code, _ = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual(code, 0)
        conn = _connect(store)
        try:
            ids = {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")}
            self.assertEqual(ids, {"alpha", "beta", "gamma"})
            skipped_row = conn.execute(
                "SELECT value FROM meta WHERE key='match_index_skipped'"
            ).fetchone()
            self.assertEqual(json.loads(skipped_row["value"]), {})
        finally:
            conn.close()
        self.assertTrue((_ctx(store).topics_dir / "alpha.md").is_file())

    def test_escape_entity_roundtrip(self):
        store = make_store()
        materialize_store(store)
        code, _ = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = _connect(store)
        try:
            disk_row = conn.execute(
                "SELECT topic_slug, title, focus, c, q, a, r FROM topic_fts"
                " WHERE topic_id='codec-escape-entity'"
            ).fetchone()
        finally:
            conn.close()
        text = " ".join(disk_row)
        self.assertNotIn("8212", text)  # &#8212; 已还原为 —，不残留实体数字
        self.assertIn("verified", text)

        other = _init_at(Path(self.tmp_root) / "mem-store")
        docs = {doc.topic_id: doc for doc in build_topic_docs()}
        doc = docs["codec-escape-entity"]
        conn = get_or_create_db(StoreContext(other))
        try:
            refresh_match_index(conn, doc.topic_id, doc, "sha-mem")
            mem_row = conn.execute(
                "SELECT topic_slug, title, focus, c, q, a, r FROM topic_fts"
                " WHERE topic_id=?",
                (doc.topic_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(disk_row), tuple(mem_row))


class TestPayloadLiteralEntityCanonicalization(TopicDockTestCase):
    """payload 字面 `&#8212;` 经正常 save 写入 item 正文：三路喂词归一到解码正典。

    复现 M5 真实库 hop-076 场景：条目正文 payload 字面含 `&#8212;` 实体串（文本本身
    在讨论该转义序列），topic md 与 hop 分片同为字面形。migrate 走 `parse_topic_md`
    已解码（无 `8212` 词元），而增量/重建走 `rep.doc` 原文产词元 `8212`——正典归一到
    解码形后三库 topic_fts 8 列投影与词元流一致。
    """

    _PROJECTION = "topic_id, topic_slug, title, focus, c, q, a, r"

    def _projection(self, store):
        conn = get_or_create_db(_ctx(store))
        try:
            rows = [
                tuple(row)
                for row in conn.execute(
                    f"SELECT {self._PROJECTION} FROM topic_fts ORDER BY topic_id"
                )
            ]
            conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('integrity-check')")
        finally:
            conn.close()
        return rows

    @staticmethod
    def _token_streams(rows):
        # 除 topic_id 外的每个文本列均是排序后的词元串 → 按列切词即词元流。
        return [[str(value).split() for value in row[1:]] for row in rows]

    def _copy_and_clear_index(self, source, path):
        target = Path(path)
        shutil.copytree(source, target)
        raw = sqlite3.connect(str(_ctx(target).db_path))
        try:
            raw.execute("DELETE FROM topic_fts")
            raw.execute(
                "DELETE FROM meta WHERE key IN"
                " ('match_index_schema','match_index_skipped')"
            )
            raw.commit()
        finally:
            raw.close()
        return target

    def test_payload_literal_entity_three_paths_converge(self):
        # 增量路径：fixture 物化 + 正常 save 追加 payload 字面 `&#8212;` 的 C 轨条目
        store_inc = make_store()
        materialize_store(store_inc, limit=17)  # 含 codec-escape-entity（index 16）
        _connect(store_inc).close()
        code, _ = run_cli_json(["list", "--store-dir", str(store_inc), "--json"])
        self.assertEqual(code, 0)

        conn = _connect(store_inc)
        try:
            state = conn.execute(
                "SELECT head_hop, md_sha256 FROM topics WHERE topic_id=?",
                ("codec-escape-entity",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(state)
        literal = "payload 字面 &#8212; 实体串在讨论该转义序列本身。"
        code, saved = run_save(
            store_inc,
            {
                "topic_id": "codec-escape-entity",
                "base_hop": state["head_hop"],
                "base_sha": state["md_sha256"],
                "session_id": "ses-m6",
                "actor": "tester",
                "summary": "复现 payload 字面实体串",
                "mutations": [
                    {
                        "op": "create",
                        "track": "C",
                        "statement": literal,
                        "evidence_status": "verified",
                        "basis": "真实库 hop-076 同形场景",
                    }
                ],
            },
            self.tmp_root,
        )
        self.assertEqual(code, 0, saved)
        md_text = (_ctx(store_inc).topics_dir / "codec-escape-entity.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("&#8212;", md_text)  # payload 字面：md 与 hop 同为字面形
        inc_rows = self._projection(store_inc)
        self.assertEqual(len(inc_rows), 17)

        # 迁移路径：清空索引与 meta 键后由 migrate_match_index 全量重建（parse_topic_md）
        store_mig = self._copy_and_clear_index(store_inc, Path(self.tmp_root) / "mig-store")
        code, _ = run_cli_json(["list", "--store-dir", str(store_mig), "--json"])
        self.assertEqual(code, 0)
        mig_rows = self._projection(store_mig)

        # 冷重建路径：doctor --rebuild（hop 重放 rep.doc 原文）
        store_reb = Path(self.tmp_root) / "reb-store"
        shutil.copytree(store_inc, store_reb)
        code, _ = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store_reb), "--json"]
        )
        self.assertEqual(code, 0)
        reb_rows = self._projection(store_reb)

        # 8 列投影 + 词元流逐行一致（解码正典形）
        self.assertEqual(mig_rows, inc_rows, "migrate 投影与增量不符")
        self.assertEqual(reb_rows, inc_rows, "rebuild 投影与增量不符")
        self.assertEqual(
            self._token_streams(mig_rows), self._token_streams(inc_rows),
            "migrate 词元流与增量不符",
        )
        self.assertEqual(
            self._token_streams(reb_rows), self._token_streams(inc_rows),
            "rebuild 词元流与增量不符",
        )

        # 三库均不再索引实体数字词元 `8212`（解码正典把 `&#8212;` 归一到 `—`，
        # 而 `—` 本身不是词元；新增条目的 ASCII 词元仍正常入索引）
        for rows in (inc_rows, mig_rows, reb_rows):
            tokens = [str(value).split() for row in rows for value in row[1:]]
            flattened = [token for column in tokens for token in column]
            self.assertNotIn("8212", flattened)
        row_by_id = {row[0]: row for row in inc_rows}
        c_tokens = set(str(row_by_id["codec-escape-entity"][4]).split())
        self.assertIn("payload", c_tokens)


# ---------- skip 注册表 ----------


class TestSkipRegistry(TopicDockTestCase):
    def test_unparseable_md_skipped_then_cleared_after_delete(self):
        store = _rich_live_store()
        garbage = "not a topic md\n"
        md_path = _ctx(store).topics_dir / "gamma.md"
        md_path.write_text(garbage, encoding="utf-8")
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            # md 与 DB 快照一致，使迁移直接面对「无法解析的 md」（绕过七步自愈修复）
            raw.execute(
                "UPDATE topics SET md_sha256=? WHERE topic_id='gamma'",
                (topicdock.sha256_text(garbage),),
            )
            raw.commit()
        finally:
            raw.close()

        conn = _connect(store)
        try:
            report = topicdock.migrate_match_index(_ctx(store), conn)
            conn.commit()
            skipped = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='match_index_skipped'"
                ).fetchone()["value"]
            )
            self.assertIn("gamma", skipped)
            self.assertEqual(report["status"], "mismatch")
            self.assertNotIn(
                "gamma",
                {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")},
            )
        finally:
            conn.close()

        # md_sha 不变 → 不再重试解析
        conn = _connect(store)
        try:
            with mock.patch.object(
                topicdock, "_load_topic_doc_for_index",
                wraps=topicdock._load_topic_doc_for_index,
            ) as loader:
                topicdock.migrate_match_index(_ctx(store), conn)
                conn.commit()
            self.assertEqual(loader.call_count, 0)
        finally:
            conn.close()

        # 主题删除后 skip 集合清理
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("DELETE FROM topics WHERE topic_id='gamma'")
            raw.commit()
        finally:
            raw.close()
        conn = _connect(store)
        try:
            topicdock.migrate_match_index(_ctx(store), conn)
            conn.commit()
            skipped = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='match_index_skipped'"
                ).fetchone()["value"]
            )
        finally:
            conn.close()
        self.assertEqual(skipped, {})

    def test_registered_skip_with_same_sha_row_drops_stale_content(self):
        """已登记 skip + 同 md_sha 的残留 FTS 行：删行、保留登记、报 mismatch。

        交叉态下「索引一致」短路可能先于 skip 分支命中，导致残留内容行与 skip
        登记同时活着，迁移返回 None 让 doctor 误报 ok（design §3.6/§5.2）。
        """
        store = _rich_live_store()
        garbage = "not a topic md\n"
        sha = topicdock.sha256_text(garbage)
        (_ctx(store).topics_dir / "gamma.md").write_text(garbage, encoding="utf-8")
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute(
                "UPDATE topics SET md_sha256=? WHERE topic_id='gamma'", (sha,)
            )
            raw.execute(
                "UPDATE topic_fts SET md_sha256=? WHERE topic_id='gamma'", (sha,)
            )
            raw.execute(
                "INSERT INTO meta(key, value) VALUES('match_index_skipped', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"gamma": sha}),),
            )
            raw.commit()
        finally:
            raw.close()

        conn = _connect(store)
        try:
            with mock.patch.object(
                topicdock, "_load_topic_doc_for_index",
                wraps=topicdock._load_topic_doc_for_index,
            ) as loader:
                report = topicdock.migrate_match_index(_ctx(store), conn)
                conn.commit()
            self.assertEqual(loader.call_count, 0)  # md_sha 不变：不重试解析
            self.assertNotIn(
                "gamma",
                {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")},
            )
            skipped = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='match_index_skipped'"
                ).fetchone()["value"]
            )
            self.assertEqual(skipped.get("gamma"), sha)  # skip 登记保留
        finally:
            conn.close()
        self.assertIsNotNone(report)
        self.assertEqual(report["status"], "mismatch")


# ---------- tokenize_terms 契约 ----------


class TestTokenizeTerms(TopicDockTestCase):
    def test_token_sets_match_and_duplicates_preserved(self):
        samples = [
            "Auth网关架构JWT改造",
            "缓存缓存",
            "单字",
            "ab-cd 12",
            "&#8212; 8212",
            "设计 缓存 命中",
        ]
        for sample in samples:
            self.assertEqual(set(tokenize_terms(sample)), tokenize(sample), sample)
        terms = tokenize_terms("缓存缓存")
        self.assertEqual(terms.count("缓存"), 2)  # 重复保留（词频）
        self.assertIn("存缓", terms)


# ---------- 性能与体积 ----------


class TestPerfAndSize(TopicDockTestCase):
    def test_full_build_budget_and_size(self):
        docs = build_topic_docs()
        store = make_store()
        conn = _connect(store)
        started = time.perf_counter()
        for doc in docs:
            refresh_match_index(conn, doc.topic_id, doc, "sha-" + doc.topic_id)
        elapsed = time.perf_counter() - started
        conn.commit()
        conn.close()
        self.assertLessEqual(
            elapsed, 0.100, f"40 主题索引构建 {elapsed * 1000:.1f}ms > 100ms"
        )

        docs80 = list(docs) + [
            topicdock.replace(doc, topic_id=doc.topic_id + "-b") for doc in docs
        ]
        store80 = _init_at(Path(self.tmp_root) / "perf80")
        conn = get_or_create_db(StoreContext(store80))
        started = time.perf_counter()
        for doc in docs80:
            refresh_match_index(conn, doc.topic_id, doc, "sha-" + doc.topic_id)
        elapsed80 = time.perf_counter() - started
        conn.commit()
        conn.close()
        self.assertLessEqual(
            elapsed80, 0.250, f"80 主题索引构建 {elapsed80 * 1000:.1f}ms > 250ms"
        )

        empty = _init_at(Path(self.tmp_root) / "empty-store")
        get_or_create_db(StoreContext(empty)).close()
        empty_bytes = _ctx(empty).db_path.stat().st_size
        index_bytes = _ctx(store).db_path.stat().st_size - empty_bytes
        self.assertLessEqual(
            index_bytes, int(1.2 * 1024 * 1024), f"40 主题索引 {index_bytes}B > 1.2MB"
        )
        print(
            f"[M1 perf] 40 主题 {elapsed * 1000:.1f}ms / "
            f"80 主题 {elapsed80 * 1000:.1f}ms / 索引 {index_bytes / 1024:.0f}KB"
        )


# ---------- merged-src 守卫 ----------


class TestMergedSrcGuard(TopicDockTestCase):
    def test_rebuild_excludes_dead_src_from_index(self):
        store = make_store()
        build_merged_store(store)
        code, _ = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = _connect(store)
        try:
            ids = {row[0] for row in conn.execute("SELECT topic_id FROM topic_fts")}
            self.assertEqual(ids, {"alpha", "beta"})
            self.assertEqual(_fts_pairs(conn), _topics_pairs(conn))
        finally:
            conn.close()


# ==========================================================================
# M2：分层召回 / score / recall / hint / similar_topics / 旧库升级 / 三库等价
# ==========================================================================


def _corpus_store(limit=None, path=None) -> Path:
    """物化真实正文语料（磁盘 store 形态）并冷重建落库。"""
    store = make_store() if path is None else _init_at(Path(path))
    materialize_store(store, limit=limit)
    code, data = run_cli_json(
        ["doctor", "--rebuild", "--store-dir", str(store), "--json"]
    )
    if code != 0:
        raise AssertionError(f"doctor --rebuild 失败: exit={code} payload={data!r}")
    return store


def _doc(topic_id, title, *, tracks=None, focus=""):
    doc = topicdock.TopicDoc(topic_id=topic_id, title=title, next_seq={})
    for track, statements in (tracks or {}).items():
        for number, statement in enumerate(statements, 1):
            doc.items[track].append(
                topicdock.TopicItem(
                    item_id=f"{track}{number}", track=track, statement=statement
                )
            )
    if focus:
        doc.latest = topicdock.LatestHop(hop_id="H001", next_focus=focus)
    return doc


def _seed_docs(rows, path=None):
    """在全新 store 的 DB 里种入主题 + 匹配索引（绕过磁盘 md）。

    `path` 传入时在该路径独立 init（同一用例需要多个互不共享的库）；
    缺省按 cwd 解析 `td init`（幂等：同用例多次调用共享同一 store）。
    """
    store = make_store() if path is None else _init_at(Path(path))
    conn = _connect(store)
    for topic_id, title, tracks, updated, focus in rows:
        doc = _doc(topic_id, title, tracks=tracks, focus=focus)
        refresh_match_index(conn, topic_id, doc, "sha-" + topic_id)
        conn.execute(
            "INSERT INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
            " next_seq_json, md_sha256) VALUES(?,?,?,?,?,?,?)",
            (topic_id, title, "H001", updated, "tester", "{}", "sha-" + topic_id),
        )
    conn.commit()
    return conn


class TestLabeledQueries(TopicDockTestCase):
    """20 条冻结标注查询：all/partial/any 集合精确匹配；top-1 ≥ 18；forbidden = 0。"""

    def test_twenty_labeled_queries(self):
        store40 = _corpus_store(path=Path(self.tmp_root) / "corpus40")
        store10 = _corpus_store(limit=10, path=Path(self.tmp_root) / "corpus10")
        participates = 0
        top1_hits = 0
        for query in QUERIES:
            store = store10 if query.get("library_size") == 10 else store40
            argv = ["match", "--tier", "any", "--store-dir", str(store), "--json"]
            for topic in query["topics"]:
                argv += ["--topic", topic]
            code, data = run_cli_json(argv)
            self.assertEqual(code, 0, (query["id"], data))
            results = data["results"]
            all_ids = {row["topic_id"] for row in results if row["tier"] == "all"}
            partial_ids = {
                row["topic_id"] for row in results if row["tier"] == "partial"
            }
            any_ids = {row["topic_id"] for row in results if row["tier"] == "any"}
            self.assertEqual(
                all_ids, set(query["expect_all"]), f"{query['id']} all 层集合不符"
            )
            self.assertEqual(
                partial_ids,
                set(query["expect_partial"]),
                f"{query['id']} partial 层集合不符",
            )
            self.assertEqual(
                any_ids, set(query["expect_any"]), f"{query['id']} any 层集合不符"
            )
            self.assertEqual(
                all_ids & set(query["forbidden_all"]),
                set(),
                f"{query['id']} forbidden_all 命中",
            )
            if query["top1_scored"]:
                participates += 1
                top1 = results[0]["topic_id"] if results else None
                self.assertEqual(
                    top1, query["expect_top1"], f"{query['id']} top-1 不符"
                )
                if top1 == query["expect_top1"]:
                    top1_hits += 1
        self.assertGreaterEqual(participates, 18)
        self.assertGreaterEqual(top1_hits, 18, f"top-1 {top1_hits}/{participates}")
        self.assertEqual(top1_hits, participates)  # 冻结标签全中


class TestMatchHintBranches(TopicDockTestCase):
    """design §3.3 / QS-M4：hint 完备四分支（① ② ④）与精准窄命中省略 hint。"""

    def _run(self, store, query_id, tier):
        query = next(item for item in QUERIES if item["id"] == query_id)
        argv = ["match", "--tier", tier, "--store-dir", str(store), "--json"]
        for topic in query["topics"]:
            argv += ["--topic", topic]
        code, data = run_cli_json(argv)
        self.assertEqual(code, 0, (query_id, data))
        return query, data

    def test_branch_zero_all_with_loose_hits(self):
        # Q04：设计+方案+穿透 → all 层归零，partial/any 非空
        store = _corpus_store(path=Path(self.tmp_root) / "hint-branch-1")
        query, data = self._run(store, "Q04", "all")
        partial = len(query["expect_partial"])
        any_term = (
            len(query["expect_all"])
            + len(query["expect_partial"])
            + len(query["expect_any"])
        )
        self.assertEqual(query["expect_all"], [])
        self.assertGreater(partial, 0)
        self.assertGreater(any_term, 0)
        self.assertEqual(data["results"], [])
        self.assertEqual(data["recall"]["returned"], 0)
        self.assertEqual(data["recall"]["partial"], partial)
        self.assertEqual(data["recall"]["any_term"], any_term)
        self.assertEqual(
            data.get("hint"),
            f"无全词命中；{partial} 个主题命中多数、{any_term} 个主题命中任一词元。"
            "可用 --tier partial|any 放宽或换词。",
        )

    def test_branch_narrow_hits_with_loose_extras(self):
        # Q11：all=1，partial=1，any=2 → returned=1，any_term=4
        store = _corpus_store(path=Path(self.tmp_root) / "hint-branch-2")
        query, data = self._run(store, "Q11", "all")
        any_term = (
            len(query["expect_all"])
            + len(query["expect_partial"])
            + len(query["expect_any"])
        )
        returned = len(data["results"])
        self.assertTrue(1 <= returned <= 5)
        self.assertGreater(any_term, returned)
        self.assertEqual(data["recall"]["returned"], returned)
        self.assertEqual(data["recall"]["any_term"], any_term)
        self.assertEqual(
            data.get("hint"),
            f"返回 {returned} 条当前层命中；另有 {any_term - returned} 个主题"
            "只命中部分词元（可用 --tier partial|any 放宽）。",
        )

    def test_precise_narrow_hit_omits_hint(self):
        # Q05：bm25 唯一 all 命中，partial/any 均为空 → 精准窄命中省略 hint
        store = _corpus_store(path=Path(self.tmp_root) / "hint-branch-3")
        query, data = self._run(store, "Q05", "all")
        any_term = (
            len(query["expect_all"])
            + len(query["expect_partial"])
            + len(query["expect_any"])
        )
        returned = len(data["results"])
        self.assertTrue(1 <= returned <= 5)
        self.assertEqual(any_term, returned)
        self.assertEqual(data["recall"]["any_term"], returned)
        self.assertNotIn("hint", data)


class TestTierBoundaries(TopicDockTestCase):
    def _conn(self):
        return _seed_docs([
            ("a", "甲", {"C": ["aa bb cc"]}, "2026-11-01T00:00:01Z", ""),
            ("b", "乙", {"C": ["aa bb"]}, "2026-11-01T00:00:02Z", ""),
            ("c", "丙", {"C": ["aa"]}, "2026-11-01T00:00:03Z", ""),
            ("d", "丁", {"C": ["zz"]}, "2026-11-01T00:00:04Z", ""),
        ])

    def test_three_token_layers(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        results = match_topics(conn, ["aa", "bb", "cc"], tier="any")
        self.assertEqual(
            [(row["topic_id"], row["tier"], row["matched_terms"]) for row in results],
            [("a", "all", 3), ("b", "partial", 2), ("c", "any", 1)],
        )

    def test_two_tokens_have_no_any_layer(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        results = match_topics(conn, ["aa", "bb"], tier="any")
        tiers = {row["topic_id"]: row["tier"] for row in results}
        self.assertEqual(tiers, {"a": "all", "b": "all", "c": "partial"})
        self.assertNotIn("any", {row["tier"] for row in results})
        self.assertEqual([row["tier"] for row in results], ["all", "all", "partial"])

    def test_single_token_all_only(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        results = match_topics(conn, ["aa"], tier="any")
        self.assertEqual(
            {row["topic_id"]: row["tier"] for row in results},
            {"a": "all", "b": "all", "c": "all"},
        )

    def test_ceil_half_boundary_five_tokens(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO topic_fts(topic_id, md_sha256, topic_slug, title, focus, c, q, a, r)"
            " VALUES('e','sha-e','','','','aa bb','','','')"
        )
        conn.execute(
            "INSERT INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
            " next_seq_json, md_sha256) VALUES('e','戊','H001',"
            "'2026-11-01T00:00:05Z','t','{}','sha-e')"
        )
        conn.commit()
        # n=5, ceil(5/2)=3：matched=2 → any；matched=3 → partial
        results = match_topics(conn, ["aa", "bb", "cc", "dd", "ee"], tier="any")
        tiers = {row["topic_id"]: row["tier"] for row in results}
        self.assertEqual(tiers["b"], "any")   # aa bb = 2 < ceil(5/2)=3
        self.assertEqual(tiers["a"], "partial")  # aa bb cc = 3 ∈ [3, 5)


class TestScoreAndRecall(TopicDockTestCase):
    def test_calibrated_constants(self):
        self.assertEqual(topicdock.SCORE_MIN_SPREAD, 1e-5)
        self.assertEqual(topicdock.SCORE_DEGENERATE_BEST, -1e-4)
        self.assertEqual(topicdock.MAX_QUERY_TOKENS, 500)
        self.assertEqual(SCORE_MIN_SPREAD, topicdock.SCORE_MIN_SPREAD)
        self.assertEqual(SCORE_DEGENERATE_BEST, topicdock.SCORE_DEGENERATE_BEST)

    def test_recall_reports_matched_sets(self):
        conn = _seed_docs([
            ("a", "甲", {"C": ["aa bb"]}, "2026-11-01T00:00:01Z", ""),
            ("b", "乙", {"C": ["aa"]}, "2026-11-01T00:00:02Z", ""),
        ])
        self.addCleanup(conn.close)
        recall = match_recall(conn, {"aa", "bb"})
        self.assertEqual(recall["a"]["matched"], {"aa", "bb"})
        self.assertEqual(recall["b"]["matched"], {"aa"})
        self.assertLess(recall["a"]["bm25"], 0.0)

    def test_degenerate_layer_is_all_100_and_updated_desc(self):
        conn = _seed_docs([
            ("a", "甲", {"C": ["aa bb"]}, "2026-11-01T00:00:01Z", ""),
            ("b", "乙", {"C": ["aa bb"]}, "2026-11-01T00:00:02Z", ""),
        ])
        self.addCleanup(conn.close)
        results = match_topics(conn, ["aa", "bb"])
        self.assertEqual(
            [(row["topic_id"], row["score"]) for row in results],
            [("b", 100), ("a", 100)],
        )

    def test_non_degenerate_layer_ranks_by_bm25(self):
        rows = [
            (f"z{index:02d}", f"干扰{index:02d}", {"C": [f"q{index}"]},
             f"2026-11-01T00:00:{index:02d}Z", "")
            for index in range(10)
        ]
        rows += [
            ("title", "配额治理", {}, "2026-11-01T00:01:01Z", ""),
            ("r-track", "无关", {"R": ["配额治理"]}, "2026-11-01T00:01:02Z", ""),
        ]
        conn = _seed_docs(rows)
        self.addCleanup(conn.close)
        results = match_topics(conn, ["配额治理"])
        self.assertEqual([row["topic_id"] for row in results], ["title", "r-track"])
        self.assertEqual(results[0]["score"], 100)
        self.assertEqual(results[1]["score"], 1)

    def test_q12_title_vs_r_weight_swap_flips_top1(self):
        """Q12 字段守卫：title 仅命中 vs R 仅命中；权重互换则 top-1 翻转。"""
        store = _corpus_store(path=Path(self.tmp_root) / "q12-corpus")
        conn = _connect(store)
        self.addCleanup(conn.close)
        normal = match_topics(conn, ["分桶"])
        self.assertEqual(
            [row["topic_id"] for row in normal],
            ["release-canary-retro", "incident-postmortem-record"],
        )
        swapped = "bm25(topic_fts, 0.0, 0.0, 1.2, 1.2, 1.5, 2.0, 2.0, 1.8, 3.0)"
        with mock.patch.object(topicdock, "_MATCH_BM25_RANK", swapped):
            flipped = match_topics(conn, ["分桶"])
        self.assertEqual(
            [row["topic_id"] for row in flipped],
            ["incident-postmortem-record", "release-canary-retro"],
        )

    def test_cumulative_filter_sets(self):
        conn = _seed_docs([
            ("a", "甲", {"C": ["aa bb cc"]}, "2026-11-01T00:00:01Z", ""),
            ("b", "乙", {"C": ["aa bb"]}, "2026-11-01T00:00:02Z", ""),
            ("c", "丙", {"C": ["aa"]}, "2026-11-01T00:00:03Z", ""),
        ])
        self.addCleanup(conn.close)
        summaries = []
        for tier in ("all", "partial", "any"):
            results = match_topics(conn, ["aa", "bb", "cc"], tier=tier)
            summaries.append(
                (
                    sum(1 for row in results if row["tier"] == "all"),
                    sum(1 for row in results if row["tier"] == "partial"),
                    len(results),
                )
            )
        self.assertEqual(summaries, [(1, 0, 1), (1, 1, 2), (1, 1, 3)])


class TestSubstringAttribution(TopicDockTestCase):
    def test_single_phrase_substring_enters_all(self):
        conn = _seed_docs([
            ("auth-jwt", "Auth JWT", {}, "2026-11-01T00:00:01Z", ""),
        ])
        self.addCleanup(conn.close)
        results = match_topics(conn, ["th-jw"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["topic_id"], "auth-jwt")
        self.assertEqual(results[0]["tier"], "all")
        self.assertEqual(results[0]["matched_terms"], 2)

    def test_multi_phrase_substring_does_not_hijack_other_phrase(self):
        conn = _seed_docs([
            ("design-full", "设计穿透", {}, "2026-11-01T00:00:01Z", ""),
            ("design-only", "设计记录", {}, "2026-11-01T00:00:02Z", ""),
        ])
        self.addCleanup(conn.close)
        results = match_topics(conn, ["设计", "穿透"], tier="any")
        tiers = {row["topic_id"]: row["tier"] for row in results}
        self.assertEqual(tiers["design-full"], "all")     # 两短语词元全中
        self.assertEqual(tiers["design-only"], "partial")  # 只满足 "设计"


class TestSimilarTopicsDirect(TopicDockTestCase):
    def test_long_title_positive_and_large_negative(self):
        store = _corpus_store()
        conn = _connect(store)
        self.addCleanup(conn.close)
        positive = similar_topics(conn, SIMILAR_POSITIVE["title"])
        ids = {entry["topic_id"] for entry in positive}
        for topic_id in SIMILAR_POSITIVE["expect_top3"]:
            self.assertIn(topic_id, ids)
        negative = similar_topics(conn, SIMILAR_NEGATIVE["title"])
        for topic_id in SIMILAR_NEGATIVE["forbidden"]:
            self.assertNotIn(topic_id, {entry["topic_id"] for entry in negative})
        for entry in positive:
            self.assertEqual(set(entry), {"topic_id", "title", "score"})
            self.assertGreaterEqual(entry["score"], 35)

    def test_body_hits_do_not_count(self):
        conn = _seed_docs([
            ("alpha", "无关标题", {"C": ["zzunique"]}, "2026-11-01T00:00:01Z", ""),
        ])
        self.addCleanup(conn.close)
        self.assertEqual(similar_topics(conn, "zzunique"), [])

    def test_topic_slug_column_counts(self):
        conn = _seed_docs([
            ("auth-jwt", "无关标题", {}, "2026-11-01T00:00:01Z", ""),
        ])
        self.addCleanup(conn.close)
        results = similar_topics(conn, "jwt")
        self.assertEqual(
            [(entry["topic_id"], entry["score"]) for entry in results],
            [("auth-jwt", 100)],
        )

    def test_fail_open_matrix(self):
        for error in (
            topicdock.MatchIndexError("boom"),
            sqlite3.OperationalError("boom"),
        ):
            with mock.patch.object(topicdock, "similar_topics", side_effect=error):
                self.assertIsNone(
                    topicdock._similar_topics_hint(None, {"title": "x"}, "new")
                )
        with mock.patch.object(topicdock, "_FTS5_AVAILABLE", False):
            self.assertIsNone(
                topicdock._similar_topics_hint(None, {"title": "x"}, "new")
            )


# ---------- M3：similar_topics 校准验收（oracle 由冻结语料机械推导） ----------


class TestSimilarCalibration(TopicDockTestCase):
    """M3 校准：min_coverage=35.0 在冻结语料上正样本全命中、负样本零误报。"""

    def test_long_title_near_duplicate_in_top3(self):
        """(a) 长标题近同题：正样本对进 top3 且覆盖率 ≥ 35（fixture 机械推导）。"""
        store = _corpus_store(path=Path(self.tmp_root) / "calib-pos")
        conn = _connect(store)
        self.addCleanup(conn.close)
        positive = similar_topics(conn, SIMILAR_POSITIVE["title"])
        self.assertLessEqual(len(positive), 3)  # top3 截断上限
        top3_ids = [entry["topic_id"] for entry in positive]
        for topic_id in SIMILAR_POSITIVE["expect_top3"]:
            self.assertIn(topic_id, top3_ids, f"{topic_id} 未进 top3")
        for entry in positive:
            self.assertGreaterEqual(
                entry["score"], 35, f"{entry['topic_id']} 覆盖率 {entry['score']} < 35"
            )

    def test_single_token_title_full_coverage_and_threshold_band(self):
        """(b) |T_title|=1 满覆盖率边界：单 token 命中同 token 主题得 100 并进结果；
        阈值带实测 1/3→33 被滤除、2/3→67 保留（35 的判别带）。

        两个种子库互相独立（独立 store 路径）：边界库只含 cache-topic，
        阈值带库只含 aa / aa-bb，判定不受其他种子主题干扰。
        """
        conn = _seed_docs([
            ("cache-topic", "缓存", {}, "2026-11-01T00:00:01Z", ""),
        ])
        self.addCleanup(conn.close)
        self.assertEqual(
            similar_topics(conn, "缓存"),
            [{"topic_id": "cache-topic", "title": "缓存", "score": 100}],
        )
        band = _seed_docs(
            [
                ("aa", "x1", {}, "2026-11-01T00:00:02Z", ""),
                ("aa-bb", "x2", {}, "2026-11-01T00:00:03Z", ""),
            ],
            path=Path(self.tmp_root) / "calib-band",
        )
        self.addCleanup(band.close)
        # 查询 aa bb cc：slug aa → 1/3=33 < 35 滤除；slug aa-bb → 2/3=67 ≥ 35 保留
        self.assertEqual(
            similar_topics(band, "aa bb cc"),
            [{"topic_id": "aa-bb", "title": "x2", "score": 67}],
        )

    def test_top3_truncation_with_bm25_then_topic_id_tiebreak(self):
        """(c) ≥4 个同覆盖率候选：只返回 3 个，顺序= score DESC, bm25 ASC, topic_id ASC。"""
        conn = _seed_docs([
            ("d-tie", "aa bb", {}, "2026-11-01T00:00:01Z", ""),
            ("b-tie", "aa aa bb", {}, "2026-11-01T00:00:02Z", ""),
            ("a-tie", "aa bb", {}, "2026-11-01T00:00:03Z", ""),
            ("c-tie", "aa bb", {}, "2026-11-01T00:00:04Z", ""),
        ])
        self.addCleanup(conn.close)
        # 前置：4 个候选同覆盖率（放开 limit 全可见，b-tie 词频高 bm25 更优居首）
        full = similar_topics(conn, "aa bb", limit=10)
        self.assertEqual(
            [entry["topic_id"] for entry in full],
            ["b-tie", "a-tie", "c-tie", "d-tie"],
        )
        self.assertEqual([entry["score"] for entry in full], [100] * 4)
        # 默认 limit=3：d-tie 被截断
        self.assertEqual(
            similar_topics(conn, "aa bb"),
            [
                {"topic_id": "b-tie", "title": "aa aa bb", "score": 100},
                {"topic_id": "a-tie", "title": "aa bb", "score": 100},
                {"topic_id": "c-tie", "title": "aa bb", "score": 100},
            ],
        )

    def test_save_path_hint_scores_match_m2_golden(self):
        """(d) save 端到端 hint：分数与 M2 再生 golden 值一致（80 场景 + GOLDEN_B 场景）。

        两个场景各用独立 store（cwd 幂等 init 会共享 <cwd>/.topicdock，
        GOLDEN_B 场景须用独立路径才真正只含 golden-a/golden-b）：

        - 近同题英文 title（M2再生口径值 80 = 4/5，同 test_save_amend_cas ①）；
        - GOLDEN_B 场景独立复现（同 topic_id/title 序列；mutations 只落 C/Q/A/R
          列，不进 {title topic_slug} 两列口径）：67 = 2/3，即 golden
          save_create_second（c6441f07…）内嵌值，由 golden 哈希用例交叉钉死。
        """
        payload = {
            "base_hop": None, "base_sha": None, "session_id": "ses-cal",
            "summary": "首轮建档",
            "mutations": [{"op": "create", "track": "C", "statement": "结论"}],
        }
        store80 = make_store()
        code, _first = run_save(
            store80, {**payload, "topic_id": "alpha", "title": "auth jwt refactor plan"},
            self.tmp_root,
        )
        self.assertEqual(code, 0)
        code, data = run_save(
            store80,
            {**payload, "topic_id": "beta", "title": "auth jwt refactor plan review"},
            self.tmp_root,
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [
            {"topic_id": "alpha", "title": "auth jwt refactor plan", "score": 80},
        ])

        store_b = _init_at(Path(self.tmp_root) / "calib-golden-b")
        code, _first = run_save(
            store_b,
            {**payload, "topic_id": "golden-a", "title": "Golden Alpha 主题"},
            self.tmp_root,
        )
        self.assertEqual(code, 0)
        code, data = run_save(
            store_b,
            {**payload, "topic_id": "golden-b", "title": "Golden Beta 主题"},
            self.tmp_root,
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["similar_topics"], [
            {"topic_id": "golden-a", "title": "Golden Alpha 主题", "score": 67},
        ])

    def test_large_topic_generic_word_short_title_zero_false_positive(self):
        """(e) 大主题+通用词+短标题负样本：min_coverage=35.0 下零误报（机械推导）。

        期望集合 = 标题列含「缓存」的主题（本语料 topic_slug 全 ASCII，slug 列
        不可能命中）；正文大量出现「缓存」的 perf-dataplane-tuning 不得出现。
        """
        store = _corpus_store(path=Path(self.tmp_root) / "calib-neg")
        conn = _connect(store)
        self.addCleanup(conn.close)
        token = SIMILAR_NEGATIVE["title"]
        expected = {
            doc.topic_id for doc in build_topic_docs() if token in doc.title
        }
        self.assertTrue(expected)  # oracle 非空护栏
        results = similar_topics(conn, token)
        self.assertEqual(
            {entry["topic_id"] for entry in results}, expected,
            "负样本集合漂移：返回了标题不含该 token 的主题",
        )
        for entry in results:
            self.assertGreaterEqual(entry["score"], 35)
        for topic_id in SIMILAR_NEGATIVE["forbidden"]:
            self.assertNotIn(
                topic_id, {entry["topic_id"] for entry in results},
                f"{topic_id} 误报：正文词泄漏进 {token!r} 的 similar 结果",
            )


class TestOldStoreUpgrade(TopicDockTestCase):
    def test_old_inverted_store_migrates_to_fts(self):
        store = _rich_live_store()
        conn = _connect(store)
        data_tables_before = {
            table: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))
            for table in ("topics", "hops", "hop_edges", "item_hops", "item_links")
        }
        conn.close()
        # 旧库形态：无 topic_fts、无 match_index_schema、存在 inverted_index
        raw = sqlite3.connect(str(_ctx(store).db_path))
        try:
            raw.execute("DROP TABLE topic_fts")
            raw.execute(
                "CREATE TABLE inverted_index ("
                " token TEXT NOT NULL, topic_id TEXT NOT NULL,"
                " field TEXT NOT NULL, weight REAL NOT NULL,"
                " PRIMARY KEY (token, topic_id, field))"
            )
            raw.execute(
                "INSERT INTO inverted_index(token, topic_id, field, weight)"
                " VALUES('legacy','alpha','C',2.0)"
            )
            raw.execute(
                "DELETE FROM meta WHERE key IN"
                " ('match_index_schema','match_index_skipped')"
            )
            raw.commit()
        finally:
            raw.close()
        conn, _reports = ensure_store_healthy(_ctx(store))
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertNotIn("inverted_index", tables)
            self.assertEqual(_fts_pairs(conn), _topics_pairs(conn))
            schema = conn.execute(
                "SELECT value FROM meta WHERE key='match_index_schema'"
            ).fetchone()
            self.assertEqual(schema["value"], "fts5-perfield-v1")
            data_tables_after = {
                table: sorted(
                    tuple(row) for row in conn.execute(f"SELECT * FROM {table}")
                )
                for table in ("topics", "hops", "hop_edges", "item_hops", "item_links")
            }
            self.assertEqual(data_tables_after, data_tables_before)
            conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('integrity-check')")
        finally:
            conn.close()


class TestThreePathQueryEquivalence(TopicDockTestCase):
    """QS-M3：增量 / migrate / doctor --rebuild 三库上的 20 查询 (topic_id, tier, score) 一致。"""

    def _query_snapshot(self, store):
        conn = get_or_create_db(_ctx(store))
        try:
            out = []
            for query in QUERIES:
                rows = match_topics(
                    conn, [str(topic) for topic in query["topics"]], tier="any"
                )
                out.append(
                    [(row["topic_id"], row["tier"], row["score"]) for row in rows]
                )
            return out
        finally:
            conn.close()

    def test_three_paths_equivalent(self):
        store_inc = make_store()
        materialize_store(store_inc, limit=10)
        _connect(store_inc).close()
        code, _ = run_cli_json(["list", "--store-dir", str(store_inc), "--json"])
        self.assertEqual(code, 0)
        inc = self._query_snapshot(store_inc)

        store_mig = Path(self.tmp_root) / "mig-store"
        shutil.copytree(store_inc, store_mig)
        raw = sqlite3.connect(str(_ctx(store_mig).db_path))
        try:
            raw.execute("DELETE FROM topic_fts")
            raw.execute(
                "DELETE FROM meta WHERE key IN"
                " ('match_index_schema','match_index_skipped')"
            )
            raw.commit()
        finally:
            raw.close()
        code, _ = run_cli_json(["list", "--store-dir", str(store_mig), "--json"])
        self.assertEqual(code, 0)
        mig = self._query_snapshot(store_mig)

        store_reb = Path(self.tmp_root) / "reb-store"
        shutil.copytree(store_inc, store_reb)
        code, _ = run_cli_json(
            ["doctor", "--rebuild", "--store-dir", str(store_reb), "--json"]
        )
        self.assertEqual(code, 0)
        reb = self._query_snapshot(store_reb)

        for index, query in enumerate(QUERIES):
            self.assertEqual(
                [(t, tier) for t, tier, _score in inc[index]],
                [(t, tier) for t, tier, _score in mig[index]],
                f"{query['id']} migrate 集合不符",
            )
            self.assertEqual(
                [(t, tier) for t, tier, _score in inc[index]],
                [(t, tier) for t, tier, _score in reb[index]],
                f"{query['id']} rebuild 集合不符",
            )
            inc_scores = {t: score for t, _tier, score in inc[index]}
            for topic_id, _tier, score in mig[index] + reb[index]:
                self.assertLessEqual(abs(score - inc_scores[topic_id]), 1e-6)
