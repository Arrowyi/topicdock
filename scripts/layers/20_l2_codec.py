# ===================== L2: MarkdownCodec =====================

SUPPORTED_SCHEMA_VERSION = 1

_TRACKS = ("C", "R", "Q", "A")

_HOP_HEADER_PREFIX = "<!-- topicdock-hop: "
_HOP_HEADER_SUFFIX = " -->"
_HOP_HEADER_REQUIRED = (
    "v", "hop_id", "topic_origin", "title", "parents",
    "session_id", "created_at", "actor", "summary", "next_focus", "artifacts",
)

_ITEM_SEPARATORS = (
    " — 依据(verified): ",
    " — 依据(inferred): ",
    " — 原因: ",
    " — 断点: ",
)

_HOP_FIELD_ORDER = (
    "结论正文", "一句话依据", "一句话原因", "推导与权衡",
    "核心证据 (verified)", "核心证据 (inferred)", "重审条件", "断点",
)

_CQ_HEADINGS = (
    ("C", "### 核心背景与已拍板结论 (Confirmed `[C]`)"),
    ("R", "### 已排除方案与被推翻结论 (Rejected & Overturned `[R]`)"),
    ("Q", "### 待继续讨论与未决问题 (Open Questions `[Q]`)"),
)

_A_LINE_RE = re.compile(r"^- \[([x~ ])\] \[([A-Za-z0-9-]+):([CRQA]\d+)\] (.*)$")
_PLAIN_LINE_RE = re.compile(r"^- \[([A-Za-z0-9-]+):([CRQA]\d+)\] (.*)$")
_TRAILING_TAG_RE = re.compile(r"^(.*) \(([^()]*)\)$", re.DOTALL)
_HOP_BLOCK_RE = re.compile(r"^### \[([A-Za-z0-9_]+)\] \((.*)\)$")
_LATEST_HEAD_RE = re.compile(
    r"^- \*\*(\S+)\*\* \(`([^`]*)` \| actor: `([^`]*)`"
    r" \| parents: `([^`]*)` \| sha: `([^`]*)`\)$"
)


class UnsupportedSchemaVersion(Exception):
    """账本/hop 的 schema_version 高于本引擎支持的版本。"""


# ---------- 数据结构 ----------

@dataclass
class TopicItem:
    item_id: str                       # 'C1'/'R1'/'Q1'/'A2'（不含 topic 前缀）
    track: str                         # 'C'|'R'|'Q'|'A'
    statement: str = ""                # 单行化正文
    a_state: str | None = None         # 'x'|'~'|' '，仅 A 轨
    evidence_status: str | None = None  # 'verified'|'inferred'
    basis: str | None = None           # 依据/原因/断点正文
    rationale: str = ""                # 推导与权衡（hop 块富字段）
    evidence_detail: str = ""          # 核心证据（hop 块富字段）
    review_cond: str = ""              # 重审条件（hop 块富字段）
    trace_records: list[str] = field(default_factory=list)


@dataclass
class LatestHop:
    hop_id: str = ""
    updated_at: str = ""
    actor: str = ""
    parents: list[str] = field(default_factory=list)
    sha12: str = ""
    summary: str = ""
    next_focus: str = ""


@dataclass
class TopicDoc:
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    topic_id: str = ""
    title: str = ""
    head_hop: str = ""
    updated_at: str = ""
    last_actor: str = ""
    next_seq: dict[str, int] = field(default_factory=dict)
    items: dict[str, list[TopicItem]] = field(
        default_factory=lambda: {track: [] for track in _TRACKS}
    )
    artifacts: list[tuple[str, str, str | None]] = field(default_factory=list)
    latest: LatestHop | None = None


@dataclass
class HopBlock:
    item_id: str
    op: str                            # create|update|overturn|resolve|fold|inherit|retract
    status: str | None = None          # 'verified'|'inferred'
    a_state: str | None = None         # 'x'|'~'|' '
    from_items: list[str] = field(default_factory=list)
    origin: str | None = None          # '<topic>:<item>@<hop>'
    fields: dict[str, str] = field(default_factory=dict)  # 操作后完整条目快照


