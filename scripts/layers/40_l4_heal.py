# ===================== L4: SelfHeal & Undo =====================

"""L4 自愈引擎（design §2.3，D-P20/21/23/33/35）：七步自愈序 ⑦→⑥→③→①→②→⑤→④。

调用方须持排他锁（D-P2）。本层自行管理 DB 事务并在成功路径统一提交；
崩溃中断后下次自愈按幂等语义收敛。冷重建写 `topicdock.db.tmp` 后 `os.replace`
原子替换并删旧 `-wal/-shm`（D-P12）。
"""

# ---------- 共享重放内核（反向路径，D-P22） ----------

_HOP_FILE_RE = re.compile(r"^hop-(\d+)\.md$")
_TOPIC_FILE_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,63})\.md$")

_AMEND_PENDING_NAME = ".topicdock-amend-pending.json"
_PUBLISHED_LEDGER_NAME = ".topicdock-published.json"

# 版本守卫短读上限：远超任何合法 frontmatter，防上限误伤真实高版本文件。
_SCHEMA_HEADER_SCAN_LIMIT = 64 * 1024
_SCHEMA_SCAN_CHUNK = 8192
_UNIVERSAL_LINE_RE = re.compile(rb"\r\n|\r|\n")


# ---------- 独立本地封板台账（design §2.3，D8） ----------

def read_published_oids(store: StoreContext) -> tuple[set[str], bool]:
    """读封板台账：返回 (已封板 oid 集, is_corrupt)。

    - 文件缺失 → `(set(), False)`（无封板记录，不触发失败关闭）；
    - 其它读取失败（目录占位、权限不足等）/ JSON 语法损坏 / `v != 1` /
      `published_oids` 非字符串列表 → `(set(), True)`，调用方须按失败关闭处理
      （拒绝原地 amend 与一切 undo）。
    """
    path = store.root / _PUBLISHED_LEDGER_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set(), False
    except (OSError, ValueError):
        return set(), True
    try:
        data = json.loads(text)
    except ValueError:
        return set(), True
    if not isinstance(data, dict):
        return set(), True
    version = data.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        return set(), True
    raw = data.get("published_oids")
    if not isinstance(raw, list) or any(not isinstance(oid, str) for oid in raw):
        return set(), True
    return set(raw), False


def mark_published_oids(store: StoreContext, oids) -> None:
    """把 `oids` 并入封板台账并原子落盘 `{"v": 1, "published_oids": sorted([...])}`。

    锁约定：调用方必须已持 `store.exclusive_lock()`；本函数内部不再加锁。
    无 CLI 暴露（供未来 export/sync 与测试直调）。

    失败关闭：台账已损坏（`is_corrupt == True`）时**拒绝覆写**并抛 `InvalidInput`，
    原台账字节保持不变——绝不静默遗忘既有封板记录。
    """
    existing, is_corrupt = read_published_oids(store)
    if is_corrupt:
        raise InvalidInput(
            "封板台账损坏（失败关闭）：拒绝覆写，请先人工修复或删除该台账"
        )
    merged = set(existing)
    merged.update(str(oid) for oid in oids)
    atomic_write_text(
        store.root / _PUBLISHED_LEDGER_NAME,
        json.dumps(
            {"v": 1, "published_oids": sorted(merged)},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
    )


def _md_frontmatter(text: str) -> str:
    """取 md 的 frontmatter 区间（第一对 `---` 之间）；未闭合/无 frontmatter → 空串。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def _read_md_frontmatter_head(path: Path) -> str | None:
    """增量读 topic md 至 frontmatter 闭合 `---`（字节上限 64 KiB）。

    返回 frontmatter 文本；无 frontmatter 或未在字节上限内闭合 → `""`（不判定）；
    读失败 / 非 UTF-8 → `None`（不判定，交七步处置）。
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None
    with handle:
        buf = b""
        closed_at = -1
        while len(buf) < _SCHEMA_HEADER_SCAN_LIMIT:
            chunk = handle.read(
                min(_SCHEMA_SCAN_CHUNK, _SCHEMA_HEADER_SCAN_LIMIT - len(buf))
            )
            if not chunk:
                break
            buf += chunk
            lines = _UNIVERSAL_LINE_RE.split(buf)
            if not lines or lines[0].strip() != b"---":
                return ""  # 无 frontmatter
            for index in range(1, len(lines)):
                if lines[index].strip() == b"---":
                    closed_at = index
                    break
            if closed_at >= 0:
                break
    lines = _UNIVERSAL_LINE_RE.split(buf)
    if not buf or not lines or lines[0].strip() != b"---":
        return ""
    if closed_at < 0:
        return ""  # 未在字节上限内闭合：维持现语义不判定
    head = b"\n".join(lines[: closed_at + 1])
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _md_frontmatter(text)


def _raise_on_newer_schema(store: StoreContext) -> None:
    """版本守卫前置（design §4.2(1)）：任一 md/hop 高于本引擎版本 → 抛
    UnsupportedSchemaVersion；在任何写盘/删盘之前执行，杜绝旧引擎自愈销毁
    新版本数据。文件头读失败（损坏/乱码）不作判定，交七步处置。
    只扫 frontmatter/注释头区间——正文行不参与判定（正文含
    `schema_version: 2` 不得锁死全库）；topic 增量读至 frontmatter 闭合，
    hop 仅读首行。"""
    for md_path in sorted(store.topics_dir.glob("*.md")):
        frontmatter = _read_md_frontmatter_head(md_path)
        if not frontmatter:
            continue
        match = re.search(
            r"^schema_version:\s*(\d+)\s*$", frontmatter, re.MULTILINE
        )
        if match and int(match.group(1)) > SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"topics md schema_version={match.group(1)}: {md_path.name}"
            )
    for hop_path in sorted(store.hops_dir.glob("hop-*.md")):
        try:
            with hop_path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline().rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            continue
        if not (
            first_line.startswith(_HOP_HEADER_PREFIX)
            and first_line.endswith(_HOP_HEADER_SUFFIX)
        ):
            continue
        try:
            header = json.loads(
                first_line[len(_HOP_HEADER_PREFIX) : -len(_HOP_HEADER_SUFFIX)]
            )
        except ValueError:
            continue
        if not isinstance(header, dict):
            continue  # 非 dict 头交七步按损坏处置，守卫自身不崩
        try:
            version = int(header.get("v", 1))
        except (TypeError, ValueError):
            continue
        if version > SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(f"hop v={version}: {hop_path.name}")


@dataclass
class LoadedHop:
    """已通过合法性校验的 hop 分片（解析结果 + 文件哈希）。"""

    hop: HopDoc
    sha: str
    path: Path


