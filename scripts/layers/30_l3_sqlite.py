# ===================== L3: SQLiteEngine =====================

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    head_hop      TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_actor    TEXT NOT NULL,
    next_focus    TEXT NOT NULL DEFAULT '',
    next_seq_json TEXT NOT NULL,
    md_sha256     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
    hop_id       TEXT PRIMARY KEY,
    topic_origin TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    next_focus   TEXT NOT NULL,
    ref_count    INTEGER NOT NULL DEFAULT 1,
    md_sha256    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hop_edges (
    parent_hop TEXT NOT NULL,
    child_hop  TEXT NOT NULL,
    PRIMARY KEY (parent_hop, child_hop)
);

CREATE TABLE IF NOT EXISTS item_hops (
    topic_id  TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    hop_id    TEXT NOT NULL,
    op        TEXT NOT NULL,
    seq_order INTEGER NOT NULL,
    PRIMARY KEY (topic_id, item_id, hop_id)
);

CREATE TABLE IF NOT EXISTS item_links (
    topic_id     TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    hop_id       TEXT NOT NULL,
    link_type    TEXT NOT NULL,
    src_topic_id TEXT NOT NULL,
    src_item_id  TEXT NOT NULL,
    origin_hop   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (topic_id, item_id, hop_id, src_topic_id, src_item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_links_src ON item_links(src_topic_id, src_item_id);

CREATE TABLE IF NOT EXISTS inverted_index (
    token    TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    field    TEXT NOT NULL,
    weight   REAL NOT NULL,
    PRIMARY KEY (token, topic_id, field)
);
CREATE INDEX IF NOT EXISTS idx_inverted_token ON inverted_index(token);

CREATE TABLE IF NOT EXISTS undo_stack (
    undo_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    op_type      TEXT NOT NULL,
    summary      TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hop_edges_child ON hop_edges(child_hop);
"""


@dataclass
class CascadePlan:
    """plan_cascade 的纯计算结果（不写任何东西）。"""

    topic_ids: list[str] = field(default_factory=list)
    topic_heads: dict[str, str] = field(default_factory=dict)
    dead_hops: list[str] = field(default_factory=list)
    removed_edges: list[tuple[str, str]] = field(default_factory=list)
    pre_files: dict[str, str] = field(default_factory=dict)


def get_or_create_db(store: StoreContext) -> sqlite3.Connection:
    """惰性建库（D-P11）：首个写命令或 doctor 时创建 topicdock.db。

    库损坏时 sqlite3 抛 DatabaseError，由 L4 ⑦ 步捕获转入冷重建。
    """
    store.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(store.db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
    conn.commit()  # 建库初始化独立提交，不进入命令层事务
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hop_number(hop_id: str) -> int:
    match = re.fullmatch(r"H(\d+)", hop_id)
    return int(match.group(1)) if match else -1


def format_hop_id(number: int) -> str:
    """hop id ≥3 位补零、自然增长（H999→H1000），数值比较（D-P30）。"""
    return f"H{number:03d}"


def hop_file_name(hop_id: str) -> str:
    return f"hop-{_hop_number(hop_id):03d}.md"


def next_hop_seq_value(store: StoreContext, conn: sqlite3.Connection) -> int:
    """全局跳号水位线：next = max(db.next_hop_seq, meta.max_hop_seq+1, max文件+1, max头+1)（D-P30）。"""
    candidates = [1]
    row = conn.execute("SELECT value FROM meta WHERE key='next_hop_seq'").fetchone()
    if row is not None:
        try:
            candidates.append(int(row["value"]))
        except ValueError:
            pass
    meta = store.meta_json()
    try:
        candidates.append(int(meta.get("max_hop_seq", 0)) + 1)
    except (TypeError, ValueError):
        pass
    if store.hops_dir.is_dir():
        for path in store.hops_dir.glob("hop-*.md"):
            match = re.fullmatch(r"hop-(\d+)\.md", path.name)
            if match:
                candidates.append(int(match.group(1)) + 1)
    for topic_row in conn.execute("SELECT head_hop FROM topics"):
        number = _hop_number(topic_row["head_hop"])
        if number >= 0:
            candidates.append(number + 1)
    return max(candidates)


def allocate_hop_id(store: StoreContext, conn: sqlite3.Connection) -> str:
    """分配新跳号并同步写 db meta 与 .topicdock-meta.json（D-P30）。"""
    number = next_hop_seq_value(store, conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('next_hop_seq', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(number + 1),),
    )
    meta = store.meta_json()
    meta.setdefault("schema_version", 1)
    meta["max_hop_seq"] = max(int(meta.get("max_hop_seq", 0) or 0), number)
    store.write_meta(meta)
    return format_hop_id(number)


def next_seq_for_topic(
    conn: sqlite3.Connection, topic_id: str, doc: TopicDoc
) -> dict[str, int]:
    """主题内各轨道编号水位线：三源取 max（frontmatter / 顶层 md / item_hops）。"""
    result: dict[str, int] = {}
    for track in _TRACKS:
        candidates = [int(doc.next_seq.get(track, 0) or 0)]
        for item in doc.items.get(track, []):
            digits = item.item_id[1:]
            if digits.isdigit():
                candidates.append(int(digits) + 1)
        for row in conn.execute(
            "SELECT item_id FROM item_hops WHERE topic_id=? AND item_id LIKE ?",
            (topic_id, f"{track}%"),
        ):
            digits = row["item_id"][1:]
            if digits.isdigit():
                candidates.append(int(digits) + 1)
        result[track] = max(candidates)
    return result


def live_refs(conn: sqlite3.Connection, hop_id: str) -> int:
    """live_refs(H) ≡ heads 引用数 + parent 边引用数（D-P23 唯一共享函数）。"""
    heads = conn.execute(
        "SELECT COUNT(*) AS n FROM topics WHERE head_hop=?", (hop_id,)
    ).fetchone()["n"]
    edges = conn.execute(
        "SELECT COUNT(*) AS n FROM hop_edges WHERE parent_hop=?", (hop_id,)
    ).fetchone()["n"]
    return heads + edges


def refresh_ref_counts(conn: sqlite3.Connection, hop_ids) -> None:
    """ref_count ≡ live_refs 的唯一重算入口（D-P23）；调用方持有事务。"""
    for hop_id in dict.fromkeys(hop_ids):
        if hop_id:
            conn.execute(
                "UPDATE hops SET ref_count=? WHERE hop_id=?",
                (live_refs(conn, hop_id), hop_id),
            )


def refresh_inverted_index(conn: sqlite3.Connection, topic_id: str, doc: TopicDoc) -> None:
    """全量重刷倒排索引；field 权重按 design §4.1（D-P5 同一分词器）。"""
    conn.execute("DELETE FROM inverted_index WHERE topic_id=?", (topic_id,))
    rows: dict[tuple[str, str], float] = {}

    def _add(text: object, index_field: str, weight: float) -> None:
        for token in tokenize(text):
            key = (token, index_field)
            if key not in rows or rows[key] < weight:
                rows[key] = weight

    _add(topic_id, "topic_id", 3.0)
    _add(doc.title, "title", 3.0)
    if doc.latest is not None:
        _add(doc.latest.next_focus, "focus", 1.5)
    for item in doc.items.get("C", []):
        _add(item.statement, "C", 2.0)
    for item in doc.items.get("Q", []):
        _add(item.statement, "Q", 2.0)
    for item in doc.items.get("A", []):
        _add(item.statement, "A", 1.8)
    for item in doc.items.get("R", []):
        _add(item.statement, "R", 1.2)
    conn.executemany(
        "INSERT OR REPLACE INTO inverted_index(token, topic_id, field, weight) "
        "VALUES(?,?,?,?)",
        [(token, topic_id, index_field, weight) for (token, index_field), weight in rows.items()],
    )


def tokenize(text: object) -> set[str]:
    """混合分词（D-P5）：[a-z0-9]+ 小写词 + 汉字串相邻二元组（串长 ≤2 另加全词）。"""
    raw = str(text)
    tokens = set(re.findall(r"[a-z0-9]+", raw.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(run) <= 2:
            tokens.add(run)
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    return tokens


_MATCH_TOKEN_CHUNK = 500


def match_topics(
    conn: sqlite3.Connection,
    queries: list[str],
    threshold: float = 35.0,
    recent: bool = False,
) -> list[dict]:
    """td match 0~100 归一化打分（design §4.1）；--recent 直取最近 ≤5（D-P16）。

    倒排命中按 ≤500 token/块批量聚合（design §3 L3 行），一次建
    `{(token, topic_id): max_weight}` 映射后再纯 Python 计分。
    """
    topic_rows = conn.execute(
        "SELECT topic_id, title, updated_at FROM topics"
        " ORDER BY updated_at DESC, topic_id ASC"
    ).fetchall()
    if recent:
        return [
            {
                "topic_id": row["topic_id"],
                "score": None,
                "title": row["title"],
                "updated_at": row["updated_at"],
            }
            for row in topic_rows[:5]
        ]
    phrase_specs = [(str(phrase).lower(), tokenize(phrase)) for phrase in queries]
    all_tokens = sorted({token for _text, tokens in phrase_specs for token in tokens})
    weight_map: dict[tuple[str, str], float] = {}
    for start in range(0, len(all_tokens), _MATCH_TOKEN_CHUNK):
        chunk = all_tokens[start : start + _MATCH_TOKEN_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        for weight_row in conn.execute(
            "SELECT token, topic_id, MAX(weight) AS max_weight FROM inverted_index "
            f"WHERE token IN ({placeholders}) GROUP BY token, topic_id",
            chunk,
        ):
            weight_map[(weight_row["token"], weight_row["topic_id"])] = weight_row["max_weight"]
    results: list[dict] = []
    for row in topic_rows:
        topic_id, title = row["topic_id"], row["title"]
        phrase_scores: list[float] = []
        for phrase_text, tokens in phrase_specs:
            if not tokens:
                continue
            if phrase_text in title.lower() or phrase_text in topic_id.lower():
                phrase_scores.append(3.0)
                continue
            matched_weights: dict[str, float] = {}
            for token in tokens:
                weight = weight_map.get((token, topic_id))
                if weight is not None:
                    matched_weights[token] = weight
            if not matched_weights:
                phrase_scores.append(0.0)
                continue
            coverage = len(matched_weights) / len(tokens)
            phrase_scores.append(coverage * max(matched_weights.values()))
        if not phrase_scores:
            continue
        phrase_scores.sort(reverse=True)
        top = phrase_scores[:3]
        total = sum(weight * score for weight, score in zip((0.6, 0.25, 0.15), top))
        score = min(100, round(100 * total / 2.4))
        if score >= threshold:
            results.append(
                {
                    "topic_id": topic_id,
                    "score": score,
                    "title": title,
                    "updated_at": row["updated_at"],
                }
            )
    results.sort(key=lambda item: item["updated_at"], reverse=True)
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def get_item_trace(conn: sqlite3.Connection, topic_id: str, item_id: str) -> dict:
    """item_hops 正序全量 + item_links 去向/上溯（递归带环保护，复审 #10）。"""
    blocks = [
        {
            "hop_id": row["hop_id"],
            "op": row["op"],
            "seq_order": row["seq_order"],
            "topic_id": topic_id,
            "item_id": item_id,
        }
        for row in conn.execute(
            "SELECT hop_id, op, seq_order FROM item_hops "
            "WHERE topic_id=? AND item_id=? ORDER BY seq_order",
            (topic_id, item_id),
        )
    ]
    links = [dict(row) for row in conn.execute(
        "SELECT * FROM item_links WHERE topic_id=? AND item_id=?",
        (topic_id, item_id),
    )]
    ancestry: list[dict] = []
    visited = {(topic_id, item_id)}
    stack = [
        (link["src_topic_id"], link["src_item_id"])
        for link in links
        if link["link_type"] == "inherit"
    ]
    while stack:
        src_topic, src_item = stack.pop()
        if (src_topic, src_item) in visited:
            continue
        visited.add((src_topic, src_item))
        ancestry.extend(
            {
                "hop_id": row["hop_id"],
                "op": row["op"],
                "seq_order": row["seq_order"],
                "topic_id": src_topic,
                "item_id": src_item,
            }
            for row in conn.execute(
                "SELECT hop_id, op, seq_order FROM item_hops "
                "WHERE topic_id=? AND item_id=? ORDER BY seq_order",
                (src_topic, src_item),
            )
        )
        stack.extend(
            (row["src_topic_id"], row["src_item_id"])
            for row in conn.execute(
                "SELECT src_topic_id, src_item_id FROM item_links "
                "WHERE topic_id=? AND item_id=? AND link_type='inherit'",
                (src_topic, src_item),
            )
        )
    successors = [dict(row) for row in conn.execute(
        "SELECT topic_id, item_id, hop_id, link_type FROM item_links "
        "WHERE src_topic_id=? AND src_item_id=?",
        (topic_id, item_id),
    )]
    return {
        "topic_id": topic_id,
        "item_id": item_id,
        "blocks": blocks,
        "links": links,
        "ancestry": ancestry,
        "successors": successors,
    }


def plan_cascade(
    conn: sqlite3.Connection, store: StoreContext, topic_ids: list[str]
) -> CascadePlan:
    """级联删除两阶段之纯计算（D-P32 前置）：worklist 不动点模拟，O(k)，不写。"""
    topic_ids = list(dict.fromkeys(topic_ids))
    plan = CascadePlan(topic_ids=topic_ids)

    head_refs: dict[str, int] = {}
    for row in conn.execute(
        "SELECT head_hop, COUNT(*) AS n FROM topics GROUP BY head_hop"
    ):
        head_refs[row["head_hop"]] = row["n"]
    parent_count: dict[str, int] = {}
    children_of: dict[str, list[str]] = {}
    parents_of: dict[str, list[str]] = {}
    all_edges: list[tuple[str, str]] = [
        (row["parent_hop"], row["child_hop"])
        for row in conn.execute("SELECT parent_hop, child_hop FROM hop_edges")
        if row["parent_hop"] != row["child_hop"]  # 自环边（篡改痕迹）不计引用
    ]
    for parent, child in all_edges:
        parent_count[parent] = parent_count.get(parent, 0) + 1
        children_of.setdefault(parent, []).append(child)
        parents_of.setdefault(child, []).append(parent)

    def _sim_live(hop_id: str) -> int:
        return head_refs.get(hop_id, 0) + parent_count.get(hop_id, 0)

    for topic_id in topic_ids:
        row = conn.execute(
            "SELECT head_hop FROM topics WHERE topic_id=?", (topic_id,)
        ).fetchone()
        if row is not None:
            plan.topic_heads[topic_id] = row["head_hop"]
            head_refs[row["head_hop"]] = head_refs.get(row["head_hop"], 0) - 1

    worklist = [
        head_hop
        for head_hop, count in head_refs.items()
        if count <= 0 and _sim_live(head_hop) <= 0
    ]
    seen: set[str] = set()
    removed: set[tuple[str, str]] = set()
    while worklist:
        hop_id = worklist.pop()
        if hop_id in seen:
            continue
        seen.add(hop_id)
        plan.dead_hops.append(hop_id)
        for child in children_of.get(hop_id, []):
            removed.add((hop_id, child))
        for parent in parents_of.get(hop_id, []):
            removed.add((parent, hop_id))
            parent_count[parent] = parent_count.get(parent, 0) - 1
            if _sim_live(parent) <= 0 and parent not in seen:
                worklist.append(parent)
    plan.dead_hops.sort(key=_hop_number)
    plan.removed_edges = sorted(removed)

    for topic_id in topic_ids:
        md_path = store.topics_dir / f"{topic_id}.md"
        if md_path.is_file():
            plan.pre_files[str(md_path)] = md_path.read_text(
                encoding="utf-8", errors="surrogateescape"
            )
    for hop_id in plan.dead_hops:
        hop_path = store.hops_dir / hop_file_name(hop_id)
        if hop_path.is_file():
            plan.pre_files[str(hop_path)] = hop_path.read_text(
                encoding="utf-8", errors="surrogateescape"
            )
    return plan


def apply_cascade(conn: sqlite3.Connection, plan: CascadePlan) -> None:
    """级联删除两阶段之执行（调用方持有事务，由命令层统一提交/回滚）。

    文件删除由 M7 命令层在事务提交后执行；幸存触点 hop 的 ref_count 在此重算。
    """
    if not plan.topic_ids:
        return
    dead_set = set(plan.dead_hops)
    topic_marks = ",".join("?" * len(plan.topic_ids))
    conn.execute(f"DELETE FROM topics WHERE topic_id IN ({topic_marks})", plan.topic_ids)
    if plan.dead_hops:
        hop_marks = ",".join("?" * len(plan.dead_hops))
        conn.execute(f"DELETE FROM hops WHERE hop_id IN ({hop_marks})", plan.dead_hops)
        for hop_id in plan.dead_hops:
            conn.execute(
                "DELETE FROM hop_edges WHERE parent_hop=? OR child_hop=?", (hop_id, hop_id)
            )
        conn.execute(f"DELETE FROM item_hops WHERE hop_id IN ({hop_marks})", plan.dead_hops)
        conn.execute(f"DELETE FROM item_links WHERE hop_id IN ({hop_marks})", plan.dead_hops)
    conn.execute(
        f"DELETE FROM inverted_index WHERE topic_id IN ({topic_marks})", plan.topic_ids
    )
    touched_survivors = {
        parent for parent, _child in plan.removed_edges if parent not in dead_set
    }
    touched_survivors.update(
        head for head in plan.topic_heads.values() if head not in dead_set
    )
    refresh_ref_counts(conn, touched_survivors)


def push_undo(
    conn: sqlite3.Connection, op_type: str, summary: str, payload: dict
) -> None:
    """压入 undo 快照并裁剪至最近 3 条（design §4.1 undo_stack LIMIT 3）。

    调用方持有事务（与命令主体同一事务提交，防"已删除但无 undo 条目"窗口）。
    """
    conn.execute(
        "INSERT INTO undo_stack(created_at, op_type, summary, payload_json) "
        "VALUES(?,?,?,?)",
        (
            _utc_now(),
            op_type,
            summary,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.execute(
        "DELETE FROM undo_stack WHERE undo_id NOT IN "
        "(SELECT undo_id FROM undo_stack ORDER BY undo_id DESC LIMIT 3)"
    )


def pop_undo(conn: sqlite3.Connection) -> dict | None:
    """弹出栈顶 undo 快照；空栈返回 None（D-P13）。调用方持有事务。"""
    row = conn.execute(
        "SELECT * FROM undo_stack ORDER BY undo_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM undo_stack WHERE undo_id=?", (row["undo_id"],))
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


