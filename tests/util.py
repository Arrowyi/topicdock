"""TopicDock 共享测试夹具：HOME/cwd 隔离、CLI 进程内调用、建库助手。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import topicdock

# 导入时刻的真实 HOME（测试进程内所有用例共享同一真实 HOME 基准）
_REAL_HOME = Path.home()


class TopicDockTestCase(unittest.TestCase):
    """隔离 HOME 与 cwd 到临时目录，收尾恢复并断言真实 HOME 无变更。"""

    def setUp(self) -> None:
        self.real_home = _REAL_HOME
        self._real_home_entries = sorted(os.listdir(self.real_home))
        self._real_had_topicdock = (self.real_home / ".topicdock").exists()
        self._real_had_agents = (self.real_home / ".agents").exists()
        self._orig_cwd = os.getcwd()
        self._orig_home = os.environ.get("HOME")

        self.tmp_root = Path(tempfile.mkdtemp(prefix="topicdock-test-")).resolve()
        self.home_dir = self.tmp_root / "home"
        self.home_dir.mkdir()
        self.workspace_dir = self.tmp_root / "workspace"
        self.workspace_dir.mkdir()
        os.environ["HOME"] = str(self.home_dir)
        os.chdir(self.workspace_dir)
        self.addCleanup(self._restore_and_verify)

    def _restore_and_verify(self) -> None:
        os.chdir(self._orig_cwd)
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home
        shutil.rmtree(self.tmp_root, ignore_errors=True)
        # 真实 HOME 顶层条目无变更；.topicdock / .agents 不得因测试新建
        self.assertEqual(sorted(os.listdir(self.real_home)), self._real_home_entries)
        self.assertEqual((self.real_home / ".topicdock").exists(), self._real_had_topicdock)
        self.assertEqual((self.real_home / ".agents").exists(), self._real_had_agents)


def run_cli(argv: list[str]) -> tuple[int, str]:
    """进程内调用 topicdock.main，捕获 stdout，返回 (退出码, stdout 文本)。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = topicdock.main(list(argv))
    return code, buffer.getvalue()


def run_cli_json(argv: list[str]) -> tuple[int, dict]:
    """同 run_cli，但解析 stdout 中第一个 '{' 到最后一个 '}' 的 JSON。"""
    code, out = run_cli(argv)
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AssertionError(f"输出中未找到 JSON 对象: exit={code} stdout={out!r}")
    return code, json.loads(out[start : end + 1])


def run_save(store, payload, tmp, *flags):
    """写载荷文件并调 `td save --json-file`；返回 (退出码, JSON)。"""
    payload_path = Path(tmp) / "save-payload.json"
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    argv = [
        "save", "--json-file", str(payload_path),
        "--store-dir", str(store), "--json", *flags,
    ]
    return run_cli_json(argv)


def make_store(scope: str = "local") -> Path:
    """在当前 HOME/cwd 环境下建库（td init），返回 store 绝对路径。"""
    code, data = run_cli_json(["init", "--scope", scope, "--json"])
    if code != 0 or data.get("status") != "OK":
        raise AssertionError(f"make_store 失败: exit={code} payload={data!r}")
    return Path(data["store_dir"])


# ---------- M4 语料构建：hop 分片 + 顶层 md（经 L2 内核正向构建，独立于 L4） ----------


def write_hop(
    store_root,
    hop_id,
    topic_origin,
    parents,
    blocks,
    *,
    session_id="ses-01",
    actor="tester",
    created_at="2026-11-01T00:00:00Z",
    title=None,
    artifacts=None,
    summary="",
    next_focus="",
):
    """构造并原子写一个合法 hop 分片，返回 (路径, header)。"""
    header = {
        "v": 1,
        "hop_id": hop_id,
        "topic_origin": topic_origin,
        "title": title if title is not None else f"{topic_origin} 主题标题",
        "parents": list(parents),
        "session_id": session_id,
        "created_at": created_at,
        "actor": actor,
        "summary": summary or f"hop {hop_id}",
        "next_focus": next_focus,
        "artifacts": [
            {"name": name, "path": path, "note": note or ""}
            for name, path, note in (artifacts or [])
        ],
    }
    hop_doc = topicdock.HopDoc(header=header, blocks=list(blocks))
    path = Path(store_root) / "hops" / topicdock.hop_file_name(hop_id)
    topicdock.atomic_write_text(path, topicdock.render_hop_md(hop_doc))
    return path, header