@dataclass
class ReplayedChain:
    """一条 own 链重放产物：文档、条目记录与可入库派生行。"""

    doc: TopicDoc
    hops: list[HopDoc] = field(default_factory=list)  # 时间正序（首跳→head）
    records: dict[str, list[str]] = field(default_factory=dict)  # item_id -> [hop_id]（演进序）
    create_hops: dict[str, str] = field(default_factory=dict)  # item_id -> 初始创建跳
    item_hops_rows: list[tuple] = field(default_factory=list)  # (topic, item, hop, op, seq_order)
    item_links_rows: list[tuple] = field(default_factory=list)
    overturns: dict[str, tuple[str, str]] = field(default_factory=dict)  # R item -> (原立, 推翻)


def _normalize_merkle_extension_header(header: dict) -> None:
    """D8 读侧容错：4 扩展字段现值类型异常 → 安全空默认（不新增缺失键、不回写）。"""
    if "topic_uid" in header and not isinstance(header["topic_uid"], str):
        header["topic_uid"] = ""
    if "parent_oids" in header:
        raw = header["parent_oids"]
        header["parent_oids"] = (
            [str(x) for x in raw] if isinstance(raw, list) else []
        )
    if "origin_oids" in header:
        raw = header["origin_oids"]
        header["origin_oids"] = (
            {str(k): str(v) for k, v in raw.items()}
            if isinstance(raw, dict) else {}
        )
    if "hop_oid" in header and not isinstance(header["hop_oid"], str):
        header["hop_oid"] = ""


def _load_hop(
    store: StoreContext, hop_id: str, cache: dict[str, LoadedHop | None] | None = None
) -> LoadedHop | None:
    """加载并校验 hop 分片；缺失/不可解析/头失配/自环 parent → None（判损坏）。

    `cache` 为单次命令调用内的 `hop_id -> LoadedHop|None` 缓存（trace 批量追根
    共享，同一分片只读盘一次）。单次读盘：raw 字节直接算 SHA-256（与
    `file_sha256` 恒等），再按通用换行语义归一（CRLF 与孤立 `\\r`）后解析。
    """
    key = str(hop_id)
    if cache is not None and key in cache:
        return cache[key]
    loaded = _load_hop_uncached(store, key)
    if cache is not None:
        cache[key] = loaded
    return loaded


def _load_hop_uncached(store: StoreContext, hop_id: str) -> LoadedHop | None:
    if not re.fullmatch(r"H\d+", hop_id):
        return None
    path = store.hops_dir / hop_file_name(hop_id)
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        hop = parse_hop_md(text)
    except (OSError, ValueError):
        return None
    header = hop.header
    for key in _HOP_HEADER_REQUIRED:
        if key not in header:
            return None
    if str(header.get("hop_id", "")) != hop_id:
        return None
    try:
        version = int(header.get("v", 1))
    except (TypeError, ValueError):
        return None
    if version > SUPPORTED_SCHEMA_VERSION:
        return None
    parents = header.get("parents")
    if not isinstance(parents, list):
        return None  # parents 非列表（篡改痕迹）判损坏，不在此处抛 TypeError
    parents = [str(p) for p in parents]
    if any(not re.fullmatch(r"H\d+", p) for p in parents):
        return None
    if hop_id in parents:  # 自环 parent = 篡改痕迹，判损坏（F11 #6）
        return None
    if not isinstance(header.get("title", ""), str):
        return None
    artifacts = header.get("artifacts")
    if not isinstance(artifacts, list) or any(
        not isinstance(a, dict) for a in artifacts
    ):
        return None
    _normalize_merkle_extension_header(header)
    return LoadedHop(hop=hop, sha=hashlib.sha256(raw).hexdigest(), path=path)


def _reachable_hops(store: StoreContext, head_hop: str) -> dict[str, LoadedHop] | None:
    """head 起沿全部 parents 递归可达集；任一缺失/非法 → None。"""
    out: dict[str, LoadedHop] = {}
    stack = [str(head_hop)]
    while stack:
        hop_id = stack.pop()
        if hop_id in out:
            continue
        loaded = _load_hop(store, hop_id)
        if loaded is None:
            return None
        out[hop_id] = loaded
        for parent in loaded.hop.header.get("parents") or []:
            stack.append(str(parent))
    return out


def _walk_origin_chain(
    store: StoreContext, head_hop: str, topic_id: str
) -> tuple[list[LoadedHop] | None, str | None, str | None]:
    """首父回溯至 topic_origin 边界的 own 链（时间正序）。

    返回 (chain, missing_blocker, invalid_blocker)：
    - chain 完整合法 → (list, None, None)；
    - 阻断于缺失文件 → (None, hop_id, None)；
    - 阻断于存在但非法的文件 → (None, None, hop_id)。
    """
    chain_rev: list[LoadedHop] = []
    seen: set[str] = set()
    cur: str | None = str(head_hop)
    loaded: LoadedHop | None = None
    while cur is not None:
        if cur in seen:
            return None, None, cur
        seen.add(cur)
        if loaded is None:
            loaded = _load_hop(store, cur)
            if loaded is None:
                path = store.hops_dir / hop_file_name(cur)
                return None, (None if path.is_file() else cur), (cur if path.is_file() else None)
        if loaded.hop.header.get("topic_origin") != topic_id:
            return None, None, cur
        chain_rev.append(loaded)
        parents = [str(p) for p in (loaded.hop.header.get("parents") or [])]
        nxt = parents[0] if parents else None
        if nxt is None:
            break
        parent_loaded = _load_hop(store, nxt)
        if parent_loaded is None:
            path = store.hops_dir / hop_file_name(nxt)
            return None, (None if path.is_file() else nxt), (nxt if path.is_file() else None)
        if parent_loaded.hop.header.get("topic_origin") != topic_id:
            break  # 边界：fork 基点在链外
        cur = nxt
        loaded = parent_loaded
    chain_rev.reverse()
    return chain_rev, None, None


def _origin_chain_from_reachable(
    reachable: dict[str, LoadedHop], head_hop: str, topic_id: str
) -> list[LoadedHop] | None:
    """从可达集内取 own 链（时间正序）；用于冷重建/纳管的行构建路径。"""
    chain_rev: list[LoadedHop] = []
    seen: set[str] = set()
    cur: str | None = str(head_hop)
    while cur is not None:
        if cur in seen:
            return None
        seen.add(cur)
        loaded = reachable.get(cur)
        if loaded is None or loaded.hop.header.get("topic_origin") != topic_id:
            return None
        chain_rev.append(loaded)
        parents = [str(p) for p in (loaded.hop.header.get("parents") or [])]
        nxt = parents[0] if parents else None
        if nxt is not None:
            parent_loaded = reachable.get(nxt)
            if parent_loaded is None or parent_loaded.hop.header.get("topic_origin") != topic_id:
                nxt = None
        cur = nxt
    chain_rev.reverse()
    return chain_rev


