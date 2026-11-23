# ===================== L6: CLI =====================
"""L6: CLI 适配层 —— argparse 装配、输出渲染与退出码。"""

# 退出码表（全局唯一来源）:
#   0 OK / 1 ERROR / 2 argparse 用法错误 / 3 UNINITIALIZED_STORE /
#   4 STALE_BASE / 5 TOPIC_NOT_FOUND 或 ITEM_NOT_FOUND /
#   6 UNSUPPORTED_SCHEMA_VERSION / 7 INVALID_INPUT
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNINITIALIZED_STORE = 3
EXIT_STALE_BASE = 4
EXIT_NOT_FOUND = 5
EXIT_UNSUPPORTED_SCHEMA_VERSION = 6
EXIT_INVALID_INPUT = 7

PLACEHOLDER_COMMANDS = ()  # 11 子命令全部实装（M7 收口）


# 人类模式输出折叠：超过 TRIGGER 字节的物理行按码点边界硬折叠，续行带 MARKER；
# 折叠后任一物理行 UTF-8 字节长度 <= 1500（TRIGGER 1200 + 标记字节数 << 1500）。
_FOLD_TRIGGER_BYTES = 1200
_FOLD_MARKER = "↪ "  # cli-contract §3 钉死的续接标记字面量（无 md 协议字面量冲突）

_MATCH_META_ORDER = ("topic_id", "title", "score", "head_hop", "full", "updated_at")
_MATCH_TAIL_ORDER = ("base_sha", "artifacts", "text")


_STRUCTURE = False  # 结构行：按层级缩进
_VERBATIM = True    # verbatim 内容行：原样落在第 0 列，不再被上层缩进改写