class ChainBuilder:
    """语料链构建器：写 hop 分片 + 用 L2 apply_hop 正向累积文档并渲染标准 md。

    与 L4 重放路径相互独立（仅共享已测的 L2 内核），用于交叉验证
    「L2 正向构建 == L4 反向重放」。
    """

    def __init__(self, store_root, topic_id, title):
        self.store_root = Path(store_root)
        self.topic_id = topic_id
        self.doc = topicdock.TopicDoc(topic_id=topic_id, title=title)
        self.records: dict = {}
        self.overturns: dict = {}
        self.head = None
        self.head_sha = ""

    def add_hop(self, hop_id, parents, blocks, **header_over):
        path, header = write_hop(
            self.store_root, hop_id, self.topic_id, parents, blocks,
            title=self.doc.title, **header_over
        )
        topicdock.apply_hop(self.doc, header, list(blocks))
        for block in blocks:
            self.records.setdefault(block.item_id, []).append(hop_id)
            if block.op == "overturn":
                origin_hop = str(block.origin or "").split("@")[-1] or hop_id
                self.overturns[block.item_id] = (origin_hop, hop_id)
        self.head = hop_id
        self.head_sha = topicdock.file_sha256(path)
        return path

    def md_text(self) -> str:
        if self.doc.latest is not None:
            self.doc.latest.sha12 = self.head_sha[:12]
        tags = {
            item_id: topicdock.format_trace_tag(records)
            for item_id, records in self.records.items()
        }
        for item_id, (origin_hop, overturn_hop) in self.overturns.items():
            tags[item_id] = topicdock.format_trace_tag(
                None, origin_hop=origin_hop, overturn_hop=overturn_hop
            )
        return topicdock.render_topic_md(self.doc, tags)

    def md_path(self) -> Path:
        return self.store_root / "topics" / f"{self.topic_id}.md"

    def write_md(self) -> str:
        text = self.md_text()
        topicdock.atomic_write_text(self.md_path(), text)
        return text


ALPHA_TITLE = "認證模塊無狀態 JWT 改造與灰度遷移 / Auth Stateless JWT Refactor"
ALPHA_ARTIFACT = ("网关架构设计记录", "/abs/path/design.md", "定义 JWT 载荷字段与公钥轮换接口")


def build_rich_store(store_root):
    """三主题语料（全存活）：alpha 覆盖 create/update/overturn/resolve/fold 全 op、
    beta fork 自 alpha@H002（共享祖先 H001/H002）、gamma 独立单跳。"""
    alpha = ChainBuilder(store_root, "alpha", ALPHA_TITLE)
    alpha.add_hop("H001", [], [
        topicdock.HopBlock(item_id="C1", op="create", status="verified",
            fields={"结论正文": "外部网关统一签发 EdDSA 短效 JWT", "一句话依据": "压测 P99 延迟降至 1.2ms"}),
        topicdock.HopBlock(item_id="A1", op="create", a_state="~",
            fields={"结论正文": "完成网关鉴权原型压测", "断点": "待敲定回调签名头格式"}),
    ], created_at="2026-11-01T00:00:00Z")
    alpha.add_hop("H002", ["H001"], [
        topicdock.HopBlock(item_id="A1", op="update", a_state="x",
            fields={"结论正文": "完成网关鉴权原型压测与算法选型"}),
        topicdock.HopBlock(item_id="C2", op="create", status="inferred",
            fields={"结论正文": "Refresh Token 滑动窗口旋转", "一句话依据": "兼顾免登体验与吊销能力"}),
        topicdock.HopBlock(item_id="Q1", op="create",
            fields={"结论正文": "Webhook 回调鉴权是否走同一网关验签中间件"}),
    ], created_at="2026-11-01T00:01:00Z")
    alpha.add_hop("H003", ["H002"], [
        topicdock.HopBlock(item_id="R1", op="overturn", from_items=["C1"], origin="alpha:C1@H001",
            fields={"结论正文": "放弃全链路 Session 共享库", "一句话原因": "跨区同步延迟导致偶发 401"}),
        topicdock.HopBlock(item_id="C3", op="resolve", from_items=["Q1"], origin="alpha:Q1@H002",
            status="verified",
            fields={"结论正文": "Webhook 回调走同一验签中间件", "一句话依据": "复用网关签名校验逻辑"}),
        topicdock.HopBlock(item_id="A2", op="create", a_state="x",
            fields={"结论正文": "编写接口契约与错误码定义"}),
    ], created_at="2026-11-01T00:02:00Z")
    alpha.add_hop("H004", ["H003"], [
        topicdock.HopBlock(item_id="A3", op="fold", from_items=["A1", "A2"],
            fields={"结论正文": "完成第一阶段网关鉴权原型、契约设计与中间件开发"}),
        topicdock.HopBlock(item_id="C4", op="create", status="verified",
            fields={"结论正文": "Ed25519 公钥轮换接口定稿", "一句话依据": "验签吞吐高 3.4 倍"}),
    ], created_at="2026-11-01T00:03:00Z",
        artifacts=[ALPHA_ARTIFACT], summary="折叠 A1/A2，定稿公钥轮换",
        next_focus="推进中间件开发")
    alpha.write_md()

    beta = ChainBuilder(store_root, "beta", "Webhook 验签分支")
    beta.add_hop("H005", ["H002"], [
        topicdock.HopBlock(item_id="C1", op="inherit", origin="alpha:C1@H001",
            fields={"结论正文": "外部网关统一签发 EdDSA 短效 JWT", "一句话依据": "压测 P99 延迟降至 1.2ms"}),
        topicdock.HopBlock(item_id="C2", op="inherit", origin="alpha:C2@H002",
            fields={"结论正文": "Refresh Token 滑动窗口旋转", "一句话依据": "兼顾免登体验与吊销能力"}),
        topicdock.HopBlock(item_id="A1", op="inherit", a_state="x", origin="alpha:A1@H002",
            fields={"结论正文": "完成网关鉴权原型压测与算法选型"}),
        topicdock.HopBlock(item_id="C5", op="create", status="inferred",
            fields={"结论正文": "Webhook 签名头采用 EdDSA", "一句话依据": "与主链算法对齐"}),
    ], created_at="2026-11-01T00:04:00Z")
    beta.write_md()

    gamma = ChainBuilder(store_root, "gamma", "压测基准子题")
    gamma.add_hop("H006", [], [
        topicdock.HopBlock(item_id="C1", op="create", status="verified",
            fields={"结论正文": "压测脚本基准建立", "一句话依据": "bench 结果落库"}),
        topicdock.HopBlock(item_id="Q1", op="create",
            fields={"结论正文": "压测是否纳入 CI 门槛"}),
    ], created_at="2026-11-01T00:05:00Z")
    gamma.write_md()
    return {"alpha": alpha, "beta": beta, "gamma": gamma}


