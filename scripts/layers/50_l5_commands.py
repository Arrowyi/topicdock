# ===================== L5: Commands =====================
"""L5: 命令实现层 —— 本里程碑仅 cmd_init；save/match 等由后续里程碑填充。"""


def cmd_init(args: argparse.Namespace) -> tuple[dict, int]:
    """`td init --scope global|local [--path <dir>]` —— 幂等初始化 store。

    - 落点: 有 --path 用之；否则 global → ~/.topicdock/、local → <cwd>/.topicdock/。
    - 创建 root/、topics/、hops/、.gitignore（内容一致则不重写）、meta（不存在或
      损坏时写默认值，已存在且合法则保留）；禁止创建 topicdock.db（惰性建库）。
    - 仅当给了 --path 时写指针配置（global → ~/.agents/topicdock/config.json，
      local → <cwd>/.agents/topicdock.json），内容 {"store_dir": "<abs>"}。
    """
    has_path = bool(getattr(args, "path", None))
    if has_path:
        store = Path(args.path).expanduser().resolve()
    elif args.scope == "global":
        store = Path.home() / ".topicdock"
    else:
        store = Path.cwd() / ".topicdock"
    created = not store.exists()
    store.mkdir(parents=True, exist_ok=True)
    (store / "topics").mkdir(exist_ok=True)
    (store / "hops").mkdir(exist_ok=True)

    ctx = StoreContext(store)
    gitignore_content = (
        "topicdock.db*\n.topicdock.lock\n*.tmp\n"
        ".topicdock-undo-pending.json\n.topicdock-amend-pending.json\n"
        ".topicdock-published.json\n"
    )
    if (
        not ctx.gitignore_path.exists()
        or ctx.gitignore_path.read_text(encoding="utf-8") != gitignore_content
    ):
        atomic_write_text(ctx.gitignore_path, gitignore_content)

    existing_meta: object = None
    if ctx.meta_path.exists():
        try:
            existing_meta = json.loads(ctx.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_meta = None
    if not isinstance(existing_meta, dict):
        ctx.write_meta(_default_meta())

    if has_path:
        pointer_path = (
            Path.home() / ".agents" / "topicdock" / "config.json"
            if args.scope == "global"
            else Path.cwd() / ".agents" / "topicdock.json"
        )
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            pointer_path,
            json.dumps({"store_dir": str(store)}, ensure_ascii=False, indent=2) + "\n",
        )

    result = {"status": "OK", "store_dir": str(store), "scope": args.scope, "created": created}
    return result, EXIT_OK


# ---------- cmd_save 写路径核心（design §4.3(1)，D-P24/25/26/33/35） ----------

_TOPIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ITEM_ID_RE = re.compile(r"^[CRQA]\d+$")


class InvalidInput(Exception):
    """载荷校验失败等输入类错误 → 退出码 7。"""


class TopicNotFoundError(Exception):
    """主题不存在 → 退出码 5。"""


class ItemNotFoundError(Exception):
    """条目不存在 → 退出码 5。"""


class StaleBase(Exception):
    """base 失步（D-P24）→ 退出码 4；payload 为 STALE 载荷。"""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("detail", "STALE_BASE"))
        self.payload = payload


def _save_field_map(track: str, status: str | None, mutation: dict) -> dict:
    """载荷字段 → hop 块字段名（design §4.2 块模板；仅收录显式提供的字段）。"""
    fields: dict[str, str] = {}
    if mutation.get("statement") is not None:
        fields["结论正文"] = str(mutation["statement"])
    if mutation.get("basis") is not None:
        key = {"R": "一句话原因", "A": "断点"}.get(track, "一句话依据")
        fields[key] = str(mutation["basis"])
    if mutation.get("rationale"):
        fields["推导与权衡"] = str(mutation["rationale"])
    if mutation.get("evidence_detail"):
        key = "核心证据 (verified)" if status == "verified" else "核心证据 (inferred)"
        fields[key] = str(mutation["evidence_detail"])
    if mutation.get("review_cond"):
        fields["重审条件"] = str(mutation["review_cond"])
    return fields


def _item_fields_snapshot(item) -> dict:
    """inherit 块的全量条目快照字段（D-P22：操作后完整状态）。"""
    fields: dict[str, str] = {}
    if item.statement:
        fields["结论正文"] = item.statement
    if item.basis is not None:
        key = {"R": "一句话原因", "A": "断点"}.get(item.track, "一句话依据")
        fields[key] = item.basis
    if item.rationale:
        fields["推导与权衡"] = item.rationale
    if item.evidence_detail:
        key = "核心证据 (verified)" if item.evidence_status == "verified" else "核心证据 (inferred)"
        fields[key] = item.evidence_detail
    if item.review_cond:
        fields["重审条件"] = item.review_cond
    return fields


def _require_topic_item(doc, item_id: str) -> None:
    item = _find_item(doc, item_id)
    if item is None:
        raise ItemNotFoundError(f"条目不存在: {item_id}")
    return item


def _prune_dead_artifacts(artifacts: list) -> list:
    """新列表自动剔除已销毁死链（D-P33/D-P34：仅对显式提供的新列表生效）。"""
    pruned = []
    for entry in artifacts:
        if not isinstance(entry, dict) or "path" not in entry:
            raise InvalidInput(f"artifacts 条目非法: {entry!r}")
        path = Path(str(entry["path"])).expanduser()
        if path.is_file():
            pruned.append(
                {
                    "name": str(entry.get("name", path.name)),
                    "path": str(entry["path"]),
                    "note": str(entry.get("note", "") or ""),
                }
            )
    return pruned


def _normalize_artifacts(payload_artifacts, base_snapshot: list | None) -> list:
    """D-P33 流转：省略/None → 继承 base 快照；[] → 清空；新列表 → 更新并剔除死链。"""
    if payload_artifacts is None:
        return [dict(a) for a in (base_snapshot or [])]
    if not isinstance(payload_artifacts, list):
        raise InvalidInput("artifacts 必须是列表")
    return _prune_dead_artifacts(payload_artifacts)


def _make_allocator(conn, topic_id: str, doc) -> tuple[dict, callable]:
    """条目号分配器：L3 next_seq_for_topic 起步（max(frontmatter, used+1)，F13 M5 约束）。"""
    counters = next_seq_for_topic(conn, topic_id, doc)

    def allocate(track: str) -> int:
        number = int(counters.get(track, 1) or 1)
        counters[track] = number + 1
        return number

    return counters, allocate


def _normalize_mutations(
    conn, topic_id: str, doc, rep, mutations: list, allocate
) -> list:
    """载荷 mutations → 规范化 HopBlock 序列（逐条按 design §4.3(1) 状态转移矩阵校验）。"""
    if not isinstance(mutations, list):
        raise InvalidInput("mutations 必须是数组")
    blocks: list = []
    created_here: dict = {}  # 本载荷刚建条目（amend 合并规则的容许域）
    for mutation in mutations:
        if not isinstance(mutation, dict):
            raise InvalidInput(f"mutation 必须是对象: {mutation!r}")
        op = mutation.get("op")
        if op == "create":
            track = mutation.get("track")
            if track not in _TRACKS:
                raise InvalidInput(f"create 轨道非法: {track!r}")
            status = mutation.get("evidence_status")
            if status is not None and status not in ("verified", "inferred"):
                raise InvalidInput(f"evidence_status 非法: {status!r}")
            a_state = None
            if track == "A":
                a_state = mutation.get("state", " ")
                if a_state not in ("x", "~", " "):
                    raise InvalidInput(f"A 轨状态非法: {a_state!r}")
            item_id = f"{track}{allocate(track)}"
            block = HopBlock(
                item_id=item_id, op="create", status=status, a_state=a_state,
                fields=_save_field_map(track, status, mutation),
            )
            blocks.append(block)
            created_here[item_id] = block
        elif op == "update":
            item_id = str(mutation.get("item_id", ""))
            if not _ITEM_ID_RE.fullmatch(item_id):
                raise InvalidInput(f"update item_id 非法: {item_id!r}")
            if item_id in created_here:
                track = _track_of(item_id)
            else:
                item = _require_topic_item(doc, item_id)
                track = item.track
            status = mutation.get("evidence_status")
            if status is not None and status not in ("verified", "inferred"):
                raise InvalidInput(f"evidence_status 非法: {status!r}")
            a_state = mutation.get("state") if track == "A" else None
            if a_state is not None and a_state not in ("x", "~", " "):
                raise InvalidInput(f"A 轨状态非法: {a_state!r}")
            blocks.append(
                HopBlock(
                    item_id=item_id, op="update", status=status, a_state=a_state,
                    fields=_save_field_map(track, status, mutation),
                )
            )
        elif op == "overturn":
            from_item = str(mutation.get("from_item", ""))
            if from_item not in created_here:
                source = _require_topic_item(doc, from_item)
                if source.track != "C":
                    raise InvalidInput(f"overturn 仅适用于 [C] 条目: {from_item}")
            elif _track_of(from_item) != "C":
                raise InvalidInput(f"overturn 仅适用于 [C] 条目: {from_item}")
            status = mutation.get("evidence_status")
            item_id = f"R{allocate('R')}"
            origin_hop = rep.create_hops.get(from_item, "")
            blocks.append(
                HopBlock(
                    item_id=item_id, op="overturn", from_items=[from_item],
                    origin=f"{topic_id}:{from_item}@{origin_hop}", status=status,
                    fields=_save_field_map("R", status, mutation),
                )
            )
        elif op == "resolve":
            from_item = str(mutation.get("from_item", ""))
            if from_item not in created_here:
                source = _require_topic_item(doc, from_item)
                if source.track != "Q":
                    raise InvalidInput(f"resolve 仅适用于 [Q] 条目: {from_item}")
            elif _track_of(from_item) != "Q":
                raise InvalidInput(f"resolve 仅适用于 [Q] 条目: {from_item}")
            into = mutation.get("into_track")
            if into not in (None, "C", "R"):
                raise InvalidInput(f"into_track 非法: {into!r}")
            origin_hop = rep.create_hops.get(from_item, "")
            if into is None:
                blocks.append(
                    HopBlock(
                        item_id=from_item, op="resolve", from_items=[from_item],
                        fields=_save_field_map("Q", None, mutation),
                    )
                )
            else:
                status = mutation.get("evidence_status")
                if status is not None and status not in ("verified", "inferred"):
                    raise InvalidInput(f"evidence_status 非法: {status!r}")
                item_id = f"{into}{allocate(into)}"
                blocks.append(
                    HopBlock(
                        item_id=item_id, op="resolve", from_items=[from_item],
                        origin=f"{topic_id}:{from_item}@{origin_hop}", status=status,
                        fields=_save_field_map(into, status, mutation),
                    )
                )
        elif op == "fold":
            from_items = mutation.get("from_items")
            if not isinstance(from_items, list) or not from_items:
                raise InvalidInput("fold from_items 必须是非空数组")
            for from_id in from_items:
                from_id = str(from_id)
                if from_id in created_here:
                    if created_here[from_id].a_state != "x":
                        raise InvalidInput(
                            f"fold 仅适用于已完成 [x] 的 [A] 条目: {from_id}"
                        )
                    continue
                item = _require_topic_item(doc, from_id)
                if item.track != "A" or item.a_state != "x":
                    raise InvalidInput(
                        f"fold 仅适用于已完成 [x] 的 [A] 条目: {from_id}"
                    )
            item_id = f"A{allocate('A')}"
            blocks.append(
                HopBlock(
                    item_id=item_id, op="fold",
                    from_items=[str(i) for i in from_items],
                    fields=_save_field_map("A", None, mutation),
                )
            )
        elif op == "retract":
            item_id = str(mutation.get("item_id", ""))
            if not _ITEM_ID_RE.fullmatch(item_id):
                raise InvalidInput(f"retract item_id 非法: {item_id!r}")
            if item_id not in created_here:
                _require_topic_item(doc, item_id)
            blocks.append(HopBlock(item_id=item_id, op="retract"))
        else:
            raise InvalidInput(f"未知 op: {op!r}")
    return blocks