@dataclass
class HopDoc:
    header: dict = field(default_factory=dict)
    blocks: list[HopBlock] = field(default_factory=list)


@dataclass
class ApplyContext:
    hop_id: str = ""
    new_seq: dict[str, int] = field(default_factory=dict)


# ---------- 条目文本转义 ----------

def _flatten_item_text(text: str) -> str:
    """条目文本压平为单行：内部换行替换为空格（design §4.2(1)）。"""
    return str(text).replace("\n", " ").replace("\r", " ")


def _escape_item_text(text: str) -> str:
    """转义字面量分隔符与行尾 (…) 组，保证解析无歧义（design §4.2(1)）。"""
    for sep in _ITEM_SEPARATORS:
        if sep in text:
            text = text.replace(sep, sep.replace("—", "&#8212;"))
    match = _TRAILING_TAG_RE.match(text)
    if match:
        text = match.group(1) + " &#40;" + match.group(2) + ")"
    return text


def _unescape_item_text(text: str) -> str:
    return text.replace("&#8212;", "—").replace("&#40;", "(")


def _split_item_body(body: str) -> tuple[str, str | None, str | None, str | None]:
    """拆解条目行剩余部分 → (statement, evidence_status, basis, trace_tag)。"""
    tag = None
    match = _TRAILING_TAG_RE.match(body)
    if match and match.group(2):
        tag = match.group(2)
        body = match.group(1)
    statement, status, basis = body, None, None
    best_index, best_sep = -1, None
    for sep in _ITEM_SEPARATORS:
        index = body.rfind(sep)
        if index > best_index:
            best_index, best_sep = index, sep
    if best_sep is not None:
        statement = body[:best_index]
        basis = body[best_index + len(best_sep):]
        if best_sep.startswith(" — 依据("):
            status = "verified" if "verified" in best_sep else "inferred"
    return (
        _unescape_item_text(statement),
        status,
        _unescape_item_text(basis) if basis is not None else None,
        tag,
    )


def _tag_to_records(tag: str | None) -> list[str]:
    """简单跳号短标 (H001→H002) 还原为 records；折叠/R 形态返回 []。"""
    if not tag:
        return []
    parts = tag.split("→")
    if parts and all(re.fullmatch(r"H\d+", part) for part in parts):
        return parts
    return []


# ---------- 行尾短标（D-P4/D-P30） ----------

def _tag_fits(tag: str) -> bool:
    return len(tag) <= 20 and len(tag.encode("utf-8")) <= 32


def _truncate_tag(tag: str) -> str:
    return tag[:18] + ")" if len(tag) > 20 else tag


def format_trace_tag(
    records: list[str],
    origin_hop: str | None = None,
    overturn_hop: str | None = None,
) -> str:
    """生成 ≤20 字符 / UTF-8 ≤32B 的行尾短标，全形态带确定性降级链。"""
    if origin_hop is not None and overturn_hop is not None:
        tag = f"(原立{origin_hop}→推翻{overturn_hop})"
        if _tag_fits(tag):
            return tag
        tag = f"(推翻{overturn_hop})"
        if _tag_fits(tag):
            return tag
        return _truncate_tag(tag)
    hops = [record for record in records if record]
    if not hops:
        return ""
    first, last, count = hops[0], hops[-1], len(hops)
    if count == 1:
        tag = f"({first})"
    elif count <= 3:
        tag = "(" + "→".join(hops) + ")"
    else:
        tag = f"({first}→..→{last}, {count}跳)"
    if _tag_fits(tag):
        return tag
    if count > 3:
        tag = f"({first}..+{count}跳)"
        if _tag_fits(tag):
            return tag
    tag = f"({first})"
    if _tag_fits(tag):
        return tag
    return _truncate_tag(tag)


# ---------- topics/<id>.md ----------

