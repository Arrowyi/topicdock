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


def render_output(result: dict, as_json: bool) -> str:
    """--json 时输出缩进 JSON；否则输出人类可读的键值行。"""
    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = []
    for key, value in result.items():
        if isinstance(value, (dict, list)):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


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