def _touched_items(mutations: list) -> set:
    """载荷触及的既有条目集合（create 不算冲突，D-P24）；对畸形载荷静默跳过。"""
    touched: set = set()
    for mutation in mutations:
        if not isinstance(mutation, dict):
            continue
        op = mutation.get("op")
        if op in ("update", "retract"):
            touched.add(str(mutation.get("item_id", "")))
        elif op == "overturn":
            touched.add(str(mutation.get("from_item", "")))
        elif op == "resolve":
            touched.add(str(mutation.get("from_item", "")))
        elif op == "fold":
            from_items = mutation.get("from_items")
            if isinstance(from_items, list):
                touched.update(str(i) for i in from_items)
    touched.discard("")
    return touched


def _merge_amend_blocks(old_blocks: list, new_blocks: list) -> list:
    """amend 合并规则（design §4.3(1) amend 列）：本跳刚建的条目在分片内就地收敛。"""
    merged = [replace(block) for block in old_blocks]

    def _created_block(item_id):
        for block in merged:
            if block.item_id == item_id and block.op == "create":
                return block
        return None

    for nb in new_blocks:
        if nb.op == "update":
            target = _created_block(nb.item_id)
            if target is not None:
                target.fields.update(nb.fields)
                if nb.status is not None:
                    target.status = nb.status
                if nb.a_state is not None:
                    target.a_state = nb.a_state
            else:
                merged.append(nb)
        elif nb.op == "retract":
            target = _created_block(nb.item_id)
            if target is not None:
                merged.remove(target)  # 刚 create 的 retract → 从分片抵消删除
            else:
                merged.append(nb)
        elif nb.op == "overturn":
            target = _created_block(nb.from_items[0])
            if target is not None:
                merged.remove(target)
                merged.append(
                    HopBlock(
                        item_id=nb.item_id, op="create", status=nb.status,
                        fields=dict(nb.fields),
                    )
                )  # 刚 create 的 overturn → 替换为 R<n>(op=create)
            else:
                merged.append(nb)
        elif nb.op == "resolve":
            target = _created_block(nb.from_items[0])
            if target is not None:
                merged.remove(target)
                if nb.item_id != nb.from_items[0]:
                    merged.append(
                        HopBlock(
                            item_id=nb.item_id, op="create", status=nb.status,
                            fields=dict(nb.fields),
                        )
                    )  # 刚建当跳即 resolve → 收敛为结果条目
            else:
                merged.append(nb)
        elif nb.op == "fold":
            created_ids = {b.item_id for b in merged if b.op == "create"}
            for block in [
                b for b in merged
                if b.op == "create" and b.item_id in set(nb.from_items)
            ]:
                merged.remove(block)
            new_from = [i for i in nb.from_items if i not in created_ids]
            existing_fold = next(
                (b for b in reversed(merged) if b.op == "fold"), None
            )
            if (
                existing_fold is not None
                and existing_fold.item_id not in set(nb.from_items)
                and new_from
            ):
                # fold 合并 from 集合（自引用既有 fold 结果时不并入，另起新块）
                existing_fold.from_items = list(
                    dict.fromkeys(
                        [i for i in existing_fold.from_items if i not in set(nb.from_items)]
                        + new_from
                    )
                )
                existing_fold.fields.update(nb.fields)
            elif new_from:
                merged.append(
                    HopBlock(
                        item_id=nb.item_id, op="fold", from_items=new_from,
                        fields=dict(nb.fields),
                    )
                )
            else:
                # from 全为本跳刚建条目：不并入既有 fold（否则改写另一条目正文），
                # 收敛为 create x 独立承载汇总（正文不丢）
                merged.append(
                    HopBlock(
                        item_id=nb.item_id, op="create", a_state="x",
                        fields=dict(nb.fields),
                    )
                )
        else:  # create / inherit
            merged.append(nb)
    return merged


def _apply_blocks_and_render(
    topic_id: str, doc, blocks, rep_old, hop_id: str, header: dict
) -> tuple:
    """新块应用到当前态并渲染（D-P22 正向路径）；返回 (md_text, delta ReplayedChain)。

    须在写任何文件之前调用——apply 抛错时零落盘。调用方随后自行写
    hop 分片并以 `hop_sha` 回填 `## 4.` 的 sha12 再渲染 md。
    """
    ctx = ApplyContext(hop_id=hop_id, new_seq=dict(doc.next_seq))
    delta = ReplayedChain(doc=doc)
    delta.create_hops.update(rep_old.create_hops)
    for item_id, hops in rep_old.records.items():
        # 全链 records 基座：既有条目的跨跳 update/retract 才有连续 seq_order（与
        # rebuild 同口径，六表恒等前提）；create_hops 基座供 fold 的 origin_hop 回退。
        delta.records.setdefault(item_id, []).extend(hops)
    delta.overturns.update(rep_old.overturns)
    for block in blocks:
        apply_hop_block(doc, block, ctx)
        _record_block_rows(topic_id, hop_id, block, delta)
    doc.next_seq = dict(ctx.new_seq)
    apply_hop(doc, header, [])  # 头部级字段唯一内核（D-P22）
    return doc, delta


def _render_md_with_tags(doc, records, overturns, head_sha: str) -> str:
    tags = {
        item_id: format_trace_tag(recs) for item_id, recs in records.items()
    }
    for item_id, (origin_hop, overturn_hop) in overturns.items():
        tags[item_id] = format_trace_tag(
            None, origin_hop=origin_hop, overturn_hop=overturn_hop
        )
    if doc.latest is not None:
        doc.latest.sha12 = head_sha[:12]
    return render_topic_md(doc, tags)


def _creating_op(block) -> bool:
    """块是否新建条目（create / inherit（fork·merge 续编）/ overturn 的 R /
    resolve 结果 / fold 的 A_new）。"""
    return block.op in ("create", "inherit") or (
        block.op == "resolve" and bool(block.from_items)
        and block.item_id != block.from_items[0]
    ) or block.op in ("overturn", "fold")


def _count_creates(blocks, exclude_ids: set | None = None) -> dict:
    """净新增条目计数：排除基态既有块已覆盖的同 id 条目（amend 合并口径）。"""
    counts: dict[str, int] = {}
    exclude = exclude_ids or set()
    for block in blocks:
        if _creating_op(block) and block.item_id not in exclude:
            track = _track_of(block.item_id)
            counts[track] = counts.get(track, 0) + 1
    return counts


def _anchor_echo(topic_id, head_hop, sha6, *, new_topic, amend, degraded_from, counts):
    parts = []
    for track in ("C", "R", "Q", "A"):
        if counts.get(track):
            parts.append(f"+{counts[track]}[{track}]")
    tail = " ".join(parts) or "无新增条目"
    if amend:
        return f"⚓ TopicDock: {topic_id} ({head_hop}@{sha6} amend | {tail})"
    if degraded_from:
        return (
            f"⚓ TopicDock: {topic_id} ({degraded_from} 已封板 → "
            f"自动新建 {head_hop}@{sha6} | {tail})"
        )
    if new_topic:
        return f"⚓ TopicDock: {topic_id} ({head_hop}@{sha6} 新建 | {tail})"
    return f"⚓ TopicDock: {topic_id} ({head_hop}@{sha6} | {tail})"


def _save_row_dict(row) -> dict:
    if row is None:
        return None
    keys = (
        "topic_id", "title", "head_hop", "updated_at", "last_actor",
        "next_focus", "next_seq_json", "md_sha256",
    )
    return {key: row[key] for key in keys}


