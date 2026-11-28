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

CREATE TABLE IF NOT EXISTS undo_stack (
    undo_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    op_type      TEXT NOT NULL,
    summary      TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hop_edges_child ON hop_edges(child_hop);
"""

# ---------- FTS5 匹配索引（design §3.1/§5.2/§5.4） ----------

_MATCH_INDEX_SCHEMA = "fts5-perfield-v1"
_MATCH_INDEX_SCHEMA_KEY = "match_index_schema"
_MATCH_INDEX_SKIPPED_KEY = "match_index_skipped"
_FTS5_MISSING_MODULE_MESSAGE = "no such module: fts5"

# 进程级能力标志：None=未探测 / True=可用 / False=不可用（降级 no-op）
_FTS5_AVAILABLE: bool | None = None
# 建表健康检查发现损坏并重建空表后置位，交由 migrate_match_index 全量重索引
_FTS_NEEDS_FULL_REINDEX = False

_TOPIC_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS topic_fts USING fts5(
    topic_id UNINDEXED,
    md_sha256 UNINDEXED,
    topic_slug,
    title,
    focus,
    c,
    q,
    a,
    r,
    detail=full,
    tokenize='unicode61'
);
"""


class MatchIndexError(RuntimeError):
    """匹配索引写删失败（表缺失/损坏）；调用方回滚并提示 doctor --rebuild。"""


class MatchIndexUnavailable(MatchIndexError):
    """SQLite 未启用 FTS5，匹配索引不可用（其余命令照常工作）。"""


@dataclass
class CascadePlan:
    """plan_cascade 的纯计算结果（不写任何东西）。"""

    topic_ids: list[str] = field(default_factory=list)
    topic_heads: dict[str, str] = field(default_factory=dict)
    dead_hops: list[str] = field(default_factory=list)
    removed_edges: list[tuple[str, str]] = field(default_factory=list)
    pre_files: dict[str, str] = field(default_factory=dict)


def assert_db_schema_supported(conn: sqlite3.Connection) -> None:
    """DB schema_version 高于本引擎支持版本 → UnsupportedSchemaVersion。

    须在任何写入之前调用（含建虚表）；缺 meta 表 / 无该键 / 值不可解析 → 不判定，
    交调用方既有的损坏/初始化路径。与 L4 连接打开序列共用同一判版口径。
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None:
        return
    try:
        db_version = int(row["value"])
    except (TypeError, ValueError):
        return
    if db_version > SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(f"db schema_version={db_version}")


def get_or_create_db(store: StoreContext) -> sqlite3.Connection:
    """惰性建库（D-P11）：首个写命令或 doctor 时创建 topicdock.db。

    库损坏时 sqlite3 抛 DatabaseError，由 L4 ⑦ 步捕获转入冷重建。版本守卫先于
    一切写入：高版本库直接抛 UnsupportedSchemaVersion，不建/不修 `topic_fts`。
    """
    store.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(store.db_path))
    conn.row_factory = sqlite3.Row
    try:
        assert_db_schema_supported(conn)
    except BaseException:
        conn.close()
        raise
    conn.executescript(_SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
    ensure_match_index(conn)
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


def tokenize_terms(text: object) -> list[str]:
    """索引侧分词（design §3.2）：词表与 `tokenize()` 一致，保留重复次数（词频）。

    中文 run 长 1 → 单字；长 2 → 该二字词一次；长 ≥3 → 相邻二元组。
    """
    raw = str(text)
    terms = re.findall(r"[a-z0-9]+", raw.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(run) <= 2:
            terms.append(run)
            continue
        for index in range(len(run) - 1):
            terms.append(run[index : index + 2])
    return terms


def _indexed_field_text(text: object) -> str:
    """字段文本：先施解码正典（`unescape_item_text`，幂等），token 按字典序排序后
    空格拼接（重复保留，增量/重建逐字确定）。"""
    return " ".join(sorted(tokenize_terms(unescape_item_text(str(text)))))


def probe_fts5(conn: sqlite3.Connection) -> bool:
    """同一连接 `temp` schema 建/删探测表判定 FTS5 能力（不写主库、不新建连接）。"""
    global _FTS5_AVAILABLE
    try:  # 清理极端情况下残留的探测表，避免误判为「模块缺失」
        conn.execute("DROP TABLE IF EXISTS temp._fts5_probe")
    except sqlite3.DatabaseError:
        pass
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
    except sqlite3.DatabaseError:
        _FTS5_AVAILABLE = False
        return False
    try:
        conn.execute("DROP TABLE temp._fts5_probe")
    except sqlite3.DatabaseError:  # pragma: no cover — 建表成功时删除不应失败
        pass
    _FTS5_AVAILABLE = True
    return True


def _drop_match_index_table(conn: sqlite3.Connection) -> None:
    """清理损坏的 `topic_fts`：先 DROP；DROP 自身失败则 'rebuild' 后重试 DROP。

    兼容 macOS 系统 Python 3.9.6 的 SQLITE_DBCONFIG_DEFENSIVE（禁止直接改影子表）。
    """
    try:
        conn.execute("DROP TABLE IF EXISTS topic_fts")
        return
    except sqlite3.DatabaseError:
        pass
    conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('rebuild')")
    conn.execute("DROP TABLE IF EXISTS topic_fts")


def ensure_match_index(conn: sqlite3.Connection) -> bool:
    """连接打开时确保 `topic_fts` 就位；损坏则清理重建空表并置全量重索引标志。"""
    global _FTS5_AVAILABLE, _FTS_NEEDS_FULL_REINDEX
    if _FTS5_AVAILABLE is None:
        probe_fts5(conn)
    if _FTS5_AVAILABLE is False:
        return False
    try:
        conn.execute(_TOPIC_FTS_SQL)
        conn.execute("SELECT COUNT(*), MIN(md_sha256) FROM topic_fts").fetchone()
        return True
    except sqlite3.DatabaseError:
        pass
    try:
        _drop_match_index_table(conn)
        conn.execute(_TOPIC_FTS_SQL)
        conn.execute("SELECT COUNT(*), MIN(md_sha256) FROM topic_fts").fetchone()
    except sqlite3.DatabaseError:
        _FTS5_AVAILABLE = False
        return False
    _FTS_NEEDS_FULL_REINDEX = True
    return True


def _refresh_match_index_rows(conn: sqlite3.Connection, topic_id: str, doc: TopicDoc,
                              md_sha256: str) -> None:
    latest = doc.latest
    columns = (
        topic_id,
        str(md_sha256),
        _indexed_field_text(topic_id),
        _indexed_field_text(doc.title),
        _indexed_field_text(latest.next_focus if latest is not None else ""),
        _indexed_field_text(" ".join(item.statement for item in doc.items.get("C", []))),
        _indexed_field_text(" ".join(item.statement for item in doc.items.get("Q", []))),
        _indexed_field_text(" ".join(item.statement for item in doc.items.get("A", []))),
        _indexed_field_text(" ".join(item.statement for item in doc.items.get("R", []))),
    )
    conn.execute("DELETE FROM topic_fts WHERE topic_id=?", (topic_id,))
    conn.execute(
        "INSERT INTO topic_fts(topic_id, md_sha256, topic_slug, title, focus, c, q, a, r)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        columns,
    )


def refresh_match_index(
    conn: sqlite3.Connection, topic_id: str, doc: TopicDoc, md_sha256: str = ""
) -> None:
    """`topic_fts` 唯一写入口（design §3.1）：DELETE+INSERT 幂等单行。"""
    if _FTS5_AVAILABLE is False:
        return
    try:
        _refresh_match_index_rows(conn, str(topic_id), doc, md_sha256)
    except sqlite3.DatabaseError as exc:
        raise MatchIndexError(
            f"匹配索引写入失败（请运行 td doctor --rebuild）: {exc}"
        ) from exc


def delete_match_index(conn: sqlite3.Connection, topic_ids) -> None:
    """`topic_fts` 唯一删除入口（design §3.1）；仅能力缺失时 no-op。"""
    if _FTS5_AVAILABLE is False:
        return
    ids = [str(topic_id) for topic_id in dict.fromkeys(topic_ids)]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    try:
        conn.execute(f"DELETE FROM topic_fts WHERE topic_id IN ({marks})", ids)
    except sqlite3.DatabaseError as exc:
        raise MatchIndexError(
            f"匹配索引删除失败（请运行 td doctor --rebuild）: {exc}"
        ) from exc


def match_index_health(conn: sqlite3.Connection) -> int:
    """匹配索引健康巡检：FTS5 `integrity-check` + 内容行数（只读）。

    返回 `topic_fts` 行数；任何 `sqlite3.DatabaseError`（缺表/损坏/缺模块）原样
    向上抛出，由调用方转成 doctor 报告口径。
    """
    conn.execute("INSERT INTO topic_fts(topic_fts) VALUES('integrity-check')")
    return conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]


# bm25 列权重：topic_id/md_sha256 两个 UNINDEXED 列在前，随后 topic_slug/title/
# focus/c/q/a/r（design §3.1）。
_MATCH_BM25_RANK = "bm25(topic_fts, 0.0, 0.0, 3.0, 3.0, 1.5, 2.0, 2.0, 1.8, 1.2)"
# 层内相对排名的噪声下限（M0 实测定值，design §3.4）。
SCORE_MIN_SPREAD = 1e-5
SCORE_DEGENERATE_BEST = -1e-4
# 查询词元上限（design §3.2）；校验钉在 L5 cmd_match_locked。
MAX_QUERY_TOKENS = 500
# 仅靠短语子串归属、未在 FTS5 命中的词元折算的有效 bm25（对齐 title 权重 3.0）。
_SUBSTRING_BM25 = -3.0


def _fts_quote(token: str) -> str:
    """查询词元包双引号（tokenize 产物只含 ASCII 字母数字/中文，无注入面）。"""
    return '"' + str(token) + '"'


def _is_match_index_missing(exc: BaseException) -> bool:
    message = str(exc)
    return (
        "no such table: topic_fts" in message
        or _FTS5_MISSING_MODULE_MESSAGE in message
    )


def _raise_match_unavailable(detail: str = "") -> None:
    suffix = f"（{detail}）" if detail else ""
    raise MatchIndexUnavailable(
        "td match 需要 SQLite 启用 FTS5；当前平台 "
        f"{sys.platform} / SQLite {sqlite3.sqlite_version}{suffix}"
    )


def _query_match_index(conn: sqlite3.Connection, expression: str) -> list:
    """执行一次 FTS5 MATCH；缺表/缺模块 → MatchIndexUnavailable，其余损坏 → MatchIndexError。"""
    if _FTS5_AVAILABLE is False:
        _raise_match_unavailable("匹配索引不可用")
    try:
        return conn.execute(
            "SELECT topic_id, "
            f"{_MATCH_BM25_RANK} AS rank FROM topic_fts WHERE topic_fts MATCH ?",
            (expression,),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        if _is_match_index_missing(exc):
            _raise_match_unavailable(str(exc))
        raise MatchIndexError(
            f"匹配索引查询失败（请运行 td doctor --rebuild）: {exc}"
        ) from exc


def match_recall(
    conn: sqlite3.Connection, tokens
) -> dict[str, dict]:
    """召回原语：候选宇宙恒来自 `topics`；返回 `{topic_id: {"matched": set, "bm25": float}}`。

    每个词元单独 MATCH 求命中集，再对 `T` 并集跑一次 bm25（越负越好）求层内排序分。
    """
    token_list = sorted({str(token) for token in tokens if str(token)})
    if not token_list:
        return {}
    universe = {row["topic_id"] for row in conn.execute("SELECT topic_id FROM topics")}
    matched: dict[str, set[str]] = {}
    for token in token_list:
        for row in _query_match_index(conn, _fts_quote(token)):
            topic_id = row["topic_id"]
            if topic_id in universe:
                matched.setdefault(topic_id, set()).add(token)
    expression = " OR ".join(_fts_quote(token) for token in token_list)
    bm25: dict[str, float] = {}
    for row in _query_match_index(conn, expression):
        topic_id = row["topic_id"]
        if topic_id not in universe:
            continue
        rank = float(row["rank"])
        if topic_id not in bm25 or rank < bm25[topic_id]:  # 幽灵/重复行按最优分兜底
            bm25[topic_id] = rank
    return {
        topic_id: {"matched": matched.get(topic_id, set()), "bm25": bm25.get(topic_id, 0.0)}
        for topic_id in set(matched) | set(bm25)
    }


def _finalize_tier_bucket(bucket: list[dict]) -> None:
    """层内相对排名：`round(1 + 99*(worst-b)/(worst-best))`；退化时全 100。"""
    if not bucket:
        return
    ranks = [entry["_bm25"] for entry in bucket]
    best, worst = min(ranks), max(ranks)
    degenerate = (
        len(bucket) == 1
        or (worst - best) < SCORE_MIN_SPREAD
        or best > SCORE_DEGENERATE_BEST
    )
    if degenerate:
        for entry in bucket:
            entry["score"] = 100
        bucket.sort(key=lambda entry: entry["topic_id"])
        bucket.sort(key=lambda entry: entry["updated_at"], reverse=True)
        return
    span = worst - best
    for entry in bucket:
        entry["score"] = round(1 + 99 * (worst - entry["_bm25"]) / span)
    # 主序 bm25 升序（越负越好），同分 updated_at 倒序、topic_id 升序
    bucket.sort(key=lambda entry: entry["topic_id"])
    bucket.sort(key=lambda entry: entry["updated_at"], reverse=True)
    bucket.sort(key=lambda entry: entry["_bm25"])


_TIER_ORDER = {
    "all": ("all",),
    "partial": ("all", "partial"),
    "any": ("all", "partial", "any"),
}


def match_topics(
    conn: sqlite3.Connection,
    queries: list[str],
    tier: str = "all",
    recent: bool = False,
) -> list[dict]:
    """分层召回 + 层内 bm25 相对排名（design §3.3/§3.4）；--recent 直取最近 ≤5。

    条目 `{topic_id, title, updated_at, tier, matched_terms, score}`；`--tier` 是累计
    最低层过滤（all / all+partial / 三层），各层互斥且独立评分。
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
    all_tokens = {token for _text, tokens in phrase_specs for token in tokens}
    if not all_tokens:
        return []
    recall = match_recall(conn, all_tokens)
    rows_by_id = {row["topic_id"]: row for row in topic_rows}
    entries: dict[str, dict] = {}
    for topic_id, data in recall.items():
        row = rows_by_id.get(topic_id)
        if row is None:  # 候选宇宙恒来自 topics：FTS 幽灵行不成为结果
            continue
        entries[topic_id] = {
            "topic_id": topic_id,
            "title": row["title"],
            "updated_at": row["updated_at"],
            "matched": set(data["matched"]),
            "_bm25": float(data["bm25"]),
        }
    # 短语子串归属：仅记该短语自身词元；未在 FTS5 命中的子串词元按 -3.0 折算
    for row in topic_rows:
        topic_id, title = row["topic_id"], row["title"]
        title_lower, id_lower = title.lower(), topic_id.lower()
        for phrase_text, phrase_tokens in phrase_specs:
            if not phrase_tokens:
                continue
            if not phrase_text or (
                phrase_text not in title_lower and phrase_text not in id_lower
            ):
                continue
            entry = entries.get(topic_id)
            if entry is None:
                entry = {
                    "topic_id": topic_id,
                    "title": title,
                    "updated_at": row["updated_at"],
                    "matched": set(),
                    "_bm25": 0.0,
                }
                entries[topic_id] = entry
            attributed = phrase_tokens - entry["matched"]
            if attributed:
                entry["_bm25"] += _SUBSTRING_BM25 * len(attributed)
                entry["matched"] |= attributed
    token_count = len(all_tokens)
    half = (token_count + 1) // 2  # ceil(|T|/2)，整数式（不依赖 math）
    buckets: dict[str, list[dict]] = {"all": [], "partial": [], "any": []}
    for entry in entries.values():
        matched_count = len(entry["matched"])
        if matched_count == token_count:
            entry["tier"] = "all"
        elif half <= matched_count < token_count:
            entry["tier"] = "partial"
        elif 1 <= matched_count < half:
            entry["tier"] = "any"
        else:  # pragma: no cover — 候选宇宙保证 matched >= 1
            continue
        entry["matched_terms"] = matched_count
        buckets[entry["tier"]].append(entry)
    for bucket in buckets.values():
        _finalize_tier_bucket(bucket)
    results: list[dict] = []
    for name in _TIER_ORDER.get(tier, ("all",)):
        for entry in buckets[name]:
            results.append(
                {
                    "topic_id": entry["topic_id"],
                    "title": entry["title"],
                    "updated_at": entry["updated_at"],
                    "tier": entry["tier"],
                    "matched_terms": entry["matched_terms"],
                    "score": entry["score"],
                }
            )
    return results


def similar_topics(
    conn: sqlite3.Connection,
    title: str,
    limit: int = 3,
    min_coverage: float = 35.0,
) -> list[dict]:
    """新建主题查重（design §3.5）：只统计 `{title topic_slug}` 覆盖率，bm25 tie-break。

    `score=round(100*matched/|T_title|)`；`score >= min_coverage` 过滤；候选来自
    `topics`（FTS 幽灵行不参与）；排除新 topic_id 由 L5 完成。
    """
    tokens = sorted(tokenize(title))
    if not tokens:
        return []
    universe = {
        row["topic_id"]: row["title"]
        for row in conn.execute("SELECT topic_id, title FROM topics")
    }
    matched: dict[str, int] = {}
    column_filter = "{title topic_slug}"
    for token in tokens:
        expression = f"{column_filter}:{_fts_quote(token)}"
        # 每个 token 每主题最多计一次（幽灵/重复行按 GROUP BY 兜底口径去重）
        hit_ids = {
            row["topic_id"]
            for row in _query_match_index(conn, expression)
            if row["topic_id"] in universe
        }
        for topic_id in hit_ids:
            matched[topic_id] = matched.get(topic_id, 0) + 1
    expression = " OR ".join(
        f"{column_filter}:{_fts_quote(token)}" for token in tokens
    )
    bm25: dict[str, float] = {}
    for row in _query_match_index(conn, expression):
        topic_id = row["topic_id"]
        if topic_id not in universe:
            continue
        rank = float(row["rank"])
        if topic_id not in bm25 or rank < bm25[topic_id]:
            bm25[topic_id] = rank
    denominator = len(tokens)
    scored: list[dict] = []
    for topic_id, count in matched.items():
        score = round(100 * count / denominator)
        if score >= min_coverage:
            scored.append(
                {
                    "topic_id": topic_id,
                    "title": universe[topic_id],
                    "score": score,
                    "_bm25": bm25.get(topic_id, 0.0),
                }
            )
    scored.sort(key=lambda entry: entry["topic_id"])
    scored.sort(key=lambda entry: entry["_bm25"])
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return [
        {"topic_id": entry["topic_id"], "title": entry["title"], "score": entry["score"]}
        for entry in scored[:limit]
    ]


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
    delete_match_index(conn, plan.topic_ids)
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