def build_merged_store(store_root):
    """在 rich 语料上落合流终态：alpha 并入 gamma（H007 双父），gamma.md 已删。

    gamma 的 hop H006 因被合流跳引用而存活——验证已并流 src 派生行（复审 #8）。
    """
    builders = build_rich_store(store_root)
    alpha = builders["alpha"]
    alpha.add_hop("H007", ["H004", "H006"], [
        topicdock.HopBlock(item_id="C5", op="inherit", origin="gamma:C1@H006",
            fields={"结论正文": "压测脚本基准建立", "一句话依据": "bench 结果落库"}),
        topicdock.HopBlock(item_id="Q2", op="inherit", origin="gamma:Q1@H006",
            fields={"结论正文": "压测是否纳入 CI 门槛"}),
    ], created_at="2026-11-01T00:06:00Z", summary="并入 gamma 压测基准子题")
    alpha.write_md()
    (Path(store_root) / "topics" / "gamma.md").unlink()
    return builders


def build_big_corpus(store_root, n_topics=100, hops_per_topic=5):
    """QS-1 性能语料：n_topics 条线性链 × hops_per_topic 跳，全部独立主题。"""
    builders = []
    hop_number = 1
    for t in range(1, n_topics + 1):
        topic_id = f"t{t:03d}"
        builder = ChainBuilder(store_root, topic_id, f"主题 {topic_id}")
        parent = []
        for h in range(hops_per_topic):
            hop_id = f"H{hop_number:03d}"
            if h == 0:
                blocks = [
                    topicdock.HopBlock(item_id="C1", op="create", status="verified",
                        fields={"结论正文": f"{topic_id} 的核心结论", "一句话依据": "基准证据"}),
                    topicdock.HopBlock(item_id="A1", op="create", a_state="~",
                        fields={"结论正文": "启动首轮行动", "断点": "待细化"}),
                ]
            elif h == 1:
                blocks = [
                    topicdock.HopBlock(item_id="A1", op="update", a_state="x",
                        fields={"结论正文": "完成首轮行动"}),
                    topicdock.HopBlock(item_id="C2", op="create", status="inferred",
                        fields={"结论正文": "次要结论", "一句话依据": "推导依据"}),
                ]
            elif h == 2:
                blocks = [topicdock.HopBlock(item_id="C3", op="create", status="verified",
                    fields={"结论正文": "补充结论", "一句话依据": "补充证据"})]
            elif h == 3:
                blocks = [topicdock.HopBlock(item_id="Q1", op="create",
                    fields={"结论正文": "遗留问题"})]
            else:
                blocks = [topicdock.HopBlock(item_id="A2", op="create", a_state="x",
                    fields={"结论正文": "收尾行动"})]
            builder.add_hop(
                hop_id, parent, blocks,
                created_at=f"2026-11-02T{(hop_number // 3600) % 24:02d}:{(hop_number // 60) % 60:02d}:{hop_number % 60:02d}Z",
            )
            parent = [hop_id]
            hop_number += 1
        builder.write_md()
        builders.append(builder)
    return builders


def dump_tables(conn, include_meta=True):
    """QS-1 快照（D-P23 口径）：六表全量 + meta 的 schema_version/next_hop_seq。"""
    out = {}
    for table in ("topics", "hops", "hop_edges", "item_hops", "item_links", "inverted_index"):
        out[table] = sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))
    if include_meta:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        out["meta"] = {
            "schema_version": meta.get("schema_version"),
            "next_hop_seq": meta.get("next_hop_seq"),
        }
    return out