def _commit_new_hop(
    store, conn, *, topic_id, header, blocks, doc, rep_old, old_head,
    old_row, payload_summary, amend_meta, degraded_from=None,
    undo_op_type=None, undo_extra=None,
) -> dict:
    """四步落盘 ③④ 段（CAS 已过）：应用块（重放校验，失败零落盘）→ 渲染 →
    写分片 → 写 md（提交点）→ DB 行 + undo。"""
    hop_id = str(header["hop_id"])
    hop_doc = HopDoc(header=header, blocks=list(blocks))
    hop_text = render_hop_md(hop_doc)
    hop_sha = sha256_text(hop_text)
    try:
        doc, delta = _apply_blocks_and_render(
            topic_id, doc, blocks, rep_old, hop_id, header
        )
    except ValueError as exc:  # 重放失败 = 载荷与当前态不合法：零落盘
        raise InvalidInput(f"载荷与主题状态不相容: {exc}") from exc
    md_text = _render_md_with_tags(doc, delta.records, delta.overturns, hop_sha)
    md_path = store.topics_dir / f"{topic_id}.md"
    old_md_text = md_path.read_text(encoding="utf-8") if md_path.is_file() else None
    hop_path = store.hops_dir / hop_file_name(hop_id)
    atomic_write_text(hop_path, hop_text)  # 步骤 ②：先写分片（meta 已随分配更新）
    atomic_write_text(md_path, md_text)    # 步骤 ③：持久化提交点
    # 步骤 ④：DB 事务（调用方统一提交）
    conn.execute(
        "INSERT OR REPLACE INTO hops(hop_id, topic_origin, session_id, created_at,"
        " actor, summary, next_focus, ref_count, md_sha256) VALUES(?,?,?,?,?,?,?,0,?)",
        (
            hop_id, str(header["topic_origin"]), str(header.get("session_id", "")),
            str(header["created_at"]), str(header["actor"]),
            str(header.get("summary", "")), str(header.get("next_focus", "")), hop_sha,
        ),
    )
    for parent in [str(p) for p in (header.get("parents") or [])]:
        if parent:
            conn.execute(
                "INSERT OR IGNORE INTO hop_edges(parent_hop, child_hop) VALUES(?,?)",
                (parent, hop_id),
            )
    conn.executemany(
        "INSERT OR REPLACE INTO item_hops(topic_id, item_id, hop_id, op, seq_order)"
        " VALUES(?,?,?,?,?)",
        delta.item_hops_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO item_links(topic_id, item_id, hop_id, link_type,"
        " src_topic_id, src_item_id, origin_hop) VALUES(?,?,?,?,?,?,?)",
        delta.item_links_rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO topics(topic_id, title, head_hop, updated_at, last_actor,"
        " next_focus, next_seq_json, md_sha256) VALUES(?,?,?,?,?,?,?,?)",
        (
            topic_id, doc.title, hop_id, doc.updated_at, doc.last_actor,
            str(header.get("next_focus", "")),
            json.dumps(doc.next_seq, ensure_ascii=False, sort_keys=True),
            sha256_text(md_text),
        ),
    )
    refresh_inverted_index(conn, topic_id, doc)
    refresh_ref_counts(conn, [hop_id] + ([old_head] if old_head else []))
    created_files = []
    if old_md_text is None:
        created_files.append(str(md_path))
    undo_payload = {
        "pre_files": {str(md_path): old_md_text} if old_md_text is not None else {},
        "post_hashes": {str(md_path): sha256_text(md_text), str(hop_path): hop_sha},
        "old_row": old_row, "created": created_files, "new_hop": hop_id,
        "amend": bool(amend_meta), "degraded": degraded_from is not None,
        "degraded_from": degraded_from or "", "old_head": old_head or "",
    }
    if undo_extra:
        undo_payload.update(undo_extra)
    push_undo(
        conn, undo_op_type or ("amend" if amend_meta else "save"),
        payload_summary, undo_payload,
    )
    return {"md_text": md_text, "hop_sha": hop_sha, "delta": delta, "doc": doc}


def _state_from_md(store, conn, topic_id):
    """head 分片缺失时的降级基态（D-P20 断链跳过、md 完好=最后提交点）：
    records/create_hops/overturns 取自 DB（item_hops/item_links）。"""
    md_path = store.topics_dir / f"{topic_id}.md"
    doc = parse_topic_md(md_path.read_text(encoding="utf-8"))
    rep = ReplayedChain(doc=doc)
    for found in conn.execute(
        "SELECT item_id, hop_id FROM item_hops"
        " WHERE topic_id=? ORDER BY item_id, seq_order",
        (topic_id,),
    ).fetchall():
        rep.records.setdefault(found["item_id"], []).append(found["hop_id"])
    for item_id, hops in rep.records.items():
        rep.create_hops[item_id] = hops[0]
    for link in conn.execute(
        "SELECT item_id, hop_id, origin_hop FROM item_links"
        " WHERE topic_id=? AND link_type='overturn'",
        (topic_id,),
    ).fetchall():
        if link["item_id"] in rep.records:
            rep.overturns[link["item_id"]] = (
                link["origin_hop"] or "", link["hop_id"],
            )
    return doc, rep


def _floor_next_seq_from_md(store, topic_id: str, doc) -> None:
    """重放基座套用 md frontmatter 的 next_seq 高水位（D-P30 三源之一）。"""
    try:
        md_doc = parse_topic_md(
            (store.topics_dir / f"{topic_id}.md").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, UnsupportedSchemaVersion):
        return
    for track, value in md_doc.next_seq.items():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        doc.next_seq[track] = max(int(doc.next_seq.get(track, 0) or 0), value)


def _save_state(store, conn, topic_id):
    """主题当前态：topics 行 + own 链重放（L4 内核）。

    own 链上首个缺失跳（含 head 本身；md 完好=最后提交点）→ 返回
    (row, None, None, True) 走降级基态 + 自动新建（D-P20 断链跳过；
    引擎自身状态不得自锁为不可写）；不可解析损坏 → InvalidInput（doctor 先行）。
    """
    row = conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
    if row is None:
        return None, [], ReplayedChain(doc=TopicDoc(topic_id=topic_id)), False
    chain, missing, invalid = _walk_origin_chain(store, row["head_hop"], topic_id)
    if chain is None:
        if missing is not None:
            return row, None, None, True
        raise InvalidInput("主题跳链损坏或不完整，请先运行 doctor 自愈后再保存")
    rep = _replay_chain(topic_id, chain)
    # md frontmatter 的 next_seq 高水位（D-P30 三源之一）：
    # undo 后已用号永不复用——重放基座须与 md 存储的取 max
    _floor_next_seq_from_md(store, topic_id, rep.doc)
    return row, chain, rep, False


def _cas_prefix(base_sha: str, current_sha: str) -> bool:
    """base_sha 规则（D-P26）：完整 64-hex 相等或 ≥12 位且为当前值前缀。"""
    if not base_sha:
        return False
    if base_sha == current_sha:
        return True
    return len(base_sha) >= 12 and current_sha.startswith(base_sha)


def _stale_payload(store, conn, topic_id, row, chain, base_hop, mutations, payload):
    """STALE_BASE 载荷（D-P24）：base 后新跳、变更条目、冲突交集与可吸收标志。"""
    chain_ids = [str(loaded.hop.header.get("hop_id", "")) for loaded in chain]
    if base_hop not in chain_ids:
        raise InvalidInput(f"base_hop 不在主题首父链上: {base_hop}")
    after = chain_ids[chain_ids.index(base_hop) + 1 :]
    changed: set = set()
    if after:
        marks = ",".join("?" * len(after))
        for found in conn.execute(
            f"SELECT DISTINCT item_id FROM item_hops WHERE topic_id=? AND hop_id IN ({marks})",
            [topic_id, *after],
        ).fetchall():
            changed.add(found["item_id"])
    conflicts = sorted(_touched_items(mutations) & changed)
    base_loaded = _load_hop(store, base_hop)
    if payload.get("title") is not None or payload.get("artifacts") is not None:
        for hop_id in after:
            loaded = _load_hop(store, hop_id)
            if loaded is None or base_loaded is None:
                continue
            if (
                payload.get("title") is not None
                and str(loaded.hop.header.get("title", ""))
                != str(base_loaded.hop.header.get("title", ""))
                and "title" not in conflicts
            ):
                conflicts.append("title")
            if (
                payload.get("artifacts") is not None
                and loaded.hop.header.get("artifacts") != base_loaded.hop.header.get("artifacts")
                and "artifacts" not in conflicts
            ):
                conflicts.append("artifacts")
    conflicts = sorted(conflicts)
    return {
        "status": "STALE_BASE",
        "topic_id": topic_id,
        "current_head": row["head_hop"],
        "current_sha": row["md_sha256"],
        "hops_after_base": after,
        "changed_items": sorted(changed),
        "conflicts": conflicts,
        "auto_absorbable": not conflicts,
    }


def _normalize_actor(actor) -> str:
    """载荷 actor 归一化：None/空白串 → ""（防 `null` 落盘为 `"None"`）。"""
    if actor is None:
        return ""
    actor = str(actor)
    return "" if not actor.strip() else actor


def _actor_subject(actor: str) -> str:
    """actor 主体：剥离遗留 `@<model>` 后缀（D7 跨 actor amend 判别用）。"""
    return actor.split("@", 1)[0].strip()


def _normalize_model(model) -> str:
    """载荷/参数 model 归一化：非 None 且非 str → INVALID_INPUT；空白 → 缺省。"""
    if model is None:
        return ""
    if not isinstance(model, str):
        raise InvalidInput("model 必须是字符串")
    return model.strip()


def _hop_trace_header(header: dict, actor: str = "", model: str = "",
                      *, amend: bool = False) -> dict:
    """D6 跳头 actor/model 的唯一落点。

    新建跳：actor 恒写；model 非空才落键（缺省/空白不写键，老跳字节零漂移）。
    amend：model 非空才更新、缺省/空白保留原值；actor 保留调用前值（空 actor 回填
    由 amend 编排在调用前完成）。
    """
    if amend:
        if model:
            header["model"] = model
        return header
    header["actor"] = actor
    if model:
        header["model"] = model
    return header


# ---------- D8 位置无关 Merkle 身份收口 ----------

def _scan_topic_hops(store, topic_id):
    """扫描 hops_dir 中 topic_origin == topic_id 的存活分片（跳号倒序）。"""
    scanned = []
    try:
        paths = list(store.hops_dir.glob("hop-*.md"))
    except OSError:
        return scanned
    for path in paths:
        match = re.fullmatch(r"hop-(\d+)\.md", path.name)
        if match is None:
            continue
        loaded = _load_hop(store, format_hop_id(int(match.group(1))))
        if loaded is None:
            continue
        if str(loaded.hop.header.get("topic_origin", "")) != topic_id:
            continue
        scanned.append(loaded)
    scanned.sort(
        key=lambda loaded: _hop_number(str(loaded.hop.header.get("hop_id", ""))),
        reverse=True,
    )
    return scanned


def _persisted_topic_uid(store, conn, topic_id, chain=None):
    """最近一个已持久化非空 str topic_uid；断链时扫描存活分片（不派生）。"""
    if chain is None:
        row = conn.execute(
            "SELECT head_hop FROM topics WHERE topic_id=?", (topic_id,)
        ).fetchone()
        if row is None:
            chain = []
        else:
            walked, _missing, _invalid = _walk_origin_chain(
                store, row["head_hop"], topic_id
            )
            chain = walked or []
    for loaded in reversed(chain):
        uid = loaded.hop.header.get("topic_uid")
        if isinstance(uid, str) and uid:
            return uid
    for loaded in _scan_topic_hops(store, topic_id):
        uid = loaded.hop.header.get("topic_uid")
        if isinstance(uid, str) and uid:
            return uid
    return ""


def _resolve_topic_uid(store, conn, topic_id, chain):
    """D8 topic_uid 继承；整链无 uid 时按可达最早跳确定性派生。"""
    uid = _persisted_topic_uid(store, conn, topic_id, chain)
    if uid:
        return uid
    if chain:
        earliest = chain[0]
    else:
        scanned = _scan_topic_hops(store, topic_id)
        earliest = scanned[-1] if scanned else None
    if earliest is None:
        # 整链分片均不可达：无最早跳可派生，只能开新血统
        return uuid.uuid4().hex
    digest = hashlib.sha256(
        b"topicdock-topic-uid:v1\0"
        + compute_hop_oid(earliest.hop).encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _finalize_hop_merkle_header(
    store, conn, topic_id, header, blocks, chain, rep, *, fresh_uid=False
):
    """D8 唯一收口：header/blocks 定型后写入 4 个位置无关 Merkle 字段。"""
    if fresh_uid:
        header["topic_uid"] = uuid.uuid4().hex
    else:
        current_uid = header.get("topic_uid")
        if not (isinstance(current_uid, str) and current_uid):
            header["topic_uid"] = _resolve_topic_uid(
                store, conn, topic_id, list(chain or [])
            )

    parents = header.get("parents")
    if not isinstance(parents, list):
        parents = []
    parent_oids = []
    for parent in parents:
        parent_loaded = _load_hop(store, str(parent))
        parent_oids.append(
            compute_hop_oid(parent_loaded.hop) if parent_loaded is not None else ""
        )
    header["parent_oids"] = parent_oids

    origin_oids = {}
    for block in blocks:
        if not block.origin:
            continue
        parsed = _parse_origin(block.origin)
        if parsed is None:
            continue
        src_topic, src_item, origin_hop = parsed
        src_uid = _persisted_topic_uid(store, conn, src_topic)
        origin_loaded = _load_hop(store, origin_hop)
        origin_oid = (
            compute_hop_oid(origin_loaded.hop)
            if origin_loaded is not None else ""
        )
        origin_oids[str(block.item_id)] = f"{src_uid}:{src_item}@{origin_oid}"
    if origin_oids:
        header["origin_oids"] = origin_oids
    else:
        header.pop("origin_oids", None)

    header["hop_oid"] = compute_hop_oid(
        HopDoc(header=header, blocks=list(blocks))
    )
    return header


def _save_new_topic(store, conn, payload, topic_id, actor, model):
    """场景 1：base_hop=null 首次建档。"""
    doc = TopicDoc(topic_id=topic_id, title=str(payload.get("title") or topic_id))
    _counters, allocate = _make_allocator(conn, topic_id, doc)
    blocks = _normalize_mutations(
        conn, topic_id, doc, ReplayedChain(doc=doc), payload.get("mutations") or [], allocate
    )
    hop_id = allocate_hop_id(store, conn)
    header = _hop_trace_header({
        "v": 1, "hop_id": hop_id, "topic_origin": topic_id,
        "title": doc.title, "parents": [],
        "session_id": str(payload.get("session_id", "")),
        "created_at": _utc_now(),
        "summary": str(payload.get("summary", "")),
        "next_focus": str(payload.get("next_focus", "")),
        "artifacts": _normalize_artifacts(payload.get("artifacts"), None),
    }, actor, model)
    _finalize_hop_merkle_header(
        store, conn, topic_id, header, blocks, [], ReplayedChain(doc=doc),
        fresh_uid=True,
    )
    counts = _count_creates(blocks)
    committed = _commit_new_hop(
        store, conn, topic_id=topic_id, header=header, blocks=blocks, doc=doc,
        rep_old=ReplayedChain(doc=doc), old_head=None, old_row=None,
        payload_summary=str(payload.get("summary", "")), amend_meta=None,
    )
    base_sha = sha256_text(committed["md_text"])
    return {
        "status": "OK", "topic_id": topic_id, "head_hop": hop_id, "base_sha": base_sha,
        "amend": False, "degraded": False, "created": counts,
        "echo": _anchor_echo(
            topic_id, hop_id, base_sha[:6], new_topic=True, amend=False,
            degraded_from=None, counts=counts,
        ),
        "conflicts": [], "overwritten": [],
    }


def _save_amend(store, conn, payload, topic_id, row, chain, rep, counters_allocate,
                model, actor):
    """amend 原地合并（D-P10/35）：先重放校验（任何载荷不相容零落盘），
    再 pending → 分片覆写 → md（提交点）→ DB 追平。"""
    head = row["head_hop"]
    head_loaded = _load_hop(store, head)
    # D8：必须在改 created_at/summary/blocks 之前基于旧态完成 uid 提取/派生
    resolved_uid = _resolve_topic_uid(store, conn, topic_id, chain)
    _counters, allocate = counters_allocate
    new_blocks = _normalize_mutations(
        conn, topic_id, rep.doc, rep, payload.get("mutations") or [], allocate
    )
    old_blocks = head_loaded.hop.blocks
    merged_blocks = _merge_amend_blocks(old_blocks, new_blocks)
    new_header = dict(head_loaded.hop.header)
    if payload.get("summary") is not None:
        new_header["summary"] = str(payload["summary"])
    if payload.get("next_focus") is not None:
        new_header["next_focus"] = str(payload["next_focus"])
    if payload.get("title"):
        new_header["title"] = str(payload["title"])  # 非空才改（空串=保持）
    new_header["artifacts"] = _normalize_artifacts(
        payload.get("artifacts"), head_loaded.hop.header.get("artifacts") or []
    )
    new_header["created_at"] = _utc_now()  # D-P10：head created_at ≡ topics.updated_at 唯一来源
    if actor and not _normalize_actor(new_header.get("actor")):
        new_header["actor"] = actor  # D7：空 actor 跳在 amend 时回填载荷 actor（防归属丢失）
    _hop_trace_header(new_header, model=model, amend=True)
    new_header["topic_uid"] = resolved_uid
    _finalize_hop_merkle_header(
        store, conn, topic_id, new_header, merged_blocks, chain, rep
    )
    hop_text = render_hop_md(HopDoc(header=new_header, blocks=merged_blocks))
    new_sha = sha256_text(hop_text)
    # 重放先行：载荷内顺序不合法 / 合并后自引用等在此收敛为 InvalidInput，磁盘零改动
    new_chain = chain[:-1] + [
        LoadedHop(hop=parse_hop_md(hop_text), sha=new_sha, path=head_loaded.path)
    ]
    try:
        rep2 = _replay_chain(topic_id, new_chain)
        floor = json.loads(row["next_seq_json"])
        if not isinstance(floor, dict):
            floor = {}
    except (ValueError, json.JSONDecodeError):
        raise InvalidInput("amend 合并后重放失败（载荷顺序或状态不合法）")
    md_text = _render_replayed_text(rep2, new_sha, floor)
    pending_path = store.root / _AMEND_PENDING_NAME
    atomic_write_text(
        pending_path,
        json.dumps({"hop_id": head, "expected_sha": new_sha}),  # D-P35：先写 pending
    )
    old_hop_text = head_loaded.path.read_text(encoding="utf-8")
    atomic_write_text(head_loaded.path, hop_text)  # 步骤 ②：分片原子覆写
    md_path = store.topics_dir / f"{topic_id}.md"
    old_md_text = md_path.read_text(encoding="utf-8")
    atomic_write_text(md_path, md_text)  # 步骤 ③：持久化提交点
    # 步骤 ④：DB 追平（调用方统一提交）
    conn.execute(
        "UPDATE hops SET md_sha256=?, summary=?, next_focus=?, created_at=?, actor=?"
        " WHERE hop_id=?",
        (new_sha, new_header["summary"], new_header["next_focus"],
         new_header["created_at"], str(new_header.get("actor", "")), head),
    )
    conn.execute("DELETE FROM item_hops WHERE topic_id=? AND hop_id=?", (topic_id, head))
    conn.execute("DELETE FROM item_links WHERE topic_id=? AND hop_id=?", (topic_id, head))
    conn.executemany(
        "INSERT OR REPLACE INTO item_hops(topic_id, item_id, hop_id, op, seq_order)"
        " VALUES(?,?,?,?,?)",
        [r for r in rep2.item_hops_rows if r[2] == head],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO item_links(topic_id, item_id, hop_id, link_type,"
        " src_topic_id, src_item_id, origin_hop) VALUES(?,?,?,?,?,?,?)",
        [r for r in rep2.item_links_rows if r[2] == head],
    )
    conn.execute(
        "UPDATE topics SET title=?, head_hop=?, updated_at=?, last_actor=?, next_focus=?,"
        " next_seq_json=?, md_sha256=? WHERE topic_id=?",
        (
            rep2.doc.title, head, new_header["created_at"],
            str(new_header.get("actor", "")), str(new_header["next_focus"]),
            json.dumps(rep2.doc.next_seq, ensure_ascii=False, sort_keys=True),
            sha256_text(md_text), topic_id,
        ),
    )
    refresh_inverted_index(conn, topic_id, rep2.doc)
    push_undo(
        conn, "amend", str(payload.get("summary", "")),
        {
            "pre_files": {str(md_path): old_md_text, str(head_loaded.path): old_hop_text},
            "post_hashes": {
                str(md_path): sha256_text(md_text), str(head_loaded.path): new_sha,
            },
            "old_row": _save_row_dict(row), "created": [], "new_hop": head,
            "amend": True, "old_head": head, "old_hop_sha": head_loaded.sha,
        },
    )
    old_creating = {b.item_id for b in old_blocks if _creating_op(b)}  # 全部建条目型块（N3）
    counts = _count_creates(merged_blocks, old_creating)  # 合并后净新增（口径与实际落盘一致）
    base_sha = sha256_text(md_text)
    result = {
        "status": "OK", "topic_id": topic_id, "head_hop": head, "base_sha": base_sha,
        "amend": True, "degraded": False, "created": counts,
        "echo": _anchor_echo(
            topic_id, head, base_sha[:6], new_topic=False, amend=True,
            degraded_from=None, counts=counts,
        ),
        "conflicts": [], "overwritten": [], "_deferred": [pending_path],
    }
    return result


def _save_fork(store, conn, payload, topic_id, row, chain, rep, base_hop, fork_as,
               actor, model):
    """--fork-as 分叉（D-P25）：源=base_hop 态，新主题首跳 parents=[base_hop]。"""
    chain_ids = [str(loaded.hop.header.get("hop_id", "")) for loaded in chain]
    prefix = chain[: chain_ids.index(base_hop) + 1]
    rep_base = rep if base_hop == row["head_hop"] else _replay_chain(topic_id, prefix)
    base_header = prefix[-1].hop.header
    inherit_blocks = []
    for track in _TRACKS:
        for item in rep_base.doc.items.get(track, []):
            inherit_blocks.append(
                HopBlock(
                    item_id=item.item_id, op="inherit", status=item.evidence_status,
                    a_state=item.a_state if track == "A" else None,
                    origin=(
                        f"{topic_id}:{item.item_id}@"
                        f"{rep_base.create_hops.get(item.item_id, '')}"
                    ),
                    fields=_item_fields_snapshot(item),
                )
            )
    draft = TopicDoc(topic_id=fork_as)
    draft_ctx = ApplyContext(hop_id="H000", new_seq={})
    for block in inherit_blocks:
        apply_hop_block(draft, block, draft_ctx)
    _counters, allocate = _make_allocator(conn, fork_as, draft)
    mutation_blocks = _normalize_mutations(
        conn, fork_as, draft, rep_base, payload.get("mutations") or [], allocate
    )
    blocks = inherit_blocks + mutation_blocks
    hop_id = allocate_hop_id(store, conn)
    title = (
        str(payload["title"]) if payload.get("title")
        else str(base_header.get("title", ""))  # 非空才改（空串=继承）
    )
    header = _hop_trace_header({
        "v": 1, "hop_id": hop_id, "topic_origin": fork_as, "title": title,
        "parents": [base_hop], "session_id": str(payload.get("session_id", "")),
        "created_at": _utc_now(),
        "summary": str(payload.get("summary", "")),
        "next_focus": str(payload.get("next_focus", "")),
        "artifacts": _normalize_artifacts(
            payload.get("artifacts"), base_header.get("artifacts") or []
        ),
    }, actor, model)
    _finalize_hop_merkle_header(
        store, conn, fork_as, header, blocks, [], rep_base, fresh_uid=True
    )
    doc = TopicDoc(topic_id=fork_as, title=title)
    counts = _count_creates(mutation_blocks)
    committed = _commit_new_hop(
        store, conn, topic_id=fork_as, header=header, blocks=blocks, doc=doc,
        rep_old=ReplayedChain(doc=doc), old_head=None, old_row=None,
        payload_summary=str(payload.get("summary", "")), amend_meta=None,
    )
    refresh_ref_counts(conn, [base_hop])  # base_hop ref +1 自动封板
    base_sha = sha256_text(committed["md_text"])
    return {
        "status": "OK", "topic_id": fork_as, "head_hop": hop_id, "base_sha": base_sha,
        "amend": False, "degraded": False, "created": counts, "fork_from": topic_id,
        "fork_base": base_hop,
        "echo": _anchor_echo(
            fork_as, hop_id, base_sha[:6], new_topic=True, amend=False,
            degraded_from=None, counts=counts,
        ),
        "conflicts": [], "overwritten": [],
    }


def _save_locked(store, conn, payload, amend, fork_as, force_merge):
    actor = _normalize_actor(payload.get("actor"))
    model = _normalize_model(payload.get("model"))
    topic_id = str(payload.get("topic_id", ""))
    if not _TOPIC_ID_RE.fullmatch(topic_id):
        raise InvalidInput(f"topic_id 非法: {topic_id!r}")
    if fork_as is not None:
        if not _TOPIC_ID_RE.fullmatch(fork_as):
            raise InvalidInput(f"--fork-as 新主题 id 非法: {fork_as!r}")
        if amend or force_merge:
            raise InvalidInput("--fork-as 与 --amend/--force-merge 组合一律非法")
        if (
            conn.execute("SELECT 1 FROM topics WHERE topic_id=?", (fork_as,)).fetchone()
            is not None
        ):
            raise InvalidInput(f"新主题已存在: {fork_as}")
        if (store.topics_dir / f"{fork_as}.md").exists():
            raise InvalidInput(f"新主题 md 已存在: {fork_as}.md")
    base_hop = payload.get("base_hop") or None
    if base_hop is not None and not re.fullmatch(r"H\d+", str(base_hop)):
        raise InvalidInput(f"base_hop 非法: {base_hop!r}")
    base_hop = str(base_hop) if base_hop else None
    base_sha = str(payload.get("base_sha") or "")
    mutations = payload.get("mutations") or []
    if not isinstance(mutations, list):
        raise InvalidInput("mutations 必须是数组")
    session_id = str(payload.get("session_id", ""))
    parent_hops = payload.get("parent_hops")
    if parent_hops is not None:
        if not isinstance(parent_hops, list):
            raise InvalidInput("parent_hops 必须是数组")
        expected = [base_hop] if base_hop else []
        if [str(p) for p in parent_hops] != expected:
            raise InvalidInput("parent_hops 仅允许等于 [base_hop]")

    row, chain, rep, head_missing = _save_state(store, conn, topic_id)

    if row is None:
        if fork_as is not None:
            raise TopicNotFoundError(f"源主题不存在: {topic_id}")
        if base_hop is not None:
            raise TopicNotFoundError(f"主题不存在: {topic_id}")
        return _save_new_topic(store, conn, payload, topic_id, actor, model)

    head = row["head_hop"]
    if head_missing:
        # head 分片缺失（断链跳过）：md=最后提交点 → 降级基态 + 自动新建下一跳
        _fallback_doc, rep = _state_from_md(store, conn, topic_id)
        chain = []
    chain_ids = [str(loaded.hop.header.get("hop_id", "")) for loaded in chain]
    if base_hop is None:
        raise InvalidInput("非新建主题的载荷必须携带 base_hop")
    if head_missing:
        if base_hop != head:
            raise InvalidInput(f"head 分片缺失时 base_hop 必须为当前 head: {base_hop}")
    elif base_hop not in chain_ids:
        raise InvalidInput(f"base_hop 不在主题首父链上: {base_hop}")

    if fork_as is not None:
        if head_missing:
            # 链上缺失 → INVALID_INPUT（7）而非 ValueError 抢跑（D-P25）
            raise InvalidInput("head 分片缺失时暂不支持分叉（请先恢复或重建该跳）")
        if base_hop == head and not _cas_prefix(base_sha, row["md_sha256"]):
            raise StaleBase(_stale_payload(
                store, conn, topic_id, row, chain, base_hop, mutations, payload
            ))
        return _save_fork(
            store, conn, payload, topic_id, row, chain, rep, base_hop, fork_as,
            actor, model,
        )

    if base_hop != head:
        if force_merge:
            return _save_force_merge(
                store, conn, payload, topic_id, row, chain, rep, base_hop, mutations,
                actor, model,
            )
        raise StaleBase(_stale_payload(
            store, conn, topic_id, row, chain, base_hop, mutations, payload
        ))
    if not _cas_prefix(base_sha, row["md_sha256"]):
        # 同 head 失配 = head 原地变化（amend），无「base 后新跳」可查——
        # 自动吸收会静默覆盖另一窗口的 amend，保守判不可吸收（5C）；
        # --force-merge 以当前 head 为基重放（最后写入胜）。
        if force_merge:
            return _save_force_merge(
                store, conn, payload, topic_id, row, chain, rep, head, mutations,
                actor, model,
            )
        raise StaleBase({
            "status": "STALE_BASE", "topic_id": topic_id, "current_head": head,
            "current_sha": row["md_sha256"], "hops_after_base": [],
            "changed_items": [], "conflicts": [],
            "auto_absorbable": False, "reason": "BASE_SHA_MISMATCH_HEAD",
        })

    if amend:
        head_loaded = _load_hop(store, head)
        head_actor = _normalize_actor(
            head_loaded.hop.header.get("actor") if head_loaded is not None else None
        )
        # D7：新旧 actor 均非空且主体（剥离遗留 @model）不等 → 视同切换角色，降级新建
        actor_changed = bool(
            head_actor and actor
            and _actor_subject(head_actor) != _actor_subject(actor)
        )
        # D8 发布即封板：head 的权威 oid 已封板或台账损坏 → 一律失败关闭降级新建
        published_oids, ledger_corrupt = read_published_oids(store)
        head_published = False
        if head_loaded is not None and not ledger_corrupt:
            try:
                head_published = compute_hop_oid(head_loaded.hop) in published_oids
            except ValueError:
                head_published = False
        amend_ok = (
            head_loaded is not None
            and not head_missing  # own 链有祖先缺失时 amend 只重放残缺链——禁止，降级新建
            and session_id
            and str(head_loaded.hop.header.get("session_id", "")) == session_id
            and live_refs(conn, head) == 1
            and not actor_changed
            and not (ledger_corrupt or head_published)
        )
        if amend_ok:
            return _save_amend(
                store, conn, payload, topic_id, row, chain, rep,
                _make_allocator(conn, topic_id, rep.doc), model, actor,
            )
        # 封板/session 失配/head 缺失 → 自动降级新建下一跳（场景 2B）
        degraded_from = head
    else:
        # head 分片缺失同样走降级新建（D-P20 复审 #9）
        degraded_from = head if head_missing else None

    _counters, allocate = _make_allocator(conn, topic_id, rep.doc)
    blocks = _normalize_mutations(conn, topic_id, rep.doc, rep, mutations, allocate)
    hop_id = allocate_hop_id(store, conn)
    base_header = chain[-1].hop.header if chain else {}
    if chain:
        artifacts_base = base_header.get("artifacts") or []
    else:
        # head 缺失降级：artifacts 继承回退为解析 md ## 3.（D-P20 复审 #9）
        artifacts_base = [
            {"name": name, "path": path, "note": note or ""}
            for name, path, note in rep.doc.artifacts
        ]
    payload_title = payload.get("title")
    header = _hop_trace_header({
        "v": 1, "hop_id": hop_id, "topic_origin": topic_id,
        "title": str(payload_title) if payload_title
        else str(rep.doc.title or topic_id),  # 非空才改
        "parents": [head],
        "session_id": session_id, "created_at": _utc_now(),
        "summary": str(payload.get("summary", "")),
        "next_focus": str(payload.get("next_focus", "")),
        "artifacts": _normalize_artifacts(payload.get("artifacts"), artifacts_base),
    }, actor, model)
    _finalize_hop_merkle_header(
        store, conn, topic_id, header, blocks, chain or [], rep
    )
    counts = _count_creates(blocks)
    committed = _commit_new_hop(
        store, conn, topic_id=topic_id, header=header, blocks=blocks, doc=rep.doc,
        rep_old=rep, old_head=head, old_row=_save_row_dict(row),
        payload_summary=str(payload.get("summary", "")), amend_meta=None,
        degraded_from=degraded_from,
    )
    base_sha = sha256_text(committed["md_text"])
    return {
        "status": "OK", "topic_id": topic_id, "head_hop": hop_id, "base_sha": base_sha,
        "amend": False, "degraded": degraded_from is not None,
        "created": counts,
        "echo": _anchor_echo(
            topic_id, hop_id, base_sha[:6], new_topic=False, amend=False,
            degraded_from=degraded_from, counts=counts,
        ),
        "conflicts": [], "overwritten": [],
    }


def _save_force_merge(store, conn, payload, topic_id, row, chain, rep, base_hop,
                      mutations, actor, model):
    """--force-merge（D-P24）：以当前 head 为基重放（最后写入胜）。

    载荷引用已消失条目 → INVALID_INPUT + 清单；同载荷刚分配的新号
    不属于「既有引用」不判 vanished；overwritten[] = {item_id, by_hop}。
    """
    _counters, allocate = _make_allocator(conn, topic_id, rep.doc)
    touched = _touched_items(mutations)
    try:
        blocks = _normalize_mutations(
            conn, topic_id, rep.doc, rep, mutations, allocate
        )
    except ItemNotFoundError:
        # 引用已消失条目（D-P24）：INVALID_INPUT + 清单，不静默丢弃
        vanished = sorted(
            item_id for item_id in touched if _find_item(rep.doc, item_id) is None
        )
        raise InvalidInput(
            {"detail": "载荷引用的条目已消失", "vanished_items": vanished}
        )
    payload_created = {b.item_id for b in blocks if _creating_op(b)}
    existing_refs = touched - payload_created
    vanished = sorted(
        item_id for item_id in existing_refs if _find_item(rep.doc, item_id) is None
    )
    if vanished:
        raise InvalidInput(
            {"detail": "载荷引用的条目已消失", "vanished_items": vanished}
        )
    overwritten: list = []
    if chain:
        chain_ids = [str(loaded.hop.header.get("hop_id", "")) for loaded in chain]
        if base_hop in chain_ids:
            after = chain_ids[chain_ids.index(base_hop) + 1 :]
            if after:
                marks = ",".join("?" * len(after))
                for found in conn.execute(
                    f"SELECT item_id, hop_id FROM item_hops"
                    f" WHERE topic_id=? AND hop_id IN ({marks}) ORDER BY seq_order",
                    [topic_id, *after],
                ).fetchall():
                    if found["item_id"] in existing_refs:
                        overwritten.append(
                            {"item_id": found["item_id"], "by_hop": found["hop_id"]}
                        )
                seen = set()
                overwritten = [
                    entry for entry in overwritten
                    if not (entry["item_id"] in seen or seen.add(entry["item_id"]))
                ]
    hop_id = allocate_hop_id(store, conn)
    if chain:
        artifacts_base = chain[-1].hop.header.get("artifacts") or []
    else:
        # head 缺失降级：artifacts 继承回退为解析 md ## 3.（与普通降级路径同口径）
        artifacts_base = [
            {"name": name, "path": path, "note": note or ""}
            for name, path, note in rep.doc.artifacts
        ]
    header = _hop_trace_header({
        "v": 1, "hop_id": hop_id, "topic_origin": topic_id,
        "title": str(payload["title"]) if payload.get("title")
        else str(rep.doc.title or topic_id),
        "parents": [row["head_hop"]],
        "session_id": str(payload.get("session_id", "")),
        "created_at": _utc_now(),
        "summary": str(payload.get("summary", "")),
        "next_focus": str(payload.get("next_focus", "")),
        "artifacts": _normalize_artifacts(
            payload.get("artifacts"), artifacts_base
        ),
    }, actor, model)
    _finalize_hop_merkle_header(
        store, conn, topic_id, header, blocks, chain or [], rep
    )
    counts = _count_creates(blocks)
    committed = _commit_new_hop(
        store, conn, topic_id=topic_id, header=header, blocks=blocks, doc=rep.doc,
        rep_old=rep, old_head=row["head_hop"], old_row=_save_row_dict(row),
        payload_summary=str(payload.get("summary", "")), amend_meta=None,
    )
    base_sha = sha256_text(committed["md_text"])
    return {
        "status": "OK", "topic_id": topic_id, "head_hop": hop_id, "base_sha": base_sha,
        "amend": False, "degraded": False, "created": counts,
        "echo": _anchor_echo(
            topic_id, hop_id, base_sha[:6], new_topic=False, amend=False,
            degraded_from=None, counts=counts,
        ),
        "conflicts": [], "overwritten": overwritten,
    }


def cmd_save(args) -> tuple[dict, int]:
    """`td save --stdin | --json-file [--amend] [--fork-as] [--force-merge]`。"""
    payload = _read_save_payload(args)
    start_dir = Path(args.workspace) if getattr(args, "workspace", None) else None
    store = resolve_store(start_dir, getattr(args, "store_dir", None))
    if store is None or not _is_initialized_store(store.root):
        return (
            {
                "status": "UNINITIALIZED_STORE", "default_global": "~/.topicdock",
                "workspace_local": str(Path.cwd() / ".topicdock"),
            },
            EXIT_UNINITIALIZED_STORE,
        )
    deferred: list = []
    with store.exclusive_lock():
        conn, _heal = ensure_store_healthy(store)
        try:
            result = _save_locked(
                store, conn, payload,
                bool(getattr(args, "amend", False)),
                getattr(args, "fork_as", None),
                bool(getattr(args, "force_merge", False)),
            )
            deferred = result.pop("_deferred", [])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        # pending 记录的删除须在锁内：防止与并发命令的 pending 记录互删
        for path in deferred:
            path.unlink(missing_ok=True)
    return result, EXIT_OK


def _read_save_payload(args) -> dict:
    if getattr(args, "stdin", False):
        try:
            text = sys.stdin.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidInput(f"标准输入不可读: {exc}")
    elif getattr(args, "json_file", None):
        try:
            text = Path(args.json_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidInput(f"载荷文件不可读: {args.json_file}")
    else:
        raise InvalidInput("save 需要 --stdin 或 --json-file 提供载荷")
    try:
        payload = json.loads(text)
    except ValueError:
        raise InvalidInput("载荷不是合法 JSON")
    if not isinstance(payload, dict):
        raise InvalidInput("载荷必须是 JSON 对象")
    return payload


# ---------- DAG 写路径命令（merge / delete / undo / doctor） ----------
"""M7：merge 双父合流、delete 两阶段级联、undo journal 绝对还原、doctor 巡检。"""


def _artifacts_union(*snapshots: list) -> list:
    """双亲快照按 path 保序去重并集（先到优先，D-P33 merge 流转）。"""
    seen: set = set()
    out: list = []
    for snapshot in snapshots:
        for entry in snapshot or []:
            key = str(entry.get("path", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": str(entry.get("name", "")),
                    "path": key,
                    "note": str(entry.get("note", "") or ""),
                }
            )
    return out


def _merge_payload(store, conn, args) -> dict:
    """merge 的载入与四步合流（M7 提交序：hop 文件 → dst md → DB → src.md 延后删）。"""
    src_id = str(getattr(args, "src", ""))
    dst_id = str(getattr(args, "dst", ""))
    if not _TOPIC_ID_RE.fullmatch(src_id) or not _TOPIC_ID_RE.fullmatch(dst_id):
        raise InvalidInput(f"merge 参数非法: {src_id!r} {dst_id!r}")
    if src_id == dst_id:
        raise InvalidInput("merge 源与目标不能相同")
    src_row = conn.execute(
        "SELECT * FROM topics WHERE topic_id=?", (src_id,)
    ).fetchone()
    dst_row = conn.execute(
        "SELECT * FROM topics WHERE topic_id=?", (dst_id,)
    ).fetchone()
    if src_row is None:
        raise TopicNotFoundError(f"源主题不存在: {src_id}")
    if dst_row is None:
        raise TopicNotFoundError(f"目标主题不存在: {dst_id}")

    chain_src, src_missing, src_invalid = _walk_origin_chain(
        store, src_row["head_hop"], src_id
    )
    if chain_src is None:
        raise InvalidInput("源主题跳链损坏或不完整，请先运行 doctor 自愈")
    rep_src = _replay_chain(src_id, chain_src)
    chain_dst, dst_missing, dst_invalid = _walk_origin_chain(
        store, dst_row["head_hop"], dst_id
    )
    if chain_dst is None:
        raise InvalidInput("目标主题跳链损坏或不完整，请先运行 doctor 自愈")
    rep_dst = _replay_chain(dst_id, chain_dst)
    _floor_next_seq_from_md(store, dst_id, rep_dst.doc)  # md 高水位（撤号不复用）

    _counters, allocate = _make_allocator(conn, dst_id, rep_dst.doc)
    inherit_blocks = []
    for track in _TRACKS:
        for item in rep_src.doc.items.get(track, []):
            inherit_blocks.append(
                HopBlock(
                    item_id=f"{track}{allocate(track)}", op="inherit",
                    status=item.evidence_status,
                    a_state=item.a_state if track == "A" else None,
                    origin=(
                        f"{src_id}:{item.item_id}@"
                        f"{rep_src.create_hops.get(item.item_id, '')}"
                    ),
                    fields=_item_fields_snapshot(item),
                )
            )

    hop_id = allocate_hop_id(store, conn)
    summary = str(getattr(args, "summary", "") or f"并入 {src_id}")
    actor = _normalize_actor(getattr(args, "actor", None))
    model = _normalize_model(getattr(args, "model", None))
    header = _hop_trace_header({
        "v": 1, "hop_id": hop_id, "topic_origin": dst_id,
        "title": str(dst_row["title"]),  # title 取 dst（D-P33）
        "parents": [dst_row["head_hop"], src_row["head_hop"]],
        "session_id": str(getattr(args, "session_id", "") or ""),
        "created_at": _utc_now(),
        "summary": summary,
        "next_focus": str(getattr(args, "next_focus", "") or ""),
        "artifacts": _artifacts_union(  # 双亲最新跳快照按 path 保序去重并集
            chain_dst[-1].hop.header.get("artifacts") or [],
            chain_src[-1].hop.header.get("artifacts") or [],
        ),
    }, actor, model)
    _finalize_hop_merkle_header(
        store, conn, dst_id, header, inherit_blocks, chain_dst, rep_dst
    )
    src_md_path = store.topics_dir / f"{src_id}.md"
    pre_dst_md = (store.topics_dir / f"{dst_id}.md").read_text(encoding="utf-8")
    committed = _commit_new_hop(
        store, conn, topic_id=dst_id, header=header, blocks=inherit_blocks,
        doc=rep_dst.doc, rep_old=rep_dst, old_head=dst_row["head_hop"],
        old_row=_save_row_dict(dst_row), payload_summary=summary,
        amend_meta=None, undo_op_type="merge",
        undo_extra={
            "pre_files": {
                str(store.topics_dir / f"{dst_id}.md"): pre_dst_md,
                str(src_md_path): src_md_path.read_text(encoding="utf-8"),
            },
            "merged_from": src_id,
        },
    )
    # src 行删除（DB 同事务）；src 派生行保留（trace 递归依赖，复审 #8）
    conn.execute("DELETE FROM topics WHERE topic_id=?", (src_id,))
    conn.execute("DELETE FROM inverted_index WHERE topic_id=?", (src_id,))
    refresh_ref_counts(conn, [hop_id, src_row["head_hop"]])
    return {
        "status": "OK", "topic_id": dst_id, "dst_topic_id": dst_id,
        "head_hop": hop_id, "base_sha": sha256_text(committed["md_text"]),
        "merged_items": _count_creates(inherit_blocks), "merged_from": src_id,
        "echo": _anchor_echo(
            dst_id, hop_id, sha256_text(committed["md_text"])[:6],
            new_topic=False, amend=False, degraded_from=None,
            counts=_count_creates(inherit_blocks),
        ),
        "_deferred": [src_md_path],
    }


def _parse_delete_expression(conn, expr: str, keep_mode: bool) -> list:
    """`2,4~6`（列表/区间）或 `keep 1,3`（补集）→ 主题 id 列表（按 list 同序）。"""
    rows = _list_topic_rows(conn)
    if keep_mode:
        raw = expr.strip()
        if not raw:
            raise InvalidInput("keep 表达式为空")
        keep_indices = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or not chunk.isdigit():
                raise InvalidInput(f"keep 表达式序号非法: {chunk!r}")
            keep_indices.add(int(chunk))
        total = len(rows)
        for index in keep_indices:
            if index < 1 or index > total:
                raise InvalidInput(f"keep 表达式序号超出范围: {index}")
        ids = [
            row["topic_id"] for index, row in enumerate(rows, start=1)
            if index not in keep_indices
        ]
    else:
        raw = expr.strip()
        if not raw:
            raise InvalidInput("删除表达式为空")
        indices = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            match = re.fullmatch(r"(\d+)~(\d+)", chunk)
            if match:
                lo, hi = int(match.group(1)), int(match.group(2))
                if lo > hi:
                    lo, hi = hi, lo
                indices.update(range(lo, hi + 1))
            elif chunk.isdigit():
                indices.add(int(chunk))
            else:
                raise InvalidInput(f"删除表达式序号非法: {chunk!r}")
        total = len(rows)
        for index in indices:
            if index < 1 or index > total:
                raise InvalidInput(f"序号超出主题列表范围: {index}")
        ids = [rows[index - 1]["topic_id"] for index in sorted(indices)]
    if not ids:
        raise InvalidInput("表达式解析结果为空主题集")
    return ids


def _delete_payload(store, conn, args) -> dict:
    """delete 预览/执行两模式（无 --yes 或 --dry-run → 预览；--yes 且无 --dry-run → 执行）。

    --yes 走显式 topic_id 列表（--dry-run 时同样先解析再预览）；预览表达式走
    `2,4~6` / `keep 1,3` 序号语义。
    """
    expr = str(getattr(args, "expr", "") or "")
    execute = bool(getattr(args, "yes", False)) and not bool(
        getattr(args, "dry_run", False)
    )
    if getattr(args, "yes", False):
        ids = [part.strip() for part in expr.split(",") if part.strip()]
        if not ids:
            raise InvalidInput("--yes 需要显式 topic_id 列表")
        for topic_id in ids:
            if not _TOPIC_ID_RE.fullmatch(topic_id):
                raise InvalidInput(f"topic_id 非法: {topic_id!r}")
            if conn.execute(
                "SELECT 1 FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone() is None:
                raise TopicNotFoundError(f"主题不存在: {topic_id}")
    else:
        keep_mode = expr.startswith("keep ")
        body = expr[len("keep ") :].strip() if keep_mode else expr
        ids = _parse_delete_expression(conn, body, keep_mode)
    plan = plan_cascade(conn, store, ids)
    missing_hops = [
        hop_id for hop_id in plan.dead_hops
        if not (store.hops_dir / hop_file_name(hop_id)).is_file()
    ]
    if not execute:
        titles = {
            row["topic_id"]: row["title"]
            for row in conn.execute("SELECT topic_id, title FROM topics")
        }
        return {
            "status": "OK",
            "topics": [
                {"topic_id": topic_id, "title": titles.get(topic_id, "")}
                for topic_id in ids
            ],
            "exclusive_hops": len(plan.dead_hops),
            "missing_hops": len(missing_hops),
        }
    apply_cascade(conn, plan)
    push_undo(
        conn, "delete", f"删除 {len(ids)} 个主题",
        {
            "pre_files": dict(plan.pre_files), "post_hashes": {},
            "created": [], "topic_ids": ids,
        },
    )
    deferred = []
    for topic_id in ids:
        md_path = store.topics_dir / f"{topic_id}.md"
        if md_path.is_file():
            deferred.append(md_path)
    for hop_id in plan.dead_hops:
        hop_path = store.hops_dir / hop_file_name(hop_id)
        if hop_path.is_file():
            deferred.append(hop_path)
    return {
        "status": "OK", "deleted": ids, "exclusive_hops": len(plan.dead_hops),
        "missing_hops": len(missing_hops), "_deferred": deferred,
    }


def _undo_child_block_check(conn, hops_to_remove: set) -> None:
    """「无新增存活子跳引用」恒强制（D-P27）：待移除跳有外部子跳 → UNDO_BLOCKED。"""
    for hop_id in hops_to_remove:
        marks = ",".join("?" * len(hops_to_remove))
        for row in conn.execute(
            f"SELECT child_hop FROM hop_edges WHERE parent_hop=?"
            + (f" AND child_hop NOT IN ({marks})" if hops_to_remove else ""),
            [hop_id, *hops_to_remove],
        ).fetchall():
            raise InvalidInput(
                {
                    "detail": "UNDO_BLOCKED：跳存在后续存活子跳引用",
                    "blocked_hop": hop_id, "child_hop": row["child_hop"],
                }
            )


def _undo_published_block_check(store, payload: dict) -> None:
    """封板台账恒强制（D8，与子跳引用同级，`--force` 不绕过）。

    台账损坏 → 一切 undo 恒拦截（含无 `new_hop` 的 delete 型栈项）；
    目标跳仍在且权威 `compute_hop_oid` 命中台账 → 恒拦截。
    """
    published_oids, ledger_corrupt = read_published_oids(store)
    if ledger_corrupt:
        raise InvalidInput("UNDO_BLOCKED: 已发布封板跳不可回滚")
    new_hop = str(payload.get("new_hop") or "")
    if not new_hop:
        return
    loaded = _load_hop(store, new_hop)
    if loaded is None:
        return
    try:
        hop_oid = compute_hop_oid(loaded.hop)
    except ValueError:
        return
    if hop_oid in published_oids:
        raise InvalidInput("UNDO_BLOCKED: 已发布封板跳不可回滚")


def _undo_post_hash_check(store, payload: dict, force: bool) -> None:
    """post_hashes 校验（--force 仅放宽此层，D-P27）。"""
    if force:
        return
    for path_text, expected_sha in payload.get("post_hashes", {}).items():
        actual = file_sha256(Path(path_text))
        if actual != expected_sha:
            raise InvalidInput(
                {
                    "detail": "UNDO_BLOCKED：磁盘文件哈希与快照不一致（--force 可放宽）",
                    "path": path_text, "expected": expected_sha, "actual": actual,
                }
            )


def _bump_restored_md_watermarks(store, per_topic_watermarks: dict, restored_paths) -> None:
    """仅本次 undo 实际恢复的 md：frontmatter next_seq 取 max(还原值, 撤前已用最大+1)。

    撤前值+1 = 该号已被撤回的保存用过——永不复用；旁观主题不受影响。
    """
    for path_text in restored_paths or []:
        md_path = Path(path_text)
        topic_id = md_path.name[:-3]
        own = per_topic_watermarks.get(topic_id)
        if not own or not md_path.is_file():
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
            doc = parse_topic_md(text)
        except (OSError, ValueError, UnsupportedSchemaVersion):
            continue
        bumped = False
        for track, value in own.items():
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if int(doc.next_seq.get(track, 0) or 0) <= value:
                doc.next_seq[track] = value + 1
                bumped = True
        if not bumped:
            continue
        new_text = re.sub(
            r"^next_seq: .*$",
            "next_seq: " + json.dumps(doc.next_seq, ensure_ascii=False, separators=(", ", ": ")),
            text, count=1, flags=re.MULTILINE,
        )
        atomic_write_text(md_path, new_text)


def _undo_payload(store, conn, args) -> dict:
    """undo：D-P19 journal → 幂等绝对还原（恢复文件 + 删除新建 + rebuild 全库重算）。"""
    entry = pop_undo(conn)
    if entry is None:
        return {"status": "OK", "note": "回滚栈为空（LIMIT 3 已尽或从未写入）"}
    # 入口拒绝：journal 残留 = 上次回滚未完成——先 doctor/手动清理，不静默覆盖
    journal_path = store.root / ".topicdock-undo-pending.json"
    if journal_path.is_file():
        raise InvalidInput(
            "检测到未完成的回滚 journal（.topicdock-undo-pending.json）——"
            "上次 undo 中断后恢复计划未完成重放；请手动删除该文件后再撤销下一步"
        )
    try:
        payload = json.loads(entry["payload_json"]) if isinstance(
            entry.get("payload_json"), str
        ) else entry.get("payload", entry)
    except ValueError:
        payload = entry.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    op_type = str(entry.get("op_type", ""))
    # 1. 校验：封板台账（恒强制，D8） + 子跳引用（恒强制，D-P27） +
    #    post_hashes（--force 可放宽）——均在 journal 写之前；
    #    拦截时栈弹出随调用方事务回滚恢复
    _undo_published_block_check(store, payload)
    hops_to_remove: set = set()
    if op_type in ("save", "merge", "amend"):
        new_hop = str(payload.get("new_hop", ""))
        if new_hop:
            # amend 不删跳只还原内容——但 head 被外部子跳引用时还原仍可能
            # 撕裂快照依赖（防御栈异常态），按恒强制口径纳入检查
            hops_to_remove = {new_hop}
    _undo_child_block_check(conn, hops_to_remove)
    _undo_post_hash_check(store, payload, force=bool(getattr(args, "force", False)))
    # 2. journal 原子写（D-P19：含各主题水位的绝对还原计划；执行前落盘）
    per_topic_watermarks: dict = {}
    for row in conn.execute(
        "SELECT topic_id, next_seq_json FROM topics"
    ).fetchall():
        try:
            watermarks = json.loads(row["next_seq_json"])
            if isinstance(watermarks, dict):
                per_topic_watermarks[row["topic_id"]] = watermarks
        except ValueError:
            continue
    atomic_write_text(
        journal_path,
        json.dumps(
            {
                "undo_id": entry.get("undo_id"), "op_type": op_type,
                "payload": payload, "watermarks": per_topic_watermarks,
            },
            ensure_ascii=False, sort_keys=True,
        ),
    )
    # pop 先提交：rebuild 的旧栈保留必须读到「已扣减」的栈——否则
    # 已弹条目被复原回新库（弹出永久失效）
    conn.commit()
    # 3. 幂等重放：恢复文件 → 删新建 → bump → rebuild 全库重算。
    #    恢复段任何异常：条目回压栈顶（新连接）+ amend 的 hop 写回 post 态
    #    （见下）——undo 安全中止，下次可重试；journal 随回压成功删除。
    #    rebuild 前关闭连接：旧连接持有 os.replace 后的脱链 inode，
    #    其后的 WAL 清理竞态可能误删新库的 -wal。
    amend_post_hop: dict = {}
    if op_type == "amend":
        for path_text in payload.get("pre_files", {}):
            path = Path(path_text)
            if (path.parent.name == "hops" or path.suffix == ".md") and (
                "hops" in path.parts or "/hops/" in str(path)
            ):
                try:
                    amend_post_hop[str(path)] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass
        if not amend_post_hop:  # md 在 topics/、hop 在 hops/——只快照 hops/ 的
            for path_text in payload.get("pre_files", {}):
                path = Path(path_text)
                if "hops" in path.parts:
                    try:
                        amend_post_hop[str(path)] = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        pass
    conn.close()
    try:
        for path_text, content in payload.get("pre_files", {}).items():
            atomic_write_text(Path(path_text), content)
        for path_text in payload.get("created", []):
            Path(path_text).unlink(missing_ok=True)
        _bump_restored_md_watermarks(
            store, per_topic_watermarks, payload.get("pre_files", {})
        )
        rebuild_db_from_markdown(store)
    except BaseException:
        # 回压用新连接：rebuild 的 os.replace 可能把原 conn 脱链（旧库 inode），
        # 写旧连接 = 条目丢失；回压成功后 journal 删除（栈重新成为权威来源）
        # amend：hop 已回旧内容而 DB 仍是 amend 后登记——⑤ 会判损坏级联删主题；
        # 先把 hop 写回 post 态使撤销安全中止（amend 保留），再回压
        if amend_post_hop:
            for path_text, post_content in amend_post_hop.items():
                try:
                    atomic_write_text(Path(path_text), post_content)
                except OSError:
                    pass
        try:
            restore_conn = get_or_create_db(store)
            try:
                push_undo(restore_conn, op_type, str(entry.get("summary", "")), payload)
                restore_conn.commit()
            finally:
                restore_conn.close()
            journal_path.unlink(missing_ok=True)
        except Exception:
            pass  # 回压失败时 journal 保留（入口拒绝路径仍有效）
        raise
    journal_path.unlink(missing_ok=True)
    restored = sorted(payload.get("pre_files", {}).keys()) or payload.get("topic_ids", [])
    return {
        "status": "OK", "restored": restored, "op_type": op_type,
        "summary": str(entry.get("summary", "")),
    }


def _published_ledger_report(store) -> dict:
    """doctor 封板台账巡检：缺失/合法 → ok；损坏 → invalid（失败关闭）。"""
    oids, is_corrupt = read_published_oids(store)
    if is_corrupt:
        return {
            "name": "published_ledger", "status": "invalid",
            "detail": "封板台账损坏（失败关闭）：拒绝原地 amend 与一切 undo",
        }
    if not oids:
        return {"name": "published_ledger", "status": "ok", "detail": "无封板记录"}
    return {
        "name": "published_ledger", "status": "ok",
        "detail": f"已封板 {len(oids)} 跳",
    }


def _hop_oid_drift_report(store, conn) -> dict:
    """doctor hop_oid 漂移巡检（非破坏性，仅 doctor 路径）。

    扫描存活主题可达跳：跳头含 `hop_oid` 且与实时重算不一致 → 列出；无该键的
    存量旧跳不算漂移；单跳重算抛 ValueError 降级为该跳的明细项，不让 doctor 崩溃。
    """
    live: set[str] = set()
    for row in conn.execute("SELECT head_hop FROM topics").fetchall():
        live |= _db_reachable(conn, row["head_hop"])
    drift: list[str] = []
    failed: list[str] = []
    for hop_id in sorted(live):
        loaded = _load_hop(store, hop_id)
        if loaded is None or "hop_oid" not in loaded.hop.header:
            continue
        cached = loaded.hop.header.get("hop_oid")
        cached = cached if isinstance(cached, str) else ""
        try:
            actual = compute_hop_oid(loaded.hop)
        except ValueError as exc:
            failed.append(f"{hop_id}（重算失败：{exc}）")
            continue
        if cached != actual:
            drift.append(f"{hop_id}（缓存 {cached or '<空>'} ≠ 实算 {actual}）")
    items = drift + failed
    return {
        "name": "hop_oid_drift", "status": "drift" if items else "ok",
        "detail": "; ".join(items) if items else "无跳头 hop_oid 漂移",
    }


def _doctor_payload(store, conn_unused, args) -> dict:
    """doctor [--rebuild]：无参 = 七步自愈巡检 + 封板台账/oid 漂移双巡检；
    --rebuild = 强制冷重建（保留既有封板台账）。"""
    if getattr(args, "rebuild", False):
        reports = rebuild_db_from_markdown(store)
        return {"status": "OK", "checks": reports}
    conn, reports = ensure_store_healthy(store)
    try:
        conn.commit()
        reports.append(_published_ledger_report(store))
        reports.append(_hop_oid_drift_report(store, conn))
    finally:
        conn.close()
    return {"status": "OK", "checks": reports}


def cmd_merge(args) -> tuple[dict, int]:
    """`td merge <src> <dst> [--summary "..."]`（唯一入口之外的 DAG 合流写入）。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _heal = ensure_store_healthy(store)
        try:
            result = _merge_payload(store, conn, args)
            deferred = result.pop("_deferred", [])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        for path in deferred:
            path.unlink(missing_ok=True)
    return result, EXIT_OK


def cmd_delete(args) -> tuple[dict, int]:
    """`td delete <expr> [--dry-run] [--yes]`（预览表达式 / --yes 仅显式 id）。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _heal = ensure_store_healthy(store)
        try:
            result = _delete_payload(store, conn, args)
            deferred = []
            if result.get("_deferred"):
                deferred = result.pop("_deferred")
            conn.commit()
            for path in deferred:
                path.unlink(missing_ok=True)
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def cmd_undo(args) -> tuple[dict, int]:
    """`td undo [--force]`：弹出栈顶（LIMIT 3），绝对还原 + 高水位不回退。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _heal = ensure_store_healthy(store)
        try:
            result = _undo_payload(store, conn, args)
            try:
                conn.commit()
            except Exception:
                pass  # _undo_payload 已在 rebuild 前关闭连接（WAL 竞态防护）
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass  # 连接可能已关（rebuild 内部自管事务）
            raise
    return result, EXIT_OK


def cmd_doctor(args) -> tuple[dict, int]:
    """`td doctor [--rebuild]`：五维自愈巡检 / 强制冷重建。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        result = _doctor_payload(store, None, args)
    return result, EXIT_OK


# ---------- 读路径命令（use / list / view / match / trace，D-P2 全命令持排他锁） ----------

_LIST_ROWS_SQL = (
    "SELECT topic_id, title, updated_at FROM topics ORDER BY updated_at DESC, topic_id ASC"
)


def _list_topic_rows(conn, filter_kw=None) -> list:
    """list 同序全量行（updated_at 倒序）；--filter 大小写不敏感子串（topic_id/title）。"""
    rows = conn.execute(_LIST_ROWS_SQL).fetchall()
    if filter_kw:
        kw = str(filter_kw).lower()
        rows = [
            row for row in rows
            if kw in str(row["topic_id"]).lower() or kw in str(row["title"]).lower()
        ]
    return rows


def _list_page(conn, *, page: int, page_size: int, filter_kw, show_all: bool) -> dict:
    """list 分页核心（编号在过滤后的全集上 1-based 全局连续，跨页连续）。"""
    rows = _list_topic_rows(conn, filter_kw)
    total = len(rows)
    if show_all:
        page, page_size = 1, total
    start = (page - 1) * page_size
    items = [
        {
            "index": start + offset + 1,
            "topic_id": row["topic_id"],
            "title": row["title"],
            "updated_at": row["updated_at"],
        }
        for offset, row in enumerate(rows[start : start + page_size])
    ]
    return {
        "status": "OK", "page": page, "page_size": page_size,
        "total": total, "items": items,
    }


def _resolve_topic_selector(conn, selector):
    """selector → topics 行：纯数字按 list 同序 1-based 编号，其余 topic_id 精确匹配。"""
    if selector is None or str(selector) == "":
        return None
    text = str(selector)
    if re.fullmatch(r"[0-9]+", text):
        rows = conn.execute(
            "SELECT topic_id, title, updated_at, head_hop FROM topics"
            " ORDER BY updated_at DESC, topic_id ASC"
        ).fetchall()
        index = int(text)
        if index < 1 or index > len(rows):
            raise TopicNotFoundError(f"序号超出主题列表范围: {index}")
        return rows[index - 1]
    row = conn.execute(
        "SELECT topic_id, title, updated_at, head_hop FROM topics WHERE topic_id=?",
        (text,),
    ).fetchone()
    if row is None:
        raise TopicNotFoundError(f"主题不存在: {text}")
    return row


def _mount_topic(store, conn, topic_id: str) -> dict:
    """use/view 命中后的挂载载荷：md 全文 + head_hop + base_sha + artifacts 三态探测。"""
    row = conn.execute(
        "SELECT md_sha256 FROM topics WHERE topic_id=?", (topic_id,)
    ).fetchone()
    md_path = store.topics_dir / f"{topic_id}.md"
    text = md_path.read_text(encoding="utf-8")
    doc = parse_topic_md(text)
    return {
        "status": "OK",
        "topic_id": topic_id,
        "text": text,
        "head_hop": str(doc.head_hop),
        "base_sha": str(row["md_sha256"]) if row is not None else "",
        "artifacts": probe_artifact_links(doc, Path.cwd()),
    }


def _render_block_text(block) -> str:
    """单块文本：`### [item] (op=...)` 标题 + 逐字段行（与 render_hop_md 同序同格式）。"""
    lines = [_render_block_title(block)]
    for name in _HOP_FIELD_ORDER:
        if name in block.fields:
            lines.extend(_render_field_lines(name, block.fields[name]))
    return "\n".join(lines)


def _block_entry_for(store, row, hop_cache=None) -> dict:
    """trace 的单块条目：加载分片 → 渲染该跳中 item_id 匹配的块；缺失 → missing 标注。"""
    entry = {"hop_id": row["hop_id"], "op": row["op"]}
    loaded = _load_hop(store, row["hop_id"], hop_cache)
    if loaded is None:
        entry["missing"] = True
        return entry
    texts = [
        _render_block_text(block)
        for block in loaded.hop.blocks if block.item_id == row["item_id"]
    ]
    if texts:
        entry["text"] = "\n\n".join(texts)
    return entry


def _trace_auto_item_ids(doc) -> list:
    """auto 追根目标：当前全部未决 `[Q]` + 状态为 `[~]` 的 `[A]`（保序去重）。"""
    item_ids = [item.item_id for item in doc.items.get("Q", [])]
    item_ids.extend(
        item.item_id for item in doc.items.get("A", []) if item.a_state == "~"
    )
    return list(dict.fromkeys(item_ids))


def _split_trace_items(spec) -> list:
    """逗号拆分的显式条目清单（去空白、保序去重）；空集 → InvalidInput。"""
    item_ids = [part.strip() for part in str(spec).split(",") if part.strip()]
    item_ids = list(dict.fromkeys(item_ids))
    if not item_ids:
        raise InvalidInput("--trace 需要 auto 或至少一个 item_id")
    return item_ids


def _validate_trace_spec(spec) -> None:
    """`auto` 或非空逗号清单；仅做语法校验（空集 → InvalidInput）。"""
    if spec is None or str(spec).strip() == "auto":
        return
    _split_trace_items(spec)


def _trace_item_ids(doc, spec) -> list:
    if str(spec).strip() == "auto":
        return _trace_auto_item_ids(doc)
    return _split_trace_items(spec)


def _trace_item_payload(store, conn, topic_id: str, item_id: str, hop_cache) -> dict:
    """单条目 trace 字典（与既有单条目 `cmd_trace_locked` 返回同构）。"""
    trace = get_item_trace(conn, topic_id, item_id)
    if not trace["blocks"] and not trace["links"]:
        raise ItemNotFoundError(f"条目不存在: {topic_id}:{item_id}")
    blocks = []
    for block_row in list(trace["blocks"]) + list(trace["ancestry"]):
        entry = _block_entry_for(store, block_row, hop_cache)
        if str(block_row.get("topic_id", topic_id)) != topic_id:
            entry["from_topic"] = str(block_row["topic_id"])  # 跨主题递归区分
        blocks.append(entry)
    # 被推翻条目的去向块：successor(overturn) 所在跳的推翻块并入 blocks 尾部
    for succ in trace["successors"]:
        if succ["link_type"] != "overturn":
            continue
        entry = {"hop_id": succ["hop_id"], "op": "overturn"}
        loaded = _load_hop(store, succ["hop_id"], hop_cache)
        if loaded is None:
            entry["missing"] = True
        else:
            texts = [
                _render_block_text(block)
                for block in loaded.hop.blocks if block.item_id == succ["item_id"]
            ]
            if texts:
                entry["text"] = "\n\n".join(texts)
        blocks.append(entry)
    successor_notes = []
    for succ in trace["successors"]:
        target = succ["item_id"]
        if succ["topic_id"] != topic_id:  # 同主题省 topic 前缀
            target = f"{succ['topic_id']}:{succ['item_id']}"
        successor_notes.append(f"于 {succ['hop_id']} {succ['link_type']} 转入 {target}")
    return {
        "status": "OK", "topic_id": topic_id, "item_id": item_id,
        "blocks": blocks, "links": trace["links"],
        "successor": trace["successors"], "successor_notes": successor_notes,
    }


def _collect_traces(store, conn, topic_id: str, item_ids, hop_cache) -> dict:
    """批量追根；显式条目缺失置 `{"missing_item": true}` 不中断主挂载。"""
    traces: dict = {}
    for item_id in item_ids:
        try:
            traces[item_id] = _trace_item_payload(
                store, conn, topic_id, item_id, hop_cache
            )
        except ItemNotFoundError:
            traces[item_id] = {"missing_item": True}
    return traces


def _attach_mount_traces(store, conn, payload: dict, args, hop_cache) -> dict:
    """挂载成功后按 `--trace <auto|items>` 附加同轮内联历史块。"""
    spec = getattr(args, "trace", None)
    if spec is None:
        return payload
    doc = parse_topic_md(payload["text"])
    payload["traces"] = _collect_traces(
        store, conn, payload["topic_id"], _trace_item_ids(doc, spec), hop_cache
    )
    return payload



def _history_entry(header: dict, hop_id: str) -> dict:
    """view --history 单条跳摘要：可选 model 仅非空 str 透传，非 str 容错为缺失。"""
    entry = {
        "hop_id": hop_id,
        "created_at": str(header.get("created_at", "")),
        "actor": str(header.get("actor", "")),
        "summary": str(header.get("summary", "")),
    }
    model = header.get("model")
    if isinstance(model, str) and model:
        entry["model"] = model
    return entry


def _own_chain_history(store, head_hop: str, topic_id: str) -> list:
    """断链时的可达部分 + 中断标注（head 最前；完整链走 cmd_view_locked 的 walk 分支）。"""
    entries: list = []
    seen: set = set()
    cur: str | None = str(head_hop)
    while cur is not None:
        if cur in seen:
            entries.append({"hop_id": cur, "missing": True, "note": "链中断"})
            return entries
        seen.add(cur)
        loaded = _load_hop(store, cur)
        if loaded is None:
            entries.append({"hop_id": cur, "missing": True, "note": "链中断"})
            return entries
        if loaded.hop.header.get("topic_origin") != topic_id:
            return entries  # fork 边界：链完整（与 _walk_origin_chain 同口径）
        header = loaded.hop.header
        entries.append(_history_entry(header, cur))
        parents = [str(p) for p in (header.get("parents") or [])]
        cur = parents[0] if parents else None
    return entries


def _read_command_common(args):
    """读命令通用开头（照抄 cmd_save 模式）；返回 (store, conn) 或 UNINITIALIZED 结果。"""
    start_dir = Path(args.workspace) if getattr(args, "workspace", None) else None
    store = resolve_store(start_dir, getattr(args, "store_dir", None))
    if store is None or not _is_initialized_store(store.root):
        return None, (
            {
                "status": "UNINITIALIZED_STORE", "default_global": "~/.topicdock",
                "workspace_local": str(Path.cwd() / ".topicdock"),
            },
            EXIT_UNINITIALIZED_STORE,
        )
    return store, None


def cmd_use(args) -> tuple[dict, int]:
    """`td use [<topic_id|index>]` —— 挂载主题（无 selector 时列出候选）。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _reports = ensure_store_healthy(store)
        try:
            result = cmd_use_locked(store, conn, args)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def cmd_use_locked(store, conn, args) -> dict:
    selector = getattr(args, "topic", None)
    if selector is None or str(selector) == "":
        rows = _list_topic_rows(conn)
        if not rows:
            return {
                "status": "OK", "topic_id": None,
                "note": "store 尚无主题：先调 td init / 首次 save 建档",
            }
        if len(rows) == 1:
            payload = _mount_topic(store, conn, rows[0]["topic_id"])
            return _attach_mount_traces(store, conn, payload, args, {})
        listing = _list_page(conn, page=1, page_size=10, filter_kw=None, show_all=False)
        listing["hint"] = "回复序号挂载对应主题；或用 --page 翻页 / --filter 过滤后再挂载"
        return listing
    row = _resolve_topic_selector(conn, selector)
    payload = _mount_topic(store, conn, row["topic_id"])
    return _attach_mount_traces(store, conn, payload, args, {})


def cmd_list(args) -> tuple[dict, int]:
    """`td list [--page P] [--page-size N] [--all] [--filter <kw>]`。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _reports = ensure_store_healthy(store)
        try:
            result = cmd_list_locked(store, conn, args)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def cmd_list_locked(store, conn, args) -> dict:
    return _list_page(
        conn,
        page=getattr(args, "page", None) or 1,
        page_size=getattr(args, "page_size", None) or 10,
        filter_kw=getattr(args, "filter", None),
        show_all=bool(getattr(args, "all", False)),
    )


def cmd_view(args) -> tuple[dict, int]:
    """`td view <topic_id|index> [--history]`。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _reports = ensure_store_healthy(store)
        try:
            result = cmd_view_locked(store, conn, args)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def cmd_view_locked(store, conn, args) -> dict:
    row = _resolve_topic_selector(conn, getattr(args, "topic", None))
    if row is None:
        raise TopicNotFoundError("view 需要主题 selector")
    topic_id = row["topic_id"]
    if not getattr(args, "history", False):
        md_path = store.topics_dir / f"{topic_id}.md"
        return {
            "status": "OK", "topic_id": topic_id,
            "text": md_path.read_text(encoding="utf-8"),
        }
    chain, _missing, _invalid = _walk_origin_chain(store, row["head_hop"], topic_id)
    if chain is not None:
        history = [
            _history_entry(
                loaded.hop.header,
                str(loaded.hop.header.get("hop_id", "")),
            )
            for loaded in reversed(chain)  # 逆拓扑（时间倒序）：head 在最前
        ]
    else:
        history = _own_chain_history(store, row["head_hop"], topic_id)
    return {"status": "OK", "topic_id": topic_id, "history": history}


def cmd_match(args) -> tuple[dict, int]:
    """`td match --topic <kw> [--topic <kw> ...] [--threshold 35] [--recent]`。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _reports = ensure_store_healthy(store)
        try:
            result = cmd_match_locked(store, conn, args)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def cmd_match_locked(store, conn, args) -> dict:
    queries = getattr(args, "topic", None) or []
    recent = bool(getattr(args, "recent", False))
    if not queries and not recent:
        raise InvalidInput("match 至少需要一个 --topic 查询词，或使用 --recent")
    threshold = getattr(args, "threshold", None)
    threshold = 35.0 if threshold is None else float(threshold)
    trace_spec = getattr(args, "trace", None)
    if trace_spec is not None:
        _validate_trace_spec(trace_spec)
    results = match_topics(
        conn, [str(query) for query in queries], threshold=threshold,
        recent=recent,
    )
    hop_cache: dict = {}
    for offset, entry in enumerate(results):
        if offset < 5:  # 前 ≤5 附完整挂载凭证（D-P16/D9 同口径）
            entry["full"] = True
            text = (
                store.topics_dir / f"{entry['topic_id']}.md"
            ).read_text(encoding="utf-8")
            entry["text"] = text
            doc = parse_topic_md(text)
            entry["head_hop"] = str(doc.head_hop)
            sha_row = conn.execute(
                "SELECT md_sha256 FROM topics WHERE topic_id=?",
                (entry["topic_id"],),
            ).fetchone()
            entry["base_sha"] = str(sha_row["md_sha256"]) if sha_row is not None else ""
            entry["artifacts"] = probe_artifact_links(doc, Path.cwd())
            if trace_spec is not None:
                entry["traces"] = _collect_traces(
                    store, conn, entry["topic_id"],
                    _trace_item_ids(doc, trace_spec), hop_cache,
                )
        else:
            entry["full"] = False
    return {"status": "OK", "results": results}


def cmd_trace(args) -> tuple[dict, int]:
    """`td trace <topic_id> <item_id>`。"""
    store, early = _read_command_common(args)
    if early is not None:
        return early
    with store.exclusive_lock():
        conn, _reports = ensure_store_healthy(store)
        try:
            result = cmd_trace_locked(store, conn, args)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result, EXIT_OK


def _trace_items_from_args(args) -> list:
    """trace 位置参数：兼容新 `items`（列表）与旧 `item`（单字符串），逐项逗号拆分。"""
    raw = getattr(args, "items", None)
    if raw is None:
        raw = getattr(args, "item", "")
    chunks = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    item_ids: list = []
    for chunk in chunks:
        for part in str(chunk).split(","):
            part = part.strip()
            if part:
                item_ids.append(part)
    item_ids = list(dict.fromkeys(item_ids))
    if not item_ids:
        raise InvalidInput("trace 需要至少一个 item_id")
    return item_ids


def cmd_trace_locked(store, conn, args) -> dict:
    topic_id = str(getattr(args, "topic", ""))
    item_ids = _trace_items_from_args(args)
    row = conn.execute(
        "SELECT topic_id FROM topics WHERE topic_id=?", (topic_id,)
    ).fetchone()
    if row is None:
        raise TopicNotFoundError(f"主题不存在: {topic_id}")
    hop_cache: dict = {}
    if len(item_ids) == 1:
        return _trace_item_payload(store, conn, topic_id, item_ids[0], hop_cache)
    items: dict = {}
    for item_id in item_ids:
        items[item_id] = _trace_item_payload(store, conn, topic_id, item_id, hop_cache)
    return {
        "status": "OK", "topic_id": topic_id,
        "item_ids": item_ids, "items": items,
    }