def _render_item_line(doc: TopicDoc, item: TopicItem, tag: str | None) -> str:
    # 压平为单行（design §4.2(1)）：statement/basis 内部换行替换为空格
    body = _escape_item_text(_flatten_item_text(item.statement))
    if item.track == "C" and item.evidence_status and item.basis:
        body += f" — 依据({item.evidence_status}): {_escape_item_text(_flatten_item_text(item.basis))}"
    elif item.track == "R" and item.basis:
        body += f" — 原因: {_escape_item_text(_flatten_item_text(item.basis))}"
    elif item.track == "A" and item.basis:
        body += f" — 断点: {_escape_item_text(_flatten_item_text(item.basis))}"
    if tag:
        body += f" {tag}"
    prefix = f"[{doc.topic_id}:{item.item_id}]"
    if item.track == "A":
        state = item.a_state if item.a_state is not None else " "
        return f"- [{state}] {prefix} {body}"
    return f"- {prefix} {body}"


def _parse_item_line(line: str, is_action: bool) -> TopicItem | None:
    topic_id, item_id, body, a_state = None, None, None, None
    if is_action:
        match = _A_LINE_RE.match(line)
        if match is None:
            return None
        a_state, topic_id, item_id, body = match.groups()
    else:
        match = _PLAIN_LINE_RE.match(line)
        if match is None:
            return None
        topic_id, item_id, body = match.groups()
    statement, status, basis, tag = _split_item_body(body)
    track = item_id[:1]
    return TopicItem(
        item_id=item_id,
        track=track,
        statement=statement,
        a_state=a_state if track == "A" else None,
        evidence_status=status,
        basis=basis,
        trace_records=_tag_to_records(tag),
    )