def _record_block_rows(topic_id: str, hop_id: str, block: HopBlock, rep: ReplayedChain) -> None:
    """按块累积 item_hops / item_links 派生行与条目演进记录。"""
    records = rep.records.setdefault(block.item_id, [])
    records.append(hop_id)
    rep.item_hops_rows.append((topic_id, block.item_id, hop_id, block.op, len(records)))
    op = block.op
    creates = op in ("create", "inherit", "overturn", "fold") or (
        op == "resolve" and block.item_id != block.from_items[0]
    )
    if creates:
        rep.create_hops.setdefault(block.item_id, hop_id)
    if op == "inherit":
        parsed = _parse_origin(block.origin)
        if parsed is not None:
            src_topic, src_item, origin_hop = parsed
            rep.item_links_rows.append(
                (topic_id, block.item_id, hop_id, "inherit", src_topic, src_item, origin_hop)
            )
    elif op == "overturn":
        src_item = block.from_items[0] if block.from_items else ""
        parsed = _parse_origin(block.origin)
        src_topic = parsed[0] if parsed else topic_id
        origin_hop = parsed[2] if parsed else rep.create_hops.get(src_item, "")
        rep.item_links_rows.append(
            (topic_id, block.item_id, hop_id, "overturn", src_topic, src_item, origin_hop)
        )
        rep.overturns[block.item_id] = (origin_hop, hop_id)
    elif op == "resolve" and block.item_id != block.from_items[0]:
        src_item = block.from_items[0]
        parsed = _parse_origin(block.origin)
        src_topic = parsed[0] if parsed else topic_id
        origin_hop = parsed[2] if parsed else rep.create_hops.get(src_item, "")
        rep.item_links_rows.append(
            (topic_id, block.item_id, hop_id, "resolve", src_topic, src_item, origin_hop)
        )
    elif op == "fold":
        for src_item in block.from_items:
            rep.item_links_rows.append(
                (
                    topic_id,
                    block.item_id,
                    hop_id,
                    "fold",
                    topic_id,
                    src_item,
                    rep.create_hops.get(src_item, ""),
                )
            )


def _replay_chain(topic_id: str, chain: list[LoadedHop]) -> ReplayedChain:
    """own 链 → TopicDoc + 派生行（正反两路共用 apply_hop_block/apply_hop 内核）。"""
    doc = TopicDoc(topic_id=topic_id)
    rep = ReplayedChain(doc=doc, hops=[loaded.hop for loaded in chain])
    for loaded in chain:
        header = loaded.hop.header
        hop_id = str(header.get("hop_id", ""))
        ctx = ApplyContext(hop_id=hop_id, new_seq=dict(doc.next_seq))
        for block in loaded.hop.blocks:
            apply_hop_block(doc, block, ctx)
            _record_block_rows(topic_id, hop_id, block, rep)
        doc.next_seq = dict(ctx.new_seq)
        apply_hop(doc, header, [])  # 头部级字段唯一内核（D-P22）
    return rep


def _apply_next_seq_floor(doc: TopicDoc, floor: dict | None) -> None:
    """重放 next_seq 与既有高水位逐轨取 max（design §2.3 单调编号防重用不变量）。"""
    if not floor:
        return
    for track, value in floor.items():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        current = int(doc.next_seq.get(track, 0) or 0)
        doc.next_seq[track] = max(current, value)


def _render_replayed_text(
    rep: ReplayedChain, head_sha: str, next_seq_floor: dict | None = None
) -> str:
    """重放产物 → 标准 md 全文（含行尾短标与 `## 4.` 的 head sha12）。

    next_seq_floor：既有 md frontmatter / DB topics 行的高水位，逐轨取 max 后
    再渲染——修复/校准/纳管绝不回退条目编号。
    """
    _apply_next_seq_floor(rep.doc, next_seq_floor)
    if rep.doc.latest is not None:
        rep.doc.latest.sha12 = head_sha[:12]
    tags: dict[str, str] = {}
    for item_id, records in rep.records.items():
        tags[item_id] = format_trace_tag(records)
    for item_id, (origin_hop, overturn_hop) in rep.overturns.items():
        tags[item_id] = format_trace_tag(records=None, origin_hop=origin_hop, overturn_hop=overturn_hop)
    return render_topic_md(rep.doc, tags)


# ---------- 行写入辅助 ----------

def _upsert_hops_rows(conn: sqlite3.Connection, reachable: dict[str, LoadedHop]) -> None:
    """可达集内「尚未登记」的 hop 建行 + 全部 parent 边（ref_count 随后统一重算）。

    已登记行保持原 md_sha256 不动——纳管追平不得洗白祖先的既有登记，
    失配判定留给 ⑤；边用 INSERT OR IGNORE 幂等补齐。
    """
    for hop_id, loaded in reachable.items():
        if conn.execute("SELECT 1 FROM hops WHERE hop_id=?", (hop_id,)).fetchone() is not None:
            continue
        header = loaded.hop.header
        conn.execute(
            "INSERT OR REPLACE INTO hops(hop_id, topic_origin, session_id, created_at,"
            " actor, summary, next_focus, ref_count, md_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                hop_id,
                str(header.get("topic_origin", "")),
                str(header.get("session_id", "")),
                str(header.get("created_at", "")),
                str(header.get("actor", "")),
                str(header.get("summary", "")),
                str(header.get("next_focus", "")),
                0,
                loaded.sha,
            ),
        )
        for parent in header.get("parents") or []:
            conn.execute(
                "INSERT OR IGNORE INTO hop_edges(parent_hop, child_hop) VALUES(?,?)",
                (str(parent), hop_id),
            )