def _scalar_text(value) -> str:
    """人类模式标量：None/bool 走 JSON 字面量（null/true/false），其余 str()。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_field(name: str, value, depth: int, lines: list) -> None:
    """单字段渲染：dict 逐键、list 逐元素、嵌套字符串逐行 verbatim（不进 json 转义）。"""
    pad = "  " * depth
    if isinstance(value, dict):
        if not value:
            lines.append((_STRUCTURE, f"{pad}{name}: {{}}"))
            return
        lines.append((_STRUCTURE, f"{pad}{name}:"))
        _render_mapping(value, depth + 1, lines)
    elif isinstance(value, list):
        if not value:
            lines.append((_STRUCTURE, f"{pad}{name}: []"))
            return
        lines.append((_STRUCTURE, f"{pad}{name}:"))
        _render_sequence(value, depth + 1, lines)
    elif isinstance(value, str) and "\n" in value:
        lines.append((_STRUCTURE, f"{pad}{name}:"))
        lines.extend((_VERBATIM, raw) for raw in value.splitlines())
    else:
        lines.append((_STRUCTURE, f"{pad}{name}: {_scalar_text(value)}"))


def _render_mapping(mapping: dict, depth: int, lines: list) -> None:
    for name, value in mapping.items():
        _render_field(str(name), value, depth, lines)


def _render_sequence(items: list, depth: int, lines: list) -> None:
    pad = "  " * depth
    for item in items:
        if isinstance(item, dict) and item:
            nested: list = []
            _render_mapping(item, 0, nested)
            _is_verbatim, first_text = nested[0]
            lines.append((_STRUCTURE, f"{pad}- {first_text}"))
            continuation_pad = pad + "  "
            for is_verbatim, text in nested[1:]:
                if is_verbatim:  # verbatim 内容行保持第 0 列
                    lines.append((_VERBATIM, text))
                else:
                    lines.append((_STRUCTURE, continuation_pad + text))
        elif isinstance(item, list):
            if not item:
                lines.append((_STRUCTURE, f"{pad}- []"))
            else:
                lines.append((_STRUCTURE, f"{pad}-"))
                _render_sequence(item, depth + 1, lines)
        elif isinstance(item, dict):
            lines.append((_STRUCTURE, f"{pad}- {{}}"))
        elif isinstance(item, str) and "\n" in item:
            lines.append((_STRUCTURE, f"{pad}-"))
            lines.extend((_VERBATIM, raw) for raw in item.splitlines())
        else:
            lines.append((_STRUCTURE, f"{pad}- {_scalar_text(item)}"))


def _render_match_entry(entry: dict, index: int, lines: list) -> None:
    """match 单命中分块：分隔行 + 元数据 + 全量 base_sha + artifacts + text + 结束行。"""
    topic_id = entry.get("topic_id", "")
    lines.append((_STRUCTURE, f"──── match {index}: {topic_id} ────"))
    for name in _MATCH_META_ORDER:
        if name in entry:
            _render_field(name, entry[name], 1, lines)
    for name in _MATCH_TAIL_ORDER:
        if name in entry:
            _render_field(name, entry[name], 1, lines)
    handled = set(_MATCH_META_ORDER) | set(_MATCH_TAIL_ORDER)
    for name, value in entry.items():
        if name not in handled:  # 例如 --trace 附带的 traces
            _render_field(str(name), value, 1, lines)
    lines.append((_STRUCTURE, f"──── end match {index} ────"))


def _byte_prefix_len(text: str, max_bytes: int) -> int:
    """最长码点前缀的字符数，使其 UTF-8 编码不超过 max_bytes（绝不切开多字节码点）。"""
    total = 0
    for count, char in enumerate(text):
        size = len(char.encode("utf-8"))
        if total + size > max_bytes:
            return count if count > 0 else 1
        total += size
    return len(text)


def _fold_line(line: str) -> list:
    if len(line.encode("utf-8")) <= _FOLD_TRIGGER_BYTES:
        return [line]
    chunks: list = []
    rest = line
    while rest:
        cut = _byte_prefix_len(rest, _FOLD_TRIGGER_BYTES)
        chunks.append(rest[:cut])
        rest = rest[cut:]
    folded = [chunks[0]]
    folded.extend(_FOLD_MARKER + chunk for chunk in chunks[1:])
    return folded


def _fold_lines(lines: list) -> list:
    folded: list = []
    for line in lines:
        folded.extend(_fold_line(line))
    return folded


def render_output(result: dict, as_json: bool) -> str:
    """--json 时输出缩进 JSON（改前后逐字节一致）；否则输出多行人类可读文本。

    人类模式：dict 逐键行、list 逐元素行（非空嵌套 list 同样逐元素递归）、嵌套字符串
    逐行 verbatim 且内容行恒在第 0 列；match 的 `results` 按命中分块呈现；最终拼接的
    每个物理行超过 1200 字节即按码点边界折叠（续行以 `↪ ` 起头），硬保证任一物理行
    UTF-8 字节长度 <= 1500。
    """
    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2)
    rendered: list = []
    for key, value in result.items():
        if key == "results" and isinstance(value, list):
            if not value:
                rendered.append((_STRUCTURE, "results: []"))
                continue
            rendered.append((_STRUCTURE, "results:"))
            for index, entry in enumerate(value, 1):
                _render_match_entry(entry, index, rendered)
        else:
            _render_field(str(key), value, 0, rendered)
    return "\n".join(_fold_lines([text for _is_verbatim, text in rendered]))


def _make_placeholder_handler(command: str):
    def handler(args: argparse.Namespace) -> tuple[dict, int]:
        return (
            {"status": "INVALID_INPUT", "error": f"子命令 '{command}' 在本里程碑未实现"},
            EXIT_INVALID_INPUT,
        )

    return handler


def _global_option_parser() -> argparse.ArgumentParser:
    """子命令共用的全局参数（default=SUPPRESS：只透传显式给出的值）。"""
    option_parser = argparse.ArgumentParser(add_help=False)
    option_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    option_parser.add_argument("--store-dir", dest="store_dir", default=argparse.SUPPRESS)
    option_parser.add_argument("--workspace", dest="workspace", default=argparse.SUPPRESS)
    return option_parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topicdock",
        description="TopicDock — zero-dependency single-file six-layer knowledge item engine",
    )
    parser.add_argument("--json", action="store_true", help="output results as JSON")
    parser.add_argument("--store-dir", dest="store_dir", default=None, help="store directory (explicit)")
    parser.add_argument("--workspace", dest="workspace", default=None, help="workspace directory for store resolution")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    init_parser = subparsers.add_parser(
        "init", parents=[_global_option_parser()], help="initialize a store (idempotent)"
    )
    init_parser.add_argument("--scope", choices=("global", "local"), required=True)
    init_parser.add_argument("--path", default=None, help="custom store location (also writes the pointer config)")
    init_parser.set_defaults(handler=cmd_init)

    save_parser = subparsers.add_parser(
        "save", parents=[_global_option_parser()],
        help="the only authoritative write entry (CAS + four-step commit)",
    )
    save_source = save_parser.add_mutually_exclusive_group(required=True)
    save_source.add_argument("--stdin", action="store_true", help="read the JSON payload from stdin")
    save_source.add_argument("--json-file", dest="json_file", default=None, help="read the payload from a JSON file")
    save_parser.add_argument("--amend", action="store_true", help="merge into the current hop in place (auto-degrades when sealed)")
    save_parser.add_argument("--fork-as", dest="fork_as", default=None, help="fork a new topic from the payload's topic")
    save_parser.add_argument("--force-merge", action="store_true", help="replay on top of the current head (last write wins)")
    save_parser.set_defaults(handler=cmd_save)

    use_parser = subparsers.add_parser(
        "use", parents=[_global_option_parser()],
        help="mount a topic (list candidates when no selector is given)",
    )
    use_parser.add_argument("topic", nargs="?", default=None, help="topic_id or list index")
    use_parser.add_argument(
        "--trace", default=None, metavar="<auto|items>",
        help="Inline traces after mounting: 'auto' (unresolved [Q] + breakpoint [~][A]) "
             "or comma-separated item ids (e.g. C1,C2)",
    )
    use_parser.set_defaults(handler=cmd_use)

    list_parser = subparsers.add_parser(
        "list", parents=[_global_option_parser()], help="topic list (paged/filtered)"
    )
    list_parser.add_argument("--page", type=int, default=None, help="page number (default 1)")
    list_parser.add_argument("--page-size", dest="page_size", type=int, default=None, help="items per page (default 10)")
    list_parser.add_argument("--all", action="store_true", help="list everything (ignores --page/--page-size)")
    list_parser.add_argument("--filter", dest="filter", default=None, help="case-insensitive substring filter on topic_id/title")
    list_parser.set_defaults(handler=cmd_list)

    view_parser = subparsers.add_parser(
        "view", parents=[_global_option_parser()], help="show a topic's md full text or hop-chain history"
    )
    view_parser.add_argument("topic", help="topic_id or list index")
    view_parser.add_argument("--history", action="store_true", help="output the reverse-topological hop summary chain")
    view_parser.set_defaults(handler=cmd_view)

    match_parser = subparsers.add_parser(
        "match", parents=[_global_option_parser()], help="recall topics by keywords (0-100 scoring)"
    )
    match_parser.add_argument("--topic", action="append", help="query keyword (repeatable; may be omitted with --recent)")
    match_parser.add_argument("--threshold", type=float, default=35.0, help="recall threshold (default 35)")
    match_parser.add_argument("--recent", action="store_true", help="take the <=5 most recent topics directly (no --topic needed)")
    match_parser.add_argument(
        "--trace", default=None, metavar="<auto|items>",
        help="Inline traces for the top-5 full hits: 'auto' (unresolved [Q] + "
             "breakpoint [~][A]) or comma-separated item ids (e.g. C1,C2)",
    )
    match_parser.set_defaults(handler=cmd_match)

    trace_parser = subparsers.add_parser(
        "trace", parents=[_global_option_parser()], help="full item lineage (including cross-topic recursion)"
    )
    trace_parser.add_argument("topic", help="topic_id")
    trace_parser.add_argument(
        "items", nargs="+",
        help="item_id(s), comma or space separated (e.g. C3,C4 Q3); "
             "a single item keeps the legacy payload",
    )
    trace_parser.set_defaults(handler=cmd_trace)

    merge_parser = subparsers.add_parser(
        "merge", parents=[_global_option_parser()], help="consolidate src into dst (cross-topic)"
    )
    merge_parser.add_argument("src", help="source topic_id")
    merge_parser.add_argument("dst", help="destination topic_id")
    merge_parser.add_argument("--summary", default="", help="consolidation hop summary")
    merge_parser.add_argument("--session-id", dest="session_id", default="", help="session identifier")
    merge_parser.add_argument("--actor", default="", help="acting principal (default empty)")
    merge_parser.add_argument("--model", default="", help="model identifier for this hop (optional)")
    merge_parser.add_argument("--next-focus", dest="next_focus", default="", help="next focus")
    merge_parser.set_defaults(handler=cmd_merge)

    delete_parser = subparsers.add_parser(
        "delete", parents=[_global_option_parser()], help="delete topics (preview / execute with --yes)"
    )
    delete_parser.add_argument("expr", help="index expression (2,4~6 or keep 1,3), or a topic_id list with --yes")
    delete_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="preview only (the default without --yes)")
    delete_parser.add_argument("--yes", action="store_true", help="execute deletion (accepts explicit topic_ids only)")
    delete_parser.set_defaults(handler=cmd_delete)

    undo_parser = subparsers.add_parser(
        "undo", parents=[_global_option_parser()], help="roll back the most recent step (LIMIT 3)"
    )
    undo_parser.add_argument("--force", action="store_true", help="relax only the post_hashes check (child-hop check always enforced)")
    undo_parser.set_defaults(handler=cmd_undo)

    doctor_parser = subparsers.add_parser(
        "doctor", parents=[_global_option_parser()], help="self-heal inspection"
    )
    doctor_parser.add_argument("--rebuild", action="store_true", help="force a cold rebuild of the SQLite graph")
    doctor_parser.set_defaults(handler=cmd_doctor)

    for name in PLACEHOLDER_COMMANDS:
        placeholder = subparsers.add_parser(
            name, parents=[_global_option_parser()], help=f"{name}（本里程碑未实现）"
        )
        placeholder.add_argument("remainder", nargs="*", help=argparse.SUPPRESS)
        placeholder.set_defaults(handler=_make_placeholder_handler(name))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码（argparse 用法错误时其自身以 2 退出）。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    if args.command == "init":
        result, code = cmd_init(args)
    else:
        start_dir = Path(args.workspace) if getattr(args, "workspace", None) else None
        ctx = resolve_store(start_dir, getattr(args, "store_dir", None))
        if ctx is None or not _is_initialized_store(ctx.root):
            result = {
                "status": "UNINITIALIZED_STORE",
                "default_global": "~/.topicdock",
                "workspace_local": str(Path.cwd() / ".topicdock"),
            }
            code = EXIT_UNINITIALIZED_STORE
        else:
            try:
                result, code = args.handler(args)
            except StaleBase as exc:
                result, code = exc.payload, EXIT_STALE_BASE
            except UnsupportedSchemaVersion as exc:
                result, code = (
                    {"status": "UNSUPPORTED_SCHEMA_VERSION", "error": str(exc)},
                    EXIT_UNSUPPORTED_SCHEMA_VERSION,
                )
            except TopicNotFoundError as exc:
                result, code = (
                    {"status": "TOPIC_NOT_FOUND", "error": str(exc)},
                    EXIT_NOT_FOUND,
                )
            except ItemNotFoundError as exc:
                result, code = (
                    {"status": "ITEM_NOT_FOUND", "error": str(exc)},
                    EXIT_NOT_FOUND,
                )
            except InvalidInput as exc:
                detail = exc.args[0] if exc.args else "INVALID_INPUT"
                if isinstance(detail, dict):
                    result = {"status": "INVALID_INPUT", **detail}
                else:
                    result = {"status": "INVALID_INPUT", "error": str(detail)}
                code = EXIT_INVALID_INPUT
            except Exception as exc:  # 兜底：载荷类意外异常映射为 ERROR(1) 而非 traceback
                result = {
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                code = EXIT_ERROR
    sys.stdout.write(render_output(result, as_json) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