def parse_topic_md(text: str) -> TopicDoc:
    """解析 topics/<id>.md；schema_version > 1 抛 UnsupportedSchemaVersion。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("topics md 必须以 --- frontmatter 开始")
    frontmatter_end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter_end = index
            break
    if frontmatter_end is None:
        raise ValueError("frontmatter 未闭合")
    raw: dict[str, str] = {}
    for line in lines[1:frontmatter_end]:
        if not line.strip():
            continue
        key, _, value = line.partition(": ")
        raw[key.strip()] = value.strip()

    try:
        schema_version = int(raw.get("schema_version", str(SUPPORTED_SCHEMA_VERSION)))
    except ValueError:
        schema_version = SUPPORTED_SCHEMA_VERSION
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"schema_version {schema_version} > {SUPPORTED_SCHEMA_VERSION}"
        )

    title_raw = raw.get("title", "")
    title = title_raw
    if title_raw.startswith('"'):
        try:
            title = json.loads(title_raw)
        except ValueError:
            title = title_raw

    try:
        next_seq = json.loads(raw.get("next_seq", "{}"))
    except ValueError:
        next_seq = {}
    if not isinstance(next_seq, dict):
        next_seq = {}

    doc = TopicDoc(
        schema_version=schema_version,
        topic_id=raw.get("topic_id", ""),
        title=title,
        head_hop=raw.get("head_hop", ""),
        updated_at=raw.get("updated_at", ""),
        last_actor=raw.get("last_actor", ""),
        next_seq=next_seq,
    )

    section = None
    subtrack = None
    for line in lines[frontmatter_end + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## 1."):
            section, subtrack = "crq", None
            continue
        if stripped.startswith("## 2."):
            section, subtrack = "A", None
            continue
        if stripped.startswith("## 3."):
            section, subtrack = "artifacts", None
            continue
        if stripped.startswith("## 4."):
            section, subtrack = "latest", None
            continue
        if stripped.startswith("### ") and section == "crq":
            for track, heading in _CQ_HEADINGS:
                if stripped == heading or stripped.startswith(heading.rstrip("`]") + "`]"):
                    subtrack = track
                    break
            else:
                subtrack = None
            continue
        if not stripped.startswith("- "):
            continue
        content = stripped[2:]
        if section == "crq" and subtrack:
            item = _parse_item_line(stripped, is_action=False)
            if item is not None:
                doc.items[subtrack].append(item)
        elif section == "A":
            item = _parse_item_line(stripped, is_action=True)
            if item is not None:
                doc.items["A"].append(item)
        elif section == "artifacts":
            name, _, rest = content.partition(": ")
            if not name or not rest:
                continue
            path, sep, note = rest.partition(" — ")
            doc.artifacts.append((name, path, note if sep else None))
        elif section == "latest":
            head = _LATEST_HEAD_RE.match(stripped)
            if head:
                hop_id, updated_at, actor, parents_raw, sha12 = head.groups()
                doc.latest = LatestHop(
                    hop_id=hop_id,
                    updated_at=updated_at,
                    actor=actor,
                    parents=[p for p in parents_raw.split("+") if p],
                    sha12=sha12,
                )
            elif content.startswith("本跳摘要: "):
                if doc.latest is not None:
                    doc.latest.summary = content[len("本跳摘要: "):]
            elif content.startswith("待续焦点 (Next Focus): "):
                if doc.latest is not None:
                    doc.latest.next_focus = content[len("待续焦点 (Next Focus): "):]
    return doc


def render_topic_md(doc: TopicDoc, trace_tags: dict[str, str]) -> str:
    out = [
        "---",
        f"schema_version: {doc.schema_version}",
        f"topic_id: {doc.topic_id}",
        f"title: {json.dumps(doc.title, ensure_ascii=False)}",
        f"head_hop: {doc.head_hop}",
        f"updated_at: {doc.updated_at}",
        f"last_actor: {doc.last_actor}",
        "next_seq: " + json.dumps(doc.next_seq, ensure_ascii=False, separators=(", ", ": ")),
        "---",
        "",
        "## 1. 认知与定论 (Knowledge & Conclusions)",
        "",
    ]
    for track, heading in _CQ_HEADINGS:
        out.append(heading)
        for item in doc.items.get(track, []):
            out.append(_render_item_line(doc, item, trace_tags.get(item.item_id)))
        out.append("")
    out.append("## 2. 行动与进展 (Session Actions `[A]`)")
    for item in doc.items.get("A", []):
        out.append(_render_item_line(doc, item, trace_tags.get(item.item_id)))
    out.append("")
    out.append("## 3. 关联文件链接")
    for name, path, note in doc.artifacts:
        out.append(f"- {name}: {path} — {note}" if note else f"- {name}: {path}")
    out.append("")
    out.append("## 4. 最新一跳 (Latest Hop)")
    if doc.latest is not None:
        latest = doc.latest
        out.append(
            f"- **{latest.hop_id}** (`{latest.updated_at}` | actor: `{latest.actor}`"
            f" | parents: `{'+'.join(latest.parents)}` | sha: `{latest.sha12}`)"
        )
        out.append(f"- 本跳摘要: {latest.summary}")
        out.append(f"- 待续焦点 (Next Focus): {latest.next_focus}")
    out.append("")
    return "\n".join(out)


# ---------- hops/hop-NNN.md ----------

def _canonical_hop_json(header: dict) -> str:
    for key in _HOP_HEADER_REQUIRED:
        if key not in header:
            raise ValueError(f"hop 头缺少必填字段: {key}")
    text = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = text.replace("--", "-\\u002d").replace("<", "\\u003c").replace(">", "\\u003e")
    return text


def _render_block_title(block: HopBlock) -> str:
    attrs = [f"op={block.op}"]
    if block.status:
        attrs.append(f"status={block.status}")
    if block.a_state is not None:
        attrs.append(f"state={'space' if block.a_state == ' ' else block.a_state}")
    if block.from_items:
        attrs.append(f"from={'+'.join(block.from_items)}")
    if block.origin:
        attrs.append(f"origin={block.origin}")
    return f"### [{block.item_id}] ({', '.join(attrs)})"


def _render_field_lines(name: str, value: str) -> list[str]:
    raw_lines = str(value).split("\n")
    out = [f"- {name}: {raw_lines[0]}"]
    for line in raw_lines[1:]:
        if line.startswith("### ["):
            line = "\\### [" + line[len("### ["):]
        out.append("  " + line)
    return out


def render_hop_md(hop: HopDoc) -> str:
    header = hop.header
    out = [
        f"{_HOP_HEADER_PREFIX}{_canonical_hop_json(header)}{_HOP_HEADER_SUFFIX}",
        "",
        f"# Hop {header['hop_id']} — {header['topic_origin']}",
    ]
    for block in hop.blocks:
        out.append("")
        out.append(_render_block_title(block))
        for name in _HOP_FIELD_ORDER:
            if name in block.fields:
                out.extend(_render_field_lines(name, block.fields[name]))
    return "\n".join(out) + "\n"


def _parse_block_title(line: str) -> HopBlock:
    match = _HOP_BLOCK_RE.match(line)
    if match is None:
        raise ValueError(f"无法解析条目块标题: {line}")
    item_id, attrs_raw = match.groups()
    attrs: dict[str, str] = {}
    for chunk in attrs_raw.split(", "):
        key, _, value = chunk.partition("=")
        attrs[key.strip()] = value.strip()
    if "op" not in attrs:
        raise ValueError(f"条目块缺少 op: {line}")
    state = attrs.get("state")
    if state == "space":
        state = " "
    return HopBlock(
        item_id=item_id,
        op=attrs["op"],
        status=attrs.get("status"),
        a_state=state,
        from_items=[p for p in attrs.get("from", "").split("+") if p],
        origin=attrs.get("origin"),
    )


def parse_hop_md(text: str) -> HopDoc:
    """解析 hops/hop-NNN.md（含零块 hop）；首行必须是转义 JSON 注释头。"""
    lines = text.split("\n")
    if not lines or not lines[0].startswith(_HOP_HEADER_PREFIX) or not lines[0].endswith(_HOP_HEADER_SUFFIX):
        raise ValueError("hop 首行必须是 <!-- topicdock-hop: ... --> JSON 注释头")
    payload = lines[0][len(_HOP_HEADER_PREFIX):-len(_HOP_HEADER_SUFFIX)]
    header = json.loads(payload)
    if not isinstance(header, dict):
        raise ValueError("hop 头必须是 JSON 对象")

    hop = HopDoc(header=header)
    current: HopBlock | None = None
    field_name: str | None = None
    field_lines: list[str] = []

    def _flush_field() -> None:
        nonlocal field_name, field_lines
        if current is not None and field_name is not None:
            current.fields[field_name] = "\n".join(field_lines)
        field_name, field_lines = None, []

    for line in lines[1:]:
        if line == "":
            continue
        if line.startswith("### ["):
            _flush_field()
            current = _parse_block_title(line)
            hop.blocks.append(current)
            continue
        if line.startswith("- ") and current is not None:
            _flush_field()
            name, _, value = line[2:].partition(": ")
            field_name, field_lines = name, [value]
            continue
        if field_name is not None and line.startswith("  "):
            content = line[2:]
            if content.startswith("\\### ["):
                content = "### [" + content[len("\\### ["):]
            field_lines.append(content)
            continue
        # 其余行（# Hop 标题等）忽略
    _flush_field()
    return hop


# ---------- 位置无关 Merkle 身份（D8） ----------

def _parse_origin(origin: str | None) -> tuple[str, str, str] | None:
    """解析 origin 属性 `<topic>:<item>@<hop>`；非法/缺失 → None。"""
    if not origin:
        return None
    match = re.fullmatch(r"([A-Za-z0-9-]+):([CRQA]\d+)@(H\d+)", str(origin))
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def compute_hop_oid(hop: HopDoc) -> str:
    """位置无关 Merkle 内容哈希（design §4.2(3)）：固定 13 键 + 域分隔 SHA-256。

    局部路由槽位（hop_id / parents / topic_origin / block.origin / hop_oid）不入键，
    跨库重编号或撞名改名后恒等；先经 render/parse 归一化以保证与存量旧跳同口径。
    """
    norm_hop = parse_hop_md(render_hop_md(hop))
    header = norm_hop.header
    try:
        version = int(header.get("v", 1))
    except (TypeError, ValueError):
        version = 1
    topic_uid = header.get("topic_uid")
    if not isinstance(topic_uid, str):
        topic_uid = ""
    parent_oids_raw = header.get("parent_oids")
    parent_oids = (
        [str(x) for x in parent_oids_raw]
        if isinstance(parent_oids_raw, list) else []
    )
    model = header.get("model")
    model = model.strip() if isinstance(model, str) else ""
    artifacts = [
        {
            "name": str(artifact.get("name", "")),
            "path": str(artifact.get("path", "")),
            "note": str(artifact.get("note", "") or ""),
        }
        for artifact in (header.get("artifacts") or [])
        if isinstance(artifact, dict)
    ]
    origin_oids_raw = header.get("origin_oids")
    if isinstance(origin_oids_raw, dict):
        origin_oids = {
            key: value
            for key, value in sorted(
                (str(k), str(v)) for k, v in origin_oids_raw.items()
            )
        }
    else:
        origin_oids = {}

    blocks = []
    for block in norm_hop.blocks:
        if block.a_state is None:
            a_state = None
        elif block.a_state in (" ", "space"):
            a_state = " "
        else:
            a_state = str(block.a_state)
        parsed_origin = _parse_origin(block.origin)
        blocks.append({
            "item_id": str(block.item_id),
            "op": str(block.op),
            "status": str(block.status) if block.status else None,
            "a_state": a_state,
            "from_items": [str(x) for x in (block.from_items or [])],
            "origin_item": parsed_origin[1] if parsed_origin else "",
            "fields": {
                name: str(block.fields[name])
                for name in _HOP_FIELD_ORDER if name in block.fields
            },
        })

    payload = {
        "v": version,
        "topic_uid": topic_uid,
        "title": str(header.get("title", "")),
        "parent_oids": parent_oids,
        "session_id": str(header.get("session_id", "")),
        "created_at": str(header.get("created_at", "")),
        "actor": str(header.get("actor", "")),
        "model": model,
        "summary": str(header.get("summary", "")),
        "next_focus": str(header.get("next_focus", "")),
        "artifacts": artifacts,
        "origin_oids": origin_oids,
        "blocks": blocks,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(b"topicdock-hop-oid:v1\0" + encoded).hexdigest()
    return "v1:" + digest


# ---------- 共享重放内核（D-P22） ----------

def _track_of(item_id: str) -> str:
    return item_id[:1].upper()


def _find_item(doc: TopicDoc, item_id: str) -> TopicItem | None:
    for item in doc.items.get(_track_of(item_id), []):
        if item.item_id == item_id:
            return item
    return None


def _require_item(doc: TopicDoc, item_id: str, track: str | None = None) -> TopicItem:
    item = _find_item(doc, item_id)
    if item is None:
        raise ValueError(f"条目不存在: {item_id}")
    if track is not None and item.track != track:
        raise ValueError(f"条目 {item_id} 不在 {track} 轨（实际 {item.track}）")
    return item


def _note_seq(ctx: ApplyContext, item_id: str) -> None:
    track = _track_of(item_id)
    digits = item_id[1:]
    if track in _TRACKS and digits.isdigit():
        ctx.new_seq[track] = max(ctx.new_seq.get(track, 0), int(digits))


def _basis_from_fields(fields: dict[str, str]) -> str | None:
    for key in ("一句话依据", "一句话原因", "断点"):
        if fields.get(key):
            return fields[key]
    return None


def _evidence_from_fields(fields: dict[str, str], status: str | None) -> str:
    if status == "verified" and fields.get("核心证据 (verified)"):
        return fields["核心证据 (verified)"]
    if status == "inferred" and fields.get("核心证据 (inferred)"):
        return fields["核心证据 (inferred)"]
    return fields.get("核心证据 (verified)") or fields.get("核心证据 (inferred)") or ""


def _item_from_block(block: HopBlock) -> TopicItem:
    track = _track_of(block.item_id)
    return TopicItem(
        item_id=block.item_id,
        track=track,
        statement=block.fields.get("结论正文", ""),
        a_state=block.a_state if track == "A" else None,
        evidence_status=block.status,
        basis=_basis_from_fields(block.fields),
        rationale=block.fields.get("推导与权衡", ""),
        evidence_detail=_evidence_from_fields(block.fields, block.status),
        review_cond=block.fields.get("重审条件", ""),
    )


def _apply_fields_to_item(item: TopicItem, block: HopBlock) -> None:
    fields = block.fields
    if fields.get("结论正文"):
        item.statement = fields["结论正文"]
    basis = _basis_from_fields(fields)
    if basis is not None:
        item.basis = basis
    if fields.get("推导与权衡"):
        item.rationale = fields["推导与权衡"]
    detail = _evidence_from_fields(fields, block.status)
    if detail:
        item.evidence_detail = detail
    if fields.get("重审条件"):
        item.review_cond = fields["重审条件"]
    if block.status is not None:
        item.evidence_status = block.status
    if block.a_state is not None and item.track == "A":
        item.a_state = block.a_state


def apply_hop_block(doc: TopicDoc, block: HopBlock, ctx: ApplyContext) -> None:
    """条目级唯一状态转移内核（D-P22）：7 种 op 对 TopicDoc.items 的转移。"""
    op = block.op
    if op in ("create", "inherit"):
        if _find_item(doc, block.item_id) is not None:
            raise ValueError(f"条目已存在: {block.item_id}")
        item = _item_from_block(block)
        item.trace_records.append(ctx.hop_id)
        doc.items.setdefault(item.track, []).append(item)
        _note_seq(ctx, block.item_id)
    elif op == "update":
        item = _require_item(doc, block.item_id)
        _apply_fields_to_item(item, block)
        item.trace_records.append(ctx.hop_id)
    elif op == "overturn":
        if not block.from_items:
            raise ValueError("overturn 块缺少 from")
        _require_item(doc, block.from_items[0], track="C")
        doc.items["C"] = [i for i in doc.items["C"] if i.item_id != block.from_items[0]]
        item = _item_from_block(block)
        item.trace_records.append(ctx.hop_id)
        doc.items["R"].append(item)
        _note_seq(ctx, block.item_id)
    elif op == "resolve":
        if not block.from_items:
            raise ValueError("resolve 块缺少 from")
        source = block.from_items[0]
        _require_item(doc, source, track="Q")
        doc.items["Q"] = [i for i in doc.items["Q"] if i.item_id != source]
        if block.item_id != source:
            # resolve 的块同时新建结果条目（design §4.3(1) 状态转移矩阵），
            # 使冷重建可从分片无损重放 item_links 所需语义。
            item = _item_from_block(block)
            if item.track not in _TRACKS:
                raise ValueError(f"resolve 结果条目轨道非法: {block.item_id}")
            item.trace_records.append(ctx.hop_id)
            doc.items[item.track].append(item)
            _note_seq(ctx, block.item_id)
    elif op == "fold":
        if not block.from_items:
            raise ValueError("fold 块缺少 from")
        for from_id in block.from_items:
            _require_item(doc, from_id, track="A")
        removed = {i for i in block.from_items}
        doc.items["A"] = [i for i in doc.items["A"] if i.item_id not in removed]
        item = _item_from_block(block)
        item.a_state = "x"
        item.trace_records.append(ctx.hop_id)
        doc.items["A"].append(item)
        _note_seq(ctx, block.item_id)
    elif op == "retract":
        if _find_item(doc, block.item_id) is None:
            raise ValueError(f"条目不存在: {block.item_id}")
        track = _track_of(block.item_id)
        doc.items[track] = [i for i in doc.items.get(track, []) if i.item_id != block.item_id]
    else:
        raise ValueError(f"未知 op: {op}")


def apply_hop(doc: TopicDoc, hop_header: dict, blocks: list[HopBlock]) -> None:
    """提交级唯一内核（D-P22）：逐块转移 + 头部级字段设置，正反两路共用。"""
    ctx = ApplyContext(hop_id=str(hop_header.get("hop_id", "")), new_seq=dict(doc.next_seq))
    for block in blocks:
        apply_hop_block(doc, block, ctx)
    doc.next_seq = dict(ctx.new_seq)
    doc.title = str(hop_header.get("title", doc.title))
    doc.artifacts = [
        (str(a.get("name", "")), str(a.get("path", "")), a.get("note") or "")
        for a in hop_header.get("artifacts", [])
    ]
    doc.head_hop = str(hop_header.get("hop_id", doc.head_hop))
    doc.updated_at = str(hop_header.get("created_at", doc.updated_at))
    doc.last_actor = str(hop_header.get("actor", doc.last_actor))
    doc.latest = LatestHop(
        hop_id=str(hop_header.get("hop_id", "")),
        updated_at=str(hop_header.get("created_at", "")),
        actor=str(hop_header.get("actor", "")),
        parents=[str(p) for p in hop_header.get("parents", [])],
        sha12=str(hop_header.get("sha12", "")),
        summary=str(hop_header.get("summary", "")),
        next_focus=str(hop_header.get("next_focus", "")),
    )