def _insert_replayed_rows(conn: sqlite3.Connection, rep: ReplayedChain, replace: bool) -> None:
    """写入重放派生行；replace=True 时先删同主题 own 链既有行（幂等重放）。"""
    topic_id = rep.doc.topic_id
    own_hops = [str(h.header.get("hop_id", "")) for h in rep.hops]
    if replace and own_hops:
        marks = ",".join("?" * len(own_hops))
        conn.execute(
            f"DELETE FROM item_hops WHERE topic_id=? AND hop_id IN ({marks})",
            [topic_id, *own_hops],
        )
        conn.execute(
            f"DELETE FROM item_links WHERE topic_id=? AND hop_id IN ({marks})",
            [topic_id, *own_hops],
        )
    conn.executemany(
        "INSERT OR REPLACE INTO item_hops(topic_id, item_id, hop_id, op, seq_order)"
        " VALUES(?,?,?,?,?)",
        rep.item_hops_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO item_links(topic_id, item_id, hop_id, link_type,"
        " src_topic_id, src_item_id, origin_hop) VALUES(?,?,?,?,?,?,?)",
        rep.item_links_rows,
    )


def _upsert_topic_row(
    conn: sqlite3.Connection, store: StoreContext, rep: ReplayedChain, md_sha: str
) -> None:
    doc = rep.doc
    head_header = rep.hops[-1].header if rep.hops else {}
    conn.execute(
        "INSERT OR REPLACE INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
        " next_focus, next_seq_json, md_sha256) VALUES(?,?,?,?,?,?,?,?)",
        (
            doc.topic_id,
            doc.title,
            doc.head_hop,
            doc.updated_at or str(head_header.get("created_at", "")),
            doc.last_actor,
            str(head_header.get("next_focus", "")),
            json.dumps(doc.next_seq, ensure_ascii=False, sort_keys=True),
            md_sha,
        ),
    )
    refresh_inverted_index(conn, doc.topic_id, doc)


def _merged_src_heads(
    reachable: dict[str, LoadedHop], head_hop: str
) -> list[str]:
    """合流 src 头：head 为多父合流跳时，parents[1:] 中 origin != 本主题的父。

    单父（fork 首跳/普通推进）绝不是合流——防止 fork 崩溃窗口纳管时误删源主题。
    """
    loaded = reachable.get(str(head_hop))
    if loaded is None:
        return []
    parents = [str(p) for p in (loaded.hop.header.get("parents") or [])]
    if len(parents) < 2:
        return []
    own = str(loaded.hop.header.get("topic_origin", ""))
    out = []
    for parent in parents[1:]:
        parent_loaded = reachable.get(parent)
        if parent_loaded is not None and parent_loaded.hop.header.get("topic_origin") != own:
            out.append(parent)
    return out


def _origins_in_reachable(reachable: dict[str, LoadedHop]) -> set[str]:
    """可达集中出现的全部 topic_origin（含已并流 src）。"""
    return {
        str(loaded.hop.header.get("topic_origin", "")) for loaded in reachable.values()
    }


# ---------- ⑦ 冷重建 ----------

def _preserve_undo_stack(store: StoreContext, target_conn: sqlite3.Connection) -> None:
    """冷重建保留既有 undo 栈行（撤回计划只存在于 DB——重建不得清空
    LIMIT 3 剩余栈，否则 undo 一次后多步回滚全部丢失）；旧库损坏/缺失 → 空。"""
    if not store.db_path.is_file():
        return
    try:
        source = sqlite3.connect(str(store.db_path))
        source.row_factory = sqlite3.Row
    except sqlite3.Error:
        return
    try:
        for row in source.execute(
            "SELECT created_at, op_type, summary, payload_json FROM undo_stack"
            " ORDER BY undo_id"
        ).fetchall():
            target_conn.execute(
                "INSERT INTO undo_stack(created_at, op_type, summary, payload_json)"
                " VALUES(?,?,?,?)",
                (row["created_at"], row["op_type"], row["summary"], row["payload_json"]),
            )
    except sqlite3.DatabaseError:
        pass
    finally:
        source.close()


def rebuild_db_from_markdown(store: StoreContext) -> list[dict]:
    """`topicdock.db.tmp` 全表重建（D-P12/D-P23）：语法合法 + ID 一致 + 跳链完整的主题
    100% 入库；损坏/失配/链不完整主题跳过并清理其文件与孤立分片；重建后校准顶层短标。

    本函数独立完成「建 tmp 库 → 原子替换 → 文件清理」全过程，不依赖调用方事务。
    """
    reports: list[dict] = []
    _raise_on_newer_schema(store)
    tmp_path = store.db_path.with_name(store.db_path.name + ".tmp")
    for suffix in ("", "-wal", "-shm"):
        try:
            (store.root / (tmp_path.name + suffix)).unlink()
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(str(tmp_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")

    valid: list[dict] = []
    skipped: list[str] = []
    for md_path in sorted(store.topics_dir.glob("*.md")):
        match = _TOPIC_FILE_RE.match(md_path.name)
        if match is None:
            continue
        topic_id = match.group(1)
        try:
            doc = parse_topic_md(md_path.read_text(encoding="utf-8"))
            if doc.topic_id != topic_id or not doc.head_hop:
                skipped.append(topic_id)
                continue
            reachable = _reachable_hops(store, doc.head_hop)
            chain = (
                _origin_chain_from_reachable(reachable, doc.head_hop, topic_id)
                if reachable is not None
                else None
            )
            if reachable is None or chain is None:
                skipped.append(topic_id)
                continue
            rep = _replay_chain(topic_id, chain)
            text = _render_replayed_text(rep, chain[-1].sha, doc.next_seq)
        except (OSError, ValueError):
            skipped.append(topic_id)
            continue
        valid.append(
            {"topic_id": topic_id, "reachable": reachable, "rep": rep, "text": text}
        )

    skipped = list(dict.fromkeys(skipped))
    all_reachable: dict[str, LoadedHop] = {}
    for entry in valid:
        all_reachable.update(entry["reachable"])
    _upsert_hops_rows(conn, all_reachable)

    built_origins: set[str] = set()
    for entry in valid:
        topic_id, rep, text = entry["topic_id"], entry["rep"], entry["text"]
        md_path = store.topics_dir / f"{topic_id}.md"
        if md_path.read_text(encoding="utf-8") != text:  # 校准漂移短标/篡改内容
            atomic_write_text(md_path, text)
        _insert_replayed_rows(conn, rep, replace=False)
        _upsert_topic_row(conn, store, rep, sha256_text(text))
        built_origins.add(topic_id)
        # 可达集内已并流 src（无存活 md）的派生行：item_hops/item_links 按 origin 建行（复审 #8）
        for origin in _origins_in_reachable(entry["reachable"]):
            if origin in built_origins:
                continue
            origin_hops = sorted(
                (
                    hop_id
                    for hop_id in all_reachable
                    if str(all_reachable[hop_id].hop.header.get("topic_origin", "")) == origin
                ),
                key=_hop_number,
            )
            try:
                rep_x = _replay_chain(origin, [all_reachable[h] for h in origin_hops])
            except ValueError:
                continue
            _insert_replayed_rows(conn, rep_x, replace=False)
            built_origins.add(origin)

    refresh_ref_counts(conn, all_reachable.keys())
    _preserve_undo_stack(store, conn)
    number = next_hop_seq_value(store, conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('next_hop_seq', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(number),),
    )
    skipped = list(dict.fromkeys(skipped))
    conn.commit()
    conn.close()
    os.replace(tmp_path, store.db_path)
    for suffix in ("-wal", "-shm"):
        try:
            (store.root / (store.db_path.name + suffix)).unlink()
        except FileNotFoundError:
            pass

    deferred: list[Path] = []
    for topic_id in skipped:
        md_path = store.topics_dir / f"{topic_id}.md"
        if md_path.is_file():
            md_path.unlink()
            deferred.append(md_path)
    live_ids = set(all_reachable)
    for hop_path in sorted(store.hops_dir.glob("hop-*.md")):
        match = _HOP_FILE_RE.match(hop_path.name)
        if match is None:
            continue
        if f"H{int(match.group(1)):03d}" not in live_ids:
            hop_path.unlink()
            deferred.append(hop_path)

    detail = (
        f"重建 {len(valid)} 个主题 / {len(all_reachable)} 个跳；"
        f"清理损坏主题 {len({t for t in skipped})} 个"
    )
    reports.append({"name": "rebuild_db", "status": "repaired", "detail": detail})
    return reports


# ---------- ⑥ meta.json 水位线修复 ----------

def _repair_meta_json(store: StoreContext, conn: sqlite3.Connection) -> dict:
    """meta.json 的 max_hop_seq 三源取 max（meta 自身 / hop 文件 / topics.head）。"""
    meta = store.meta_json()
    try:
        current = int(meta.get("max_hop_seq", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    max_file = 0
    for path in store.hops_dir.glob("hop-*.md"):
        match = _HOP_FILE_RE.match(path.name)
        if match:
            max_file = max(max_file, int(match.group(1)))
    max_head = 0
    for row in conn.execute("SELECT head_hop FROM topics"):
        number = _hop_number(row["head_hop"])
        if number > 0:
            max_head = max(max_head, number)
    correct = max(current, max_file, max_head)
    if meta.get("schema_version") is None or meta.get("max_hop_seq") != correct:
        meta.setdefault("schema_version", 1)
        meta["max_hop_seq"] = correct
        store.write_meta(meta)
        return {"name": "meta_json", "status": "repaired", "detail": f"max_hop_seq={correct}"}
    return {"name": "meta_json", "status": "ok"}


# ---------- ③ 崩溃追平/纳管 ----------

def _adopt_crashed_topics(store: StoreContext, conn: sqlite3.Connection) -> tuple[list[dict], list[Path]]:
    """磁盘 md 合法、DB 无主题或 head 落后、live_refs(head)==0 的崩溃窗口 → 重放纳管。

    合流提交序 ②③ 窗口（dst md 已写、DB 未提交）：纳管 dst 时发现合流 parents 含
    存活 src 的 head → 补删 src 行 + src.md（undo 无条目为已接受损失，复审 #6）。
    """
    reports: list[dict] = []
    deferred: list[Path] = []
    db_heads = {
        row["topic_id"]: row["head_hop"]
        for row in conn.execute("SELECT topic_id, head_hop FROM topics")
    }
    for md_path in sorted(store.topics_dir.glob("*.md")):
        match = _TOPIC_FILE_RE.match(md_path.name)
        if match is None:
            continue
        topic_id = match.group(1)
        try:
            doc = parse_topic_md(md_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.topic_id != topic_id or not doc.head_hop:
            continue
        head = str(doc.head_hop)
        if topic_id in db_heads and _hop_number(db_heads[topic_id]) >= _hop_number(head):
            continue
        loaded_head = _load_hop(store, head)
        if loaded_head is None or loaded_head.hop.header.get("topic_origin") != topic_id:
            continue
        if live_refs(conn, head) != 0:
            continue  # src after being merged by confluence: goes to ① wild cleanup, never mistakenly adopted (D-P21)
        reachable = _reachable_hops(store, head)
        chain = _origin_chain_from_reachable(reachable, head, topic_id) if reachable else None
        if reachable is None or chain is None:
            continue
        try:
            rep = _replay_chain(topic_id, chain)
        except ValueError:
            continue
        if (
            md_path.read_text(encoding="utf-8")
            != _render_replayed_text(rep, chain[-1].sha, doc.next_seq)
        ):
            continue  # md inconsistent with replay = tampering rather than crash window → ① cleanup
        _upsert_hops_rows(conn, reachable)
        _insert_replayed_rows(conn, rep, replace=True)
        _upsert_topic_row(
            conn, store, rep, sha256_text(md_path.read_text(encoding="utf-8"))
        )
        refresh_ref_counts(conn, reachable.keys())
        for src_head in _merged_src_heads(reachable, head):
            src_loaded = reachable.get(src_head)
            if src_loaded is None:
                continue
            src_origin = str(src_loaded.hop.header.get("topic_origin", ""))
            src_row = conn.execute(
                "SELECT head_hop FROM topics WHERE topic_id=?", (src_origin,)
            ).fetchone()
            if src_row is None or src_row["head_hop"] != src_head:
                continue  # src has advanced or this is not its current head: not a ②③ confluence window, never mistakenly deleted
            conn.execute("DELETE FROM topics WHERE topic_id=?", (src_origin,))
            conn.execute("DELETE FROM inverted_index WHERE topic_id=?", (src_origin,))
            src_md = store.topics_dir / f"{src_origin}.md"
            if src_md.is_file():
                deferred.append(src_md)
            refresh_ref_counts(conn, [src_head])
        db_heads[topic_id] = head
        reports.append(
            {"name": "adopt_crashed", "status": "repaired", "detail": f"{topic_id}@{head}"}
        )
    return reports, deferred


# ---------- ① 野生文件清理 ----------

def _cleanup_wild_files(store: StoreContext, conn: sqlite3.Connection) -> list[dict]:
    """非跳号 `*.tmp` 清扫 + 篡改 ID / 无 DB 归属的 md 清理（③ 之后执行）。"""
    reports: list[dict] = []
    for directory in (store.topics_dir, store.hops_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.tmp")):
            path.unlink()
            reports.append({"name": "tmp_sweep", "status": "deleted", "detail": str(path.name)})
    db_ids = {row["topic_id"] for row in conn.execute("SELECT topic_id FROM topics")}
    for md_path in sorted(store.topics_dir.glob("*.md")):
        match = _TOPIC_FILE_RE.match(md_path.name)
        if match is None:
            continue  # 非 topic 命名（编辑器临时文件等）忽略
        topic_id = match.group(1)
        try:
            doc = parse_topic_md(md_path.read_text(encoding="utf-8"))
            tampered = doc.topic_id != topic_id
        except (OSError, ValueError, UnsupportedSchemaVersion):
            tampered = True  # frontmatter 不可读 ⇒ 无法验证 ID 一致性
        if tampered or topic_id not in db_ids:
            md_path.unlink()
            reports.append(
                {"name": "wild_cleanup", "status": "deleted", "detail": md_path.name}
            )
    return reports


# ---------- ② 顶层 md 修复 ----------

def _repair_topic_mds(
    store: StoreContext, conn: sqlite3.Connection
) -> tuple[list[dict], list[str]]:
    """DB 主题的 md 缺失/sha 不匹配 → 链完整则重放覆写修复（含 title/artifacts 快照还原，
    D-P33）；链不完整（含缺失 own 跳）则级联删除（D-P20：仅 md 亦坏时才级联）。"""
    reports: list[dict] = []
    cascade_ids: list[str] = []
    for row in conn.execute(
        "SELECT topic_id, head_hop, md_sha256, next_seq_json FROM topics"
    ).fetchall():
        topic_id, head = row["topic_id"], row["head_hop"]
        md_path = store.topics_dir / f"{topic_id}.md"
        if file_sha256(md_path) == row["md_sha256"]:
            continue
        chain, missing, invalid = _walk_origin_chain(store, head, topic_id)
        if chain is None:
            cascade_ids.append(topic_id)
            continue
        try:
            floor = json.loads(row["next_seq_json"])
            if not isinstance(floor, dict):
                floor = {}
        except ValueError:
            floor = {}
        try:
            rep = _replay_chain(topic_id, chain)
            text = _render_replayed_text(rep, chain[-1].sha, floor)
        except ValueError:
            cascade_ids.append(topic_id)
            continue
        atomic_write_text(md_path, text)
        conn.execute(
            "UPDATE topics SET md_sha256=?, next_seq_json=? WHERE topic_id=?",
            (
                sha256_text(text),
                json.dumps(rep.doc.next_seq, ensure_ascii=False, sort_keys=True),
                topic_id,
            ),
        )
        reports.append(
            {"name": "md_repair", "status": "repaired", "detail": f"{topic_id}（重放还原）"}
        )
    return reports, cascade_ids


# ---------- ②b 存量过转义实体收敛（doctor 专属，不进入七步自愈序） ----------

def _item_lines_contain_entity(text: str) -> bool:
    """md 条目行是否含 `&#8212;` 字节（`&#40;` 不在扫描范围）。"""
    for line in text.split("\n"):
        if "&#8212;" not in line:
            continue
        if _A_LINE_RE.match(line) or _PLAIN_LINE_RE.match(line):
            return True
    return False


def _next_seq_floor(*floors) -> dict[str, int]:
    """逐轨取 max（md frontmatter 与 DB `next_seq_json` 的水位不变量）。"""
    merged: dict[str, int] = {}
    for floor in floors:
        if not isinstance(floor, dict):
            continue
        for track, value in floor.items():
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            merged[track] = max(merged.get(track, 0), value)
    return merged


def _md_frontmatter_next_seq(text: str) -> dict:
    try:
        return dict(parse_topic_md(text).next_seq)
    except (ValueError, UnsupportedSchemaVersion):
        return {}


def legacy_entity_cleanup(store: StoreContext, conn: sqlite3.Connection) -> list[dict]:
    """doctor 专属：收敛旧引擎逐字段过转义遗留的条目行（读命令路径零写）。

    命中判据：条目行含 `&#8212;` 字节且当前 md 字节 ≠ 新语义渲染的链重放文本字节。
    字节已相等（语义必需实体行或字面量实体内容）→ 零命中零报告，天然幂等；
    链不可重放的主题静默跳过零报告（其 md 修复归 `_repair_topic_mds` 级联语义管辖）。
    `next_seq` floor 取 md frontmatter 与 DB 的逐轨 max，且 DB `next_seq_json` 先行
    提交后再覆写 md——避免 md 写完即崩溃时 DB 回滚致下次以低水位重放而编号回退。
    """
    reports: list[dict] = []
    rows = conn.execute(
        "SELECT topic_id, head_hop, next_seq_json FROM topics"
    ).fetchall()
    for row in rows:
        topic_id, head = row["topic_id"], row["head_hop"]
        md_path = store.topics_dir / f"{topic_id}.md"
        if not md_path.is_file():
            continue
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _item_lines_contain_entity(md_text):
            continue
        chain = _walk_origin_chain(store, head, topic_id)[0]
        if chain is None:
            continue  # 链不可重放：静默跳过零报告
        try:
            rep = _replay_chain(topic_id, chain)
        except ValueError:
            continue
        try:
            db_floor = json.loads(row["next_seq_json"])
        except ValueError:
            db_floor = {}
        floor = _next_seq_floor(_md_frontmatter_next_seq(md_text), db_floor)
        text = _render_replayed_text(rep, chain[-1].sha, floor)
        if text == md_text:
            continue  # 语义必需实体行 / 字面量实体内容：字节已等于语义渲染
        conn.execute(
            "UPDATE topics SET next_seq_json=? WHERE topic_id=?",
            (json.dumps(rep.doc.next_seq, ensure_ascii=False, sort_keys=True), topic_id),
        )
        conn.commit()  # DB 水位先落盘（先于 md 覆写，防崩溃窗口编号回退）
        atomic_write_text(md_path, text)
        conn.execute(
            "UPDATE topics SET md_sha256=? WHERE topic_id=?",
            (sha256_text(text), topic_id),
        )
        conn.commit()
        reports.append(
            {
                "name": "legacy_entity_cleanup",
                "status": "repaired",
                "detail": f"{topic_id}（过转义实体收敛）",
            }
        )
    return reports


# ---------- ⑤ 引用中 hop 分类（D-P20 收紧 + D-P35 窗口） ----------

def _amend_pending(store: StoreContext) -> dict | None:
    path = store.root / _AMEND_PENDING_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _roll_forward_amend(
    store: StoreContext,
    conn: sqlite3.Connection,
    hop_id: str,
    topic_id: str,
    loaded: LoadedHop,
) -> Path:
    """amend 崩溃窗口收口：以文件为准重算该主题派生行与 md（D-P20/35）。

    返回待删的 pending 记录路径——延后到事务提交后删除；回滚时记录仍在，
    下一轮自愈可凭记录重新判定窗口，不会把刚改写的 head 误判损坏。
    """
    chain = _walk_origin_chain(store, hop_id, topic_id)[0]
    topic_row = conn.execute(
        "SELECT next_seq_json FROM topics WHERE topic_id=?", (topic_id,)
    ).fetchone()
    try:
        floor = json.loads(topic_row["next_seq_json"]) if topic_row is not None else {}
        if not isinstance(floor, dict):
            floor = {}
    except ValueError:
        floor = {}
    rep = _replay_chain(topic_id, chain)
    text = _render_replayed_text(rep, loaded.sha, floor)
    atomic_write_text(store.topics_dir / f"{topic_id}.md", text)
    conn.execute(
        "DELETE FROM item_hops WHERE topic_id=? AND hop_id=?", (topic_id, hop_id)
    )
    conn.execute(
        "DELETE FROM item_links WHERE topic_id=? AND hop_id=?", (topic_id, hop_id)
    )
    conn.executemany(
        "INSERT OR REPLACE INTO item_hops(topic_id, item_id, hop_id, op, seq_order)"
        " VALUES(?,?,?,?,?)",
        [row for row in rep.item_hops_rows if row[2] == hop_id],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO item_links(topic_id, item_id, hop_id, link_type,"
        " src_topic_id, src_item_id, origin_hop) VALUES(?,?,?,?,?,?,?)",
        [row for row in rep.item_links_rows if row[2] == hop_id],
    )
    conn.execute(
        "UPDATE hops SET md_sha256=?, summary=?, next_focus=?, created_at=?, actor=? WHERE hop_id=?",
        (
            loaded.sha,
            str(loaded.hop.header.get("summary", "")),
            str(loaded.hop.header.get("next_focus", "")),
            str(loaded.hop.header.get("created_at", "")),
            str(loaded.hop.header.get("actor", "")),
            hop_id,
        ),
    )
    conn.execute(
        "UPDATE topics SET title=?, head_hop=?, updated_at=?, last_actor=?, next_focus=?,"
        " next_seq_json=?, md_sha256=? WHERE topic_id=?",
        (
            rep.doc.title,
            rep.doc.head_hop,
            rep.doc.updated_at,
            rep.doc.last_actor,
            str(loaded.hop.header.get("next_focus", "")),
            json.dumps(rep.doc.next_seq, ensure_ascii=False, sort_keys=True),
            sha256_text(text),
            topic_id,
        ),
    )
    refresh_inverted_index(conn, topic_id, rep.doc)
    return store.root / _AMEND_PENDING_NAME


def _classify_referenced_hops(
    store: StoreContext, conn: sqlite3.Connection
) -> tuple[list[dict], list[str], list[Path]]:
    """缺失 → 断链跳过（sha=MISSING，不级联）；sha 失配 → 仅 amend 窄窗口 roll-forward
    （须与 .topicdock-amend-pending.json 记录匹配，D-P35）；其余一切失配/不可解析 = 损坏
    → 依赖主题按 DB 闭包级联删除 + 清理损坏文件。

    返回 (报告, 级联主题, 延后到提交后删除的文件)。
    """
    reports: list[dict] = []
    cascade_ids: list[str] = []
    deferred: list[Path] = []
    pending = _amend_pending(store)
    head_topics: dict[str, list[str]] = {}
    for row in conn.execute("SELECT topic_id, head_hop FROM topics").fetchall():
        head_topics.setdefault(row["head_hop"], []).append(row["topic_id"])
    damaged: set[str] = set()
    for row in conn.execute("SELECT hop_id, md_sha256 FROM hops").fetchall():
        hop_id = row["hop_id"]
        loaded = _load_hop(store, hop_id)
        if loaded is None:
            path = store.hops_dir / hop_file_name(hop_id)
            if not path.is_file():
                if row["md_sha256"] != "MISSING":
                    conn.execute(
                        "UPDATE hops SET md_sha256='MISSING' WHERE hop_id=?", (hop_id,)
                    )
                    reports.append(
                        {
                            "name": "hop_missing",
                            "status": "repaired",
                            "detail": f"{hop_id} 置 MISSING（断链跳过，不级联）",
                        }
                    )
                continue
            damaged.add(hop_id)  # 存在但不可解析/头失配 = 损坏
            continue
        if loaded.sha == row["md_sha256"]:
            continue
        # sha 失配 → roll-forward 窄窗口（D-P20/35）
        rollable = (
            isinstance(pending, dict)
            and pending.get("hop_id") == hop_id
            and pending.get("expected_sha") == loaded.sha
            and hop_id in head_topics
            and live_refs(conn, hop_id) == 1
        )
        if rollable:
            topic_id = head_topics[hop_id][0]
            md_path = store.topics_dir / f"{topic_id}.md"
            md_text = (
                md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
            )
            topic_row = conn.execute(
                "SELECT md_sha256 FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            md_intact = bool(topic_row) and sha256_text(md_text) == topic_row["md_sha256"]
            md_sha12 = ""
            if md_intact:
                try:
                    md_doc = parse_topic_md(md_text)
                    md_sha12 = md_doc.latest.sha12 if md_doc.latest is not None else ""
                except ValueError:
                    md_sha12 = ""
            window_ok = md_intact and (
                md_sha12 == loaded.sha[:12] or md_sha12 == row["md_sha256"][:12]
            )
            chain_ok = _walk_origin_chain(store, hop_id, topic_id)[0] is not None
            if window_ok and chain_ok:
                try:
                    deferred.append(_roll_forward_amend(store, conn, hop_id, topic_id, loaded))
                except ValueError:
                    damaged.add(hop_id)  # 重放失败 = 文件不可信，按损坏处理
                    continue
                reports.append(
                    {"name": "amend_rollforward", "status": "repaired", "detail": f"{hop_id}"}
                )
                pending = None
                continue
        damaged.add(hop_id)
    if damaged:
        # 按 DB hop_edges 闭包判定全部依赖主题：fork 后代对共享祖先的依赖同样覆盖，
        # 且不读文件——共享祖先「存在但不可解析」时 own 链视角会漏判。
        for row in conn.execute("SELECT topic_id, head_hop FROM topics").fetchall():
            if _db_reachable(conn, row["head_hop"]) & damaged:
                cascade_ids.append(row["topic_id"])
    return reports, cascade_ids, deferred


def _db_reachable(conn: sqlite3.Connection, head_hop: str) -> set[str]:
    """head 沿 hop_edges 父方向的 DB 闭包（纯表查询，不读文件）。"""
    closure: set[str] = set()
    stack = [head_hop]
    while stack:
        hop_id = stack.pop()
        if hop_id in closure:
            continue
        closure.add(hop_id)
        for row in conn.execute(
            "SELECT parent_hop FROM hop_edges WHERE child_hop=?", (hop_id,)
        ).fetchall():
            stack.append(row["parent_hop"])
    return closure


def _sweep_stale_pending(
    store: StoreContext, conn: sqlite3.Connection
) -> tuple[list[dict], list[Path]]:
    """残留 pending 清扫：与当前态一致的记录（amend 已完全持久化、仅记录删除崩溃）
    或引用跳已无 DB 行的记录，判定为残留；返回 (报告, 提交后删除路径)——
    删除一律延后到事务提交之后，回滚时记录仍在，供下一轮窗口判定。

    仍有 sha 失配的记录留给 ⑤ 窗口判定，不在本步处置。
    """
    reports: list[dict] = []
    files: list[Path] = []
    pending = _amend_pending(store)
    if pending is None:
        return reports, files
    pending_path = store.root / _AMEND_PENDING_NAME
    hop_id = str(pending.get("hop_id", ""))
    row = conn.execute("SELECT md_sha256 FROM hops WHERE hop_id=?", (hop_id,)).fetchone()
    if row is None:
        files.append(pending_path)
        reports.append(
            {"name": "pending_sweep", "status": "deleted", "detail": f"{hop_id} 已无归属"}
        )
        return reports, files
    loaded = _load_hop(store, hop_id)
    if loaded is not None and loaded.sha == row["md_sha256"]:
        files.append(pending_path)
        reports.append(
            {"name": "pending_sweep", "status": "deleted", "detail": "amend 已持久化"}
        )
    return reports, files


# ---------- ④ 孤儿 hop 清理 ----------

def _cleanup_orphan_hops(store: StoreContext, conn: sqlite3.Connection) -> list[dict]:
    """不在任何存活 topics.head 可达集内的分片文件 → 删除。"""
    reports: list[dict] = []
    live: set[str] = set()
    stack = [
        row["head_hop"] for row in conn.execute("SELECT head_hop FROM topics")
    ]
    while stack:
        hop_id = stack.pop()
        if hop_id in live:
            continue
        live.add(hop_id)
        for row in conn.execute(
            "SELECT parent_hop FROM hop_edges WHERE child_hop=?", (hop_id,)
        ):
            stack.append(row["parent_hop"])
    for hop_path in sorted(store.hops_dir.glob("hop-*.md")):
        match = _HOP_FILE_RE.match(hop_path.name)
        if match is None:
            continue
        if f"H{int(match.group(1)):03d}" not in live:
            hop_path.unlink()
            reports.append(
                {"name": "orphan_cleanup", "status": "deleted", "detail": hop_path.name}
            )
    return reports


# ---------- 级联执行（heal 路径，不压 undo） ----------

def _execute_heal_cascade(
    conn: sqlite3.Connection, store: StoreContext, topic_ids: list[str]
) -> tuple[list[dict], list[Path]]:
    """级联删除损坏主题（调用方事务内执行 DB 部分，文件删除延后到提交后）。"""
    reports: list[dict] = []
    files: list[Path] = []
    topic_ids = list(dict.fromkeys(topic_ids))
    if not topic_ids:
        return reports, files
    plan = plan_cascade(conn, store, topic_ids)
    apply_cascade(conn, plan)
    for topic_id in topic_ids:
        md_path = store.topics_dir / f"{topic_id}.md"
        if md_path.is_file():
            files.append(md_path)
        reports.append(
            {"name": "cascade_delete", "status": "deleted", "detail": f"损坏主题 {topic_id}"}
        )
    for hop_id in plan.dead_hops:
        hop_path = store.hops_dir / hop_file_name(hop_id)
        if hop_path.is_file():
            files.append(hop_path)
    return reports, files


# ---------- 自愈总入口 ----------

def ensure_store_healthy(store: StoreContext) -> tuple[sqlite3.Connection, list[dict]]:
    """七步自愈序 ⑦→⑥→③→①→②→⑤→④（D-P21），返回 (conn, 巡检报告)。

    版本守卫先于一切写盘/删盘（高版本 store 抛 UnsupportedSchemaVersion）；
    ⑦ DB 缺失/损坏 → 冷重建（quick_check 探测）；健康库探测连接直接升格为工作
    连接（免二次 connect），升格前补齐与 `get_or_create_db` 同款幂等初始化；
    级联文件删除在事务提交后执行，回滚路径关闭连接。③→⑤ 的「重采纳后又判定
    损坏」在同一轮内闭环：重采纳的主题会立刻被 ⑤ 的 DB 闭包判定再次级联，
    不会复活损坏数据。
    """
    reports: list[dict] = []
    need_rebuild = not store.db_path.is_file()
    probe: sqlite3.Connection | None = None
    if not need_rebuild:
        probe = sqlite3.connect(str(store.db_path))
        probe.row_factory = sqlite3.Row
        try:
            row = probe.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None:
                try:
                    db_version = int(row["value"])
                except ValueError:
                    db_version = 1
                if db_version > SUPPORTED_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(f"db schema_version={db_version}")
            probe.execute("SELECT COUNT(*) FROM meta")
            check = probe.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]).lower() != "ok":
                need_rebuild = True
        except sqlite3.DatabaseError:
            need_rebuild = True
        except BaseException:
            probe.close()
            raise
    if need_rebuild:
        if probe is not None:
            # rebuild 的 os.replace 前先关旧探测连接：防脱链 inode 的 WAL 清理竞态。
            probe.close()
        _raise_on_newer_schema(store)
        reports.extend(rebuild_db_from_markdown(store))
        conn = get_or_create_db(store)
    else:
        assert probe is not None
        try:
            _raise_on_newer_schema(store)
            # 复用前补齐 get_or_create_db 同款幂等初始化。
            probe.executescript(_SCHEMA_SQL)
            probe.execute("PRAGMA foreign_keys = ON")
            probe.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
            )
            probe.commit()
        except BaseException:
            probe.close()
            raise
        conn = probe
    deferred_files: list[Path] = []
    try:
        reports.append(_repair_meta_json(store, conn))
        adopt_reports, adopt_files = _adopt_crashed_topics(store, conn)
        reports.extend(adopt_reports)
        deferred_files.extend(adopt_files)
        reports.extend(_cleanup_wild_files(store, conn))
        md_reports, md_cascades = _repair_topic_mds(store, conn)
        reports.extend(md_reports)
        hop_reports, hop_cascades, hop_deferred = _classify_referenced_hops(store, conn)
        reports.extend(hop_reports)
        deferred_files.extend(hop_deferred)
        sweep_reports, sweep_files = _sweep_stale_pending(store, conn)
        reports.extend(sweep_reports)
        deferred_files.extend(sweep_files)
        cascades = list(dict.fromkeys(md_cascades + hop_cascades))
        if cascades:
            cascade_reports, cascade_files = _execute_heal_cascade(conn, store, cascades)
            reports.extend(cascade_reports)
            deferred_files.extend(cascade_files)
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    for path in deferred_files:
        path.unlink(missing_ok=True)
    reports.extend(_cleanup_orphan_hops(store, conn))
    return conn, reports


# ---------- 外部关联文件探测 ----------

def probe_artifact_links(doc: TopicDoc, workspace: Path | None) -> list[dict]:
    """关联文件存活探测：存在 → ok；缺失 → 工作区同名探测（深度 ≤3，跳 node_modules/.git
    等隐藏目录）→ 命中附 new_path（moved）；未命中 → missing（渲染层输出 ⚠️ 跳过读取）。"""
    out: list[dict] = []
    for name, path_text, note in doc.artifacts:
        entry = {"name": name, "path": path_text, "note": note or "", "status": "ok"}
        target = Path(path_text).expanduser()
        if target.is_file():
            out.append(entry)
            continue
        entry["status"] = "missing"
        if workspace is not None and Path(workspace).is_dir():
            hit = _probe_same_name(Path(workspace), target.name)
            if hit is not None:
                entry["status"] = "moved"
                entry["new_path"] = str(hit)
        out.append(entry)
    return out


def _probe_same_name(workspace: Path, basename: str, max_depth: int = 3) -> Path | None:
    skip = {"node_modules", ".git", "__pycache__", ".topicdock"}
    stack = [(workspace, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and entry.name == basename:
                return Path(entry.path)
            if (
                entry.is_dir(follow_symlinks=False)
                and not entry.name.startswith(".")
                and entry.name not in skip
            ):
                stack.append((Path(entry.path), depth + 1))
    return None
