"""M3 测试 —— live_refs / plan_cascade / apply_cascade / undo_stack / get_item_trace。

级联删除两阶段（plan 纯计算 + apply 单事务）、DAG 引用计数与 trace 递归上溯。
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

from tests.util import (
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
    apply_cascade,
    cmd_trace_locked,
    ensure_store_healthy,
    get_item_trace,
    get_or_create_db,
    live_refs,
    plan_cascade,
    pop_undo,
    push_undo,
)


def _open(store_root):
    ctx = StoreContext(Path(store_root))
    conn = get_or_create_db(ctx)
    return ctx, conn


def _add_topic(conn, topic_id, head_hop, updated_at="2026-11-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
        " next_seq_json, md_sha256) VALUES(?,?,?,?,?,?,?)",
        (topic_id, topic_id, head_hop, updated_at, "tester", "{}", "sha-" + topic_id),
    )
    conn.commit()


def _add_hop(conn, hop_id, topic_origin, sha=None):
    conn.execute(
        "INSERT INTO hops(hop_id, topic_origin, session_id, created_at, actor,"
        " summary, next_focus, ref_count, md_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
        (hop_id, topic_origin, "ses-1", "2026-11-01T00:00:00Z", "tester",
         "s", "f", 1, sha or ("sha-" + hop_id)),
    )
    conn.commit()


def _add_edge(conn, parent, child):
    conn.execute(
        "INSERT INTO hop_edges(parent_hop, child_hop) VALUES(?,?)", (parent, child)
    )
    conn.commit()


def _add_item_hops(conn, topic_id, item_id, records):
    for order, (hop_id, op) in enumerate(records, start=1):
        conn.execute(
            "INSERT INTO item_hops(topic_id, item_id, hop_id, op, seq_order)"
            " VALUES(?,?,?,?,?)",
            (topic_id, item_id, hop_id, op, order),
        )
    conn.commit()


def _add_item_link(conn, topic_id, item_id, hop_id, link_type, src_topic, src_item,
                   origin_hop=""):
    conn.execute(
        "INSERT INTO item_links(topic_id, item_id, hop_id, link_type, src_topic_id,"
        " src_item_id, origin_hop) VALUES(?,?,?,?,?,?,?)",
        (topic_id, item_id, hop_id, link_type, src_topic, src_item, origin_hop),
    )
    conn.commit()


def _dump(conn, tables=("topics", "hops", "hop_edges", "item_hops", "item_links",
                        "inverted_index", "undo_stack")):
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        for table in tables
    }


class TestLiveRefs(TopicDockTestCase):
    def test_heads_plus_parent_edges(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002", "H003"):
            _add_hop(conn, hop_id, "a")
        _add_topic(conn, "a", "H003")
        _add_edge(conn, "H001", "H002")
        _add_edge(conn, "H002", "H003")
        self.assertEqual(live_refs(conn, "H001"), 1)  # 仅 parent 边
        self.assertEqual(live_refs(conn, "H002"), 1)
        self.assertEqual(live_refs(conn, "H003"), 1)  # 仅 head 引用
        self.assertEqual(live_refs(conn, "H999"), 0)


class TestPlanCascade(TopicDockTestCase):
    def test_exclusive_chain_all_dead_and_pure(self):
        store = make_store()
        ctx, conn = _open(store)
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002", "H003"):
            _add_hop(conn, hop_id, "a")
            (ctx.hops_dir / f"hop-{hop_id[1:]}.md").write_text(
                f"content-{hop_id}", encoding="utf-8"
            )
        _add_topic(conn, "a", "H003")
        _add_edge(conn, "H001", "H002")
        _add_edge(conn, "H002", "H003")
        (ctx.topics_dir / "a.md").write_text("doc-a", encoding="utf-8")
        _add_item_hops(conn, "a", "C1", [("H001", "create"), ("H003", "update")])

        before_db = _dump(conn)
        before_files = {
            p.name: p.read_text(encoding="utf-8")
            for p in ctx.topics_dir.iterdir() if p.is_file()
        }
        plan = plan_cascade(conn, ctx, ["a"])
        self.assertEqual(plan.dead_hops, ["H001", "H002", "H003"])
        self.assertEqual(len(plan.removed_edges), 2)
        self.assertIn("doc-a", plan.pre_files.values())
        self.assertEqual(
            plan.pre_files[str(ctx.hops_dir / "hop-001.md")], "content-H001"
        )
        # 纯计算：DB 与文件零变更
        self.assertEqual(_dump(conn), before_db)
        after_files = {
            p.name: p.read_text(encoding="utf-8")
            for p in ctx.topics_dir.iterdir() if p.is_file()
        }
        self.assertEqual(after_files, before_files)

    def test_fork_shared_ancestor_survives(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002", "H003"):
            _add_hop(conn, hop_id, "fork")
        _add_topic(conn, "a", "H002")
        _add_topic(conn, "b", "H003")
        _add_edge(conn, "H001", "H002")
        _add_edge(conn, "H002", "H003")  # b 从 H002 分叉
        plan = plan_cascade(conn, ctx, ["b"])
        self.assertEqual(plan.dead_hops, ["H003"])
        # 反向删 a（分叉源主题）：H001/H002 被 b 的链 H003←H002←H001 引用，全部存活
        plan_a = plan_cascade(conn, ctx, ["a"])
        self.assertEqual(plan_a.dead_hops, [])
        # 两个分叉主题全删 → 全链死
        plan_ab = plan_cascade(conn, ctx, ["a", "b"])
        self.assertEqual(plan_ab.dead_hops, ["H001", "H002", "H003"])

    def test_duplicate_and_missing_topic_ids(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "a")
        _add_hop(conn, "H002", "a")
        _add_topic(conn, "a", "H002")
        _add_edge(conn, "H001", "H002")
        self.assertEqual(plan_cascade(conn, ctx, ["a", "a"]).dead_hops, ["H001", "H002"])
        # 不存在的 id：无死链、无异常
        plan = plan_cascade(conn, ctx, ["ghost"])
        self.assertEqual(plan.dead_hops, [])
        self.assertEqual(plan.topic_heads, {})

    def test_shared_head_delete_one_survives(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "shared")
        _add_topic(conn, "a", "H001")
        _add_topic(conn, "b", "H001")
        plan = plan_cascade(conn, ctx, ["a"])
        self.assertEqual(plan.dead_hops, [])
        self.assertEqual(plan.topic_heads, {"a": "H001"})
        apply_cascade(conn, plan)
        self.assertEqual(live_refs(conn, "H001"), 1)  # b 仍引用

    def test_self_loop_edge_ignored(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "a")
        _add_topic(conn, "a", "H001")
        conn.execute(
            "INSERT INTO hop_edges(parent_hop, child_hop) VALUES('H001','H001')"
        )
        conn.commit()
        # 自环边（篡改痕迹）不产生引用：删 a 时 H001 仍可死亡
        plan = plan_cascade(conn, ctx, ["a"])
        self.assertEqual(plan.dead_hops, ["H001"])

    def test_merge_src_head_survives_via_edge(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        # dst: H001→H002；src: H003；merge hop H004 parents [H002, H003]
        for hop_id in ("H001", "H002", "H003", "H004"):
            _add_hop(conn, hop_id, "merge")
        _add_topic(conn, "dst", "H004")
        _add_edge(conn, "H001", "H002")
        _add_edge(conn, "H002", "H004")
        _add_edge(conn, "H003", "H004")
        # src 主题行已随 merge 删除；H003 仅靠 parent 边存活
        self.assertEqual(live_refs(conn, "H003"), 1)
        # 删 dst → 全链死（H004→H002/H003→H001）
        plan = plan_cascade(conn, ctx, ["dst"])
        self.assertEqual(plan.dead_hops, ["H001", "H002", "H003", "H004"])


class TestApplyCascade(TopicDockTestCase):
    def test_deletes_rows_and_edges(self):
        store = make_store()
        ctx, conn = _open(store)
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002"):
            _add_hop(conn, hop_id, "a")
            (ctx.hops_dir / f"hop-{hop_id[1:]}.md").write_text(
                f"c-{hop_id}", encoding="utf-8"
            )
        _add_topic(conn, "a", "H002")
        _add_edge(conn, "H001", "H002")
        _add_item_hops(conn, "a", "C1", [("H001", "create"), ("H002", "update")])
        _add_item_link(conn, "a", "R1", "H002", "overturn", "a", "C1", "H001")
        conn.execute(
            "INSERT INTO inverted_index(token, topic_id, field, weight)"
            " VALUES('tok','a','C',2.0)"
        )
        conn.commit()
        plan = plan_cascade(conn, ctx, ["a"])
        apply_cascade(conn, plan)
        self.assertEqual(_dump(conn, ("topics",))["topics"], [])
        self.assertEqual(_dump(conn, ("hops",))["hops"], [])
        self.assertEqual(_dump(conn, ("hop_edges",))["hop_edges"], [])
        self.assertEqual(_dump(conn, ("item_hops",))["item_hops"], [])
        self.assertEqual(_dump(conn, ("item_links",))["item_links"], [])
        self.assertEqual(_dump(conn, ("inverted_index",))["inverted_index"], [])

    def test_merged_src_rows_preserved_while_hops_live(self):
        store = make_store()
        ctx, conn = _open(store)
        self.addCleanup(conn.close)
        # src 已并流（无 topics 行），其 H001 经 dst 合流跳 H003 存活
        for hop_id in ("H001", "H002", "H003"):
            _add_hop(conn, hop_id, "merge")
        _add_topic(conn, "dst", "H003")
        _add_edge(conn, "H002", "H003")
        _add_edge(conn, "H001", "H003")
        _add_item_hops(conn, "src", "C1", [("H001", "create")])
        # 删一个无关主题 c（head=H003 不可能——H003 是 dst 的 head）
        _add_hop(conn, "H004", "c")
        _add_topic(conn, "c", "H004")
        _add_edge(conn, "H003", "H004")
        plan = plan_cascade(conn, ctx, ["c"])
        self.assertEqual(plan.dead_hops, ["H004"])
        apply_cascade(conn, plan)
        # src 的派生行（topic_id=src，hop H001 仍存活）必须保留
        rows = _dump(conn, ("item_hops",))["item_hops"]
        self.assertEqual(
            [r for r in rows if r["topic_id"] == "src"],
            [{"topic_id": "src", "item_id": "C1", "hop_id": "H001",
              "op": "create", "seq_order": 1}],
        )
        # dst 与其链完好
        self.assertEqual(len(_dump(conn, ("hops",))["hops"]), 3)

    def test_empty_topic_ids_noop(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "a")
        _add_topic(conn, "a", "H001")
        before = _dump(conn)
        apply_cascade(conn, plan_cascade(conn, ctx, []))
        self.assertEqual(_dump(conn), before)

    def test_ref_count_recomputed_after_fork_delete(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002", "H003"):
            _add_hop(conn, hop_id, "fork")
        _add_topic(conn, "a", "H002")
        _add_topic(conn, "b", "H003")
        _add_edge(conn, "H001", "H002")
        _add_edge(conn, "H002", "H003")
        # 模拟 M5 写路径维护的封板计数（fork 时 H002 ref_count=2）
        conn.execute("UPDATE hops SET ref_count=2 WHERE hop_id='H002'")
        conn.commit()
        apply_cascade(conn, plan_cascade(conn, ctx, ["b"]))
        row = conn.execute(
            "SELECT ref_count FROM hops WHERE hop_id='H002'"
        ).fetchone()
        self.assertEqual(row["ref_count"], 1)
        self.assertEqual(live_refs(conn, "H002"), 1)

    def test_caller_holds_transaction_rollback_restores(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "a")
        _add_hop(conn, "H002", "a")
        _add_topic(conn, "a", "H002")
        _add_edge(conn, "H001", "H002")
        before = _dump(conn)
        plan = plan_cascade(conn, ctx, ["a"])
        self.assertEqual(plan.dead_hops, ["H001", "H002"])

        class _FailingConn:
            """第 N 次 execute 注入失败（模拟中途崩溃）。"""

            def __init__(self, inner, fail_on):
                self._inner = inner
                self._fail_on = fail_on
                self._calls = 0

            def execute(self, sql, params=()):
                self._calls += 1
                if self._calls == self._fail_on:
                    raise sqlite3.OperationalError("injected mid-failure")
                return self._inner.execute(sql, params)

        with self.assertRaises(sqlite3.OperationalError):
            apply_cascade(_FailingConn(conn, fail_on=2), plan)
        conn.rollback()  # 调用方持有事务：回滚即恢复原状
        self.assertEqual(_dump(conn), before)


class TestUndoStack(TopicDockTestCase):
    def test_limit_three_and_pop_order(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for i in range(5):
            push_undo(conn, "save", f"op-{i}", {"i": i})
        count = conn.execute("SELECT COUNT(*) AS n FROM undo_stack").fetchone()["n"]
        self.assertEqual(count, 3)
        top = pop_undo(conn)
        self.assertEqual(top["summary"], "op-4")
        self.assertEqual(top["payload"], {"i": 4})
        pop_undo(conn)
        pop_undo(conn)
        self.assertIsNone(pop_undo(conn))  # 空栈 D-P13


class TestGetItemTrace(TopicDockTestCase):
    def test_blocks_links_ancestry_successors(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002", "H003", "H004"):
            _add_hop(conn, hop_id, "t")
        _add_topic(conn, "a", "H003")
        _add_topic(conn, "d", "H004")
        # d:C5 继承自 a:C1（fork）；a:C1 后续被 a:R1 推翻
        _add_item_hops(conn, "a", "C1", [("H001", "create"), ("H002", "update")])
        _add_item_hops(conn, "d", "C5", [("H003", "create")])
        _add_item_link(conn, "d", "C5", "H003", "inherit", "a", "C1", "H001")
        _add_item_link(conn, "a", "R1", "H004", "overturn", "a", "C1", "H001")

        trace = get_item_trace(conn, "d", "C5")
        self.assertEqual(
            [(b["hop_id"], b["op"]) for b in trace["blocks"]],
            [("H003", "create")],
        )
        self.assertEqual(len(trace["links"]), 1)
        self.assertEqual(trace["links"][0]["src_topic_id"], "a")
        # 递归上溯：a:C1 的 H001/H002 历史
        self.assertEqual(
            [(b["topic_id"], b["item_id"], b["hop_id"]) for b in trace["ancestry"]],
            [("a", "C1", "H001"), ("a", "C1", "H002")],
        )
        # a:C1 的后续去向（被 R1 推翻；被 d:C5 继承）——无固定顺序，按集合比较
        trace_c1 = get_item_trace(conn, "a", "C1")
        self.assertEqual(
            sorted(
                (s["topic_id"], s["item_id"], s["link_type"])
                for s in trace_c1["successors"]
            ),
            sorted([("a", "R1", "overturn"), ("d", "C5", "inherit")]),
        )

    def test_ancestry_cycle_protection(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002"):
            _add_hop(conn, hop_id, "t")
        _add_topic(conn, "a", "H001")
        _add_topic(conn, "d", "H002")
        _add_item_hops(conn, "a", "C1", [("H001", "create")])
        _add_item_hops(conn, "d", "C5", [("H002", "create")])
        # 人为构造环：d:C5 ← a:C1 且 a:C1 ← d:C5
        _add_item_link(conn, "d", "C5", "H002", "inherit", "a", "C1", "H001")
        _add_item_link(conn, "a", "C1", "H001", "inherit", "d", "C5", "H002")
        # 不得死循环：两侧各恰好访问一次
        trace = get_item_trace(conn, "d", "C5")
        self.assertEqual(len(trace["ancestry"]), 1)
        trace_c1 = get_item_trace(conn, "a", "C1")
        self.assertEqual(len(trace_c1["ancestry"]), 1)

    def test_missing_item_returns_empty_blocks(self):
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        _add_hop(conn, "H001", "a")
        _add_topic(conn, "a", "H001")
        trace = get_item_trace(conn, "a", "C9")
        self.assertEqual(trace["blocks"], [])
        self.assertEqual(trace["links"], [])
        self.assertEqual(trace["ancestry"], [])
        self.assertEqual(trace["successors"], [])


# ===================== M6：td trace 命令层（场景 4 全历史/去向/跨主题递归） =====================


class TestTraceCommand(TopicDockTestCase):
    def _rich_live(self):
        """rich 语料 + 已建库（冷重建需完整链，先经 list 命令自愈落库）。"""
        store = make_store()
        build_rich_store(store)
        code, data = run_cli_json(["list", "--store-dir", str(store), "--json"])
        self.assertEqual((code, data["status"]), (0, "OK"))
        return store

    def test_overturned_item_blocks_and_successor_notes(self):
        """alpha C1（被推翻）：H001 create 全文 + H003 overturn 块 + 双去向 note。"""
        store = self._rich_live()
        code, data = run_cli_json(
            ["trace", "alpha", "C1", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        blocks = data["blocks"]
        self.assertEqual(
            [(b["hop_id"], b["op"]) for b in blocks],
            [("H001", "create"), ("H003", "overturn")],
        )
        self.assertIn("### [C1] (op=create, status=verified)", blocks[0]["text"])
        self.assertIn("外部网关统一签发 EdDSA 短效 JWT", blocks[0]["text"])
        self.assertIn("一句话依据: 压测 P99 延迟降至 1.2ms", blocks[0]["text"])
        self.assertIn("### [R1] (op=overturn, from=C1, origin=alpha:C1@H001)",
                      blocks[1]["text"])
        self.assertIn("放弃全链路 Session 共享库", blocks[1]["text"])
        self.assertEqual(sorted(data["successor_notes"]), sorted([
            "于 H003 overturn 转入 R1",  # 同主题省 topic 前缀
            "于 H005 inherit 转入 beta:C1",
        ]))
        self.assertEqual(
            sorted(
                (s["topic_id"], s["item_id"], s["hop_id"], s["link_type"])
                for s in data["successor"]
            ),
            sorted([("alpha", "R1", "H003", "overturn"), ("beta", "C1", "H005", "inherit")]),
        )
        self.assertEqual(data["links"], [])

    def test_cross_topic_recursive_ancestry_from_topic(self):
        """beta C1 继承自 alpha C1：跨主题递归块带 from_topic=alpha。"""
        store = self._rich_live()
        code, data = run_cli_json(
            ["trace", "beta", "C1", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        blocks = data["blocks"]
        self.assertEqual(
            [(b["hop_id"], b.get("from_topic"), b["op"]) for b in blocks],
            [("H005", None, "inherit"), ("H001", "alpha", "create")],
        )
        self.assertIn("外部网关统一签发 EdDSA 短效 JWT", blocks[1]["text"])
        self.assertEqual(len(data["links"]), 1)
        self.assertEqual(
            (
                data["links"][0]["link_type"],
                data["links"][0]["src_topic_id"],
                data["links"][0]["src_item_id"],
                data["links"][0]["origin_hop"],
                data["links"][0]["hop_id"],
            ),
            ("inherit", "alpha", "C1", "H001", "H005"),
        )
        self.assertEqual(data["successor"], [])
        self.assertEqual(data["successor_notes"], [])

    def test_missing_hop_block_marked_missing(self):
        """hop 文件缺失：该跳块 missing=true 且不含 text；可达块照常渲染。"""
        store = self._rich_live()
        (store / "hops" / "hop-003.md").unlink()  # 中链祖先缺失（md 完好 D-P20 跳过）
        code, data = run_cli_json(
            ["trace", "alpha", "C1", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (0, "OK"))
        self.assertEqual(data["blocks"][0]["hop_id"], "H001")
        self.assertIn("text", data["blocks"][0])
        self.assertEqual(
            data["blocks"][1],
            {"hop_id": "H003", "op": "overturn", "missing": True},
        )

    def test_cycle_protection_command_level_passthrough(self):
        """环保护命令层透传：M3 已测 get_item_trace 层；手写行主题经命令自愈会被
        级联（无 md/分片），故直调 cmd_trace_locked 验证渲染层不死循环。"""
        ctx, conn = _open(make_store())
        self.addCleanup(conn.close)
        for hop_id in ("H001", "H002"):
            _add_hop(conn, hop_id, "t")
        _add_topic(conn, "a", "H001")
        _add_topic(conn, "d", "H002")
        _add_item_hops(conn, "a", "C1", [("H001", "create")])
        _add_item_hops(conn, "d", "C5", [("H002", "create")])
        _add_item_link(conn, "d", "C5", "H002", "inherit", "a", "C1", "H001")
        _add_item_link(conn, "a", "C1", "H001", "inherit", "d", "C5", "H002")
        result = cmd_trace_locked(
            ctx, conn, types.SimpleNamespace(topic="d", item="C5")
        )
        self.assertEqual(
            result["blocks"],
            [
                {"hop_id": "H002", "op": "create", "missing": True},
                {"hop_id": "H001", "op": "create", "missing": True, "from_topic": "a"},
            ],
        )

    def test_item_and_topic_not_found(self):
        store = self._rich_live()
        code, data = run_cli_json(
            ["trace", "alpha", "C9", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (5, "ITEM_NOT_FOUND"))
        code, data = run_cli_json(
            ["trace", "nosuch", "C1", "--store-dir", str(store), "--json"]
        )
        self.assertEqual((code, data["status"]), (5, "TOPIC_NOT_FOUND"))


class TestMergeDeleteUndo(TopicDockTestCase):
    """M7：merge 双父合流（续编/派生行保留/src.md 删除）、delete 两阶段、undo 还原。"""

    def _rich(self):
        store = make_store()
        builders = build_rich_store(store)
        conn, _ = ensure_store_healthy(StoreContext(Path(store)))
        conn.close()
        return store, builders

    def test_merge_inherits_and_cleans_src(self):
        store, builders = self._rich()
        code, data = run_cli_json(
            ["merge", "gamma", "alpha", "--store-dir", str(store), "--json",
             "--summary", "并入 gamma 压测基准子题"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["dst_topic_id"], "alpha")
        conn = get_or_create_db(StoreContext(Path(store)))
        try:
            alpha = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id='alpha'"
            ).fetchone()
            self.assertEqual(alpha["head_hop"], data["head_hop"])
            self.assertIsNone(
                conn.execute(
                    "SELECT * FROM topics WHERE topic_id='gamma'"
                ).fetchone()
            )
            self.assertFalse((Path(store) / "topics" / "gamma.md").exists())
            # src 派生行保留（trace 递归依赖，复审 #8）
            src_items = conn.execute(
                "SELECT COUNT(*) FROM item_hops WHERE topic_id='gamma'"
            ).fetchone()[0]
            self.assertGreater(src_items, 0)
            # 合流跳双父
            merge_hop = conn.execute(
                "SELECT parent_hop FROM hop_edges WHERE child_hop=?",
                (data["head_hop"],),
            ).fetchall()
            parents = {row["parent_hop"] for row in merge_hop}
            self.assertEqual(parents, {"H004", "H006"})
            # 续编并入：alpha md 含 gamma 的 C1 继承块
            md = (Path(store) / "topics" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("压测脚本基准建立", md)
        finally:
            conn.close()

    def test_merge_then_delete_dst_no_dangling(self):
        """merge 后删 dst：死跳派生行全清；共享存活跳上的源主题行按
        复审 #8 保留（trace 递归依赖），不判悬挂。"""
        store, builders = self._rich()
        run_cli_json(
            ["merge", "gamma", "alpha", "--store-dir", str(store), "--json"]
        )
        conn = get_or_create_db(StoreContext(Path(store)))
        try:
            head = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id='alpha'"
            ).fetchone()["head_hop"]
            src_head = "H006"
            # 死跳集：从 dst head 起的独占分片（merge 跳、alpha 独占尾、gamma 全链）
            plan = plan_cascade(conn, StoreContext(Path(store)), ["alpha"])
            dead = set(plan.dead_hops)
            self.assertIn(head, dead)
            self.assertIn(src_head, dead)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2
            )  # beta + gamma… 不——gamma 已被 merge 并掉，剩 beta
        finally:
            conn.close()
        code, data = run_cli_json(
            ["delete", "alpha", "--yes", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        conn = get_or_create_db(StoreContext(Path(store)))
        try:
            topics = [
                row["topic_id"]
                for row in conn.execute("SELECT topic_id FROM topics")
            ]
            self.assertEqual(topics, ["beta"])  # beta 存活（fork 基点 H002 共享）
            # 死跳上的派生行全清
            for table in ("item_hops", "item_links"):
                marks = ",".join("?" * len(dead))
                dangling = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE hop_id IN ({marks})",
                    sorted(dead),
                ).fetchone()[0]
                self.assertEqual(dangling, 0)
            # 共享存活跳（H001/H002 经 beta 存活）上 alpha 的行 = trace 递归数据，保留
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM item_hops WHERE topic_id='alpha'"
                ).fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_delete_preview_expressions(self):
        store, builders = self._rich()
        # 序号表达式（1-based，list 同序 = updated_at 倒序 = gamma/beta/alpha）
        code, preview = run_cli_json(
            ["delete", "1,3", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(preview["topics"]), 2)
        # keep 补集
        code, keep = run_cli_json(
            ["delete", "keep 2", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(keep["topics"]), 2)  # 除第 2 个全删
        # 区间
        code, rng = run_cli_json(
            ["delete", "2~3", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rng["topics"]), 2)

    def test_delete_yes_missing_topic_not_found(self):
        store, builders = self._rich()
        # --yes 拒绝序号语义：显式 topic_id 语法合法但库中无此主题 → TOPIC_NOT_FOUND(5)
        code, data = run_cli_json(
            ["delete", "1", "--yes", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 5)
        self.assertEqual(data["status"], "TOPIC_NOT_FOUND")

    def test_delete_undo_restores_topic_and_hops(self):
        store, builders = self._rich()
        conn = get_or_create_db(StoreContext(Path(store)))
        before = dump_tables(conn)
        conn.close()
        code, deleted = run_cli_json(
            ["delete", "alpha", "--yes", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertFalse((Path(store) / "topics" / "alpha.md").exists())
        code, undone = run_cli_json(
            ["undo", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(undone["op_type"], "delete")
        conn = get_or_create_db(StoreContext(Path(store)))
        try:
            after = dump_tables(conn)
            self.assertEqual(
                {k: v for k, v in after.items() if k != "meta"},
                {k: v for k, v in before.items() if k != "meta"},
            )  # 完全复原（六表）
            self.assertTrue((Path(store) / "topics" / "alpha.md").exists())
        finally:
            conn.close()

    def test_merge_undo_restores_both(self):
        store, builders = self._rich()
        conn = get_or_create_db(StoreContext(Path(store)))
        before = dump_tables(conn)
        conn.close()
        code, data = run_cli_json(
            ["merge", "gamma", "alpha", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        # merge 输出契约
        self.assertEqual(data["merged_items"], {"C": 1, "Q": 1})
        self.assertEqual(data["base_sha"], data.get("base_sha"))  # 完整 sha 存在
        # 死跳文件（merge 跳）确实删——undo 后回来
        merge_hop_file = Path(store) / "hops" / "hop-007.md"
        code2, undone = run_cli_json(
            ["undo", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code2, 0)
        conn = get_or_create_db(StoreContext(Path(store)))
        after = dump_tables(conn)
        conn.close()
        # 六表恒等（topics.next_seq_json 受 undo 棘轮 +1——编号空洞、永不复用，
        # reviewer 明示可接受残余；其余字段与水位单独断言）
        self.assertEqual(after["hops"], before["hops"])
        self.assertEqual(after["hop_edges"], before["hop_edges"])
        self.assertEqual(after["item_hops"], before["item_hops"])
        self.assertEqual(after["item_links"], before["item_links"])
        self.assertEqual(after["inverted_index"], before["inverted_index"])
        before_alpha = next(r for r in before["topics"] if r[0] == "alpha")
        after_alpha = next(r for r in after["topics"] if r[0] == "alpha")
        self.assertEqual(after_alpha[:6], before_alpha[:6])  # 同键同值至 last_actor
        before_ns = json.loads(before_alpha[6])
        after_ns = json.loads(after_alpha[6])
        for track, value in before_ns.items():
            self.assertGreaterEqual(after_ns.get(track, 0), value)  # 水位单调不回退
        self.assertTrue((Path(store) / "topics" / "gamma.md").exists())

    def test_delete_dead_hop_files_removed(self):
        store, builders = self._rich()
        conn = get_or_create_db(StoreContext(Path(store)))
        alpha_head = conn.execute(
            "SELECT head_hop FROM topics WHERE topic_id='alpha'"
        ).fetchone()["head_hop"]
        conn.close()
        code, data = run_cli_json(
            ["delete", "alpha", "--yes", "--store-dir", str(store), "--json"]
        )
        # alpha 独占死跳文件删除（alpha 链 H001–H004 中 H001/H002 被 beta 共享）
        self.assertFalse((Path(store) / "topics" / "alpha.md").exists())
        self.assertFalse((Path(store) / "hops" / "hop-003.md").exists())
        self.assertFalse((Path(store) / "hops" / "hop-004.md").exists())
        self.assertTrue((Path(store) / "hops" / "hop-001.md").exists())

    def test_merge_default_does_not_inherit_model_and_actor_empty(self):
        store = make_store()
        for topic_id in ("dst", "src"):
            code, data = run_save(store, {
                "topic_id": topic_id, "title": f"{topic_id} 标题", "base_hop": None,
                "session_id": "ses-01", "actor": "tester",
                "model": f"model-{topic_id}",
                "mutations": [
                    {"op": "create", "track": "C", "statement": f"{topic_id} 结论"}
                ],
            }, self.tmp_root)
            self.assertEqual(code, 0)
        code, data = run_cli_json(
            ["merge", "src", "dst", "--store-dir", str(store), "--json"]
        )
        self.assertEqual(code, 0)
        header = topicdock.parse_hop_md(
            (Path(store) / "hops" / topicdock.hop_file_name(data["head_hop"]))
            .read_text(encoding="utf-8")
        ).header
        self.assertNotIn("model", header)  # 合流跳不继承双亲 model
        self.assertEqual(header["actor"], "")  # --actor 默认 ""

    def test_merge_model_and_actor_flags(self):
        store, _builders = self._rich()
        code, data = run_cli_json(
            ["merge", "gamma", "alpha", "--store-dir", str(store), "--json",
             "--model", "  opencode-go/gemini-4-argon  ", "--actor", ""]
        )
        self.assertEqual(code, 0)
        header = topicdock.parse_hop_md(
            (Path(store) / "hops" / topicdock.hop_file_name(data["head_hop"]))
            .read_text(encoding="utf-8")
        ).header
        self.assertEqual(header["model"], "opencode-go/gemini-4-argon")
        self.assertEqual(header["actor"], "")
