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
        description="TopicDock — 零依赖单文件六层知识条目管理引擎",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--store-dir", dest="store_dir", default=None, help="显式指定 store 目录")
    parser.add_argument("--workspace", dest="workspace", default=None, help="寻址起点工作区目录")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    init_parser = subparsers.add_parser(
        "init", parents=[_global_option_parser()], help="初始化 store（幂等）"
    )
    init_parser.add_argument("--scope", choices=("global", "local"), required=True)
    init_parser.add_argument("--path", default=None, help="自定义 store 落点（并写指针配置）")
    init_parser.set_defaults(handler=cmd_init)

    save_parser = subparsers.add_parser(
        "save", parents=[_global_option_parser()],
        help="唯一权威写入入口（CAS + 四步落盘）",
    )
    save_source = save_parser.add_mutually_exclusive_group(required=True)
    save_source.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON 载荷")
    save_source.add_argument("--json-file", dest="json_file", default=None, help="从 JSON 文件读取载荷")
    save_parser.add_argument("--amend", action="store_true", help="同会话原地合并（封板自动降级）")
    save_parser.add_argument("--fork-as", dest="fork_as", default=None, help="以载荷主题为源分叉新主题")
    save_parser.add_argument("--force-merge", action="store_true", help="以当前 head 为基重放（最后写入胜）")
    save_parser.set_defaults(handler=cmd_save)

    use_parser = subparsers.add_parser(
        "use", parents=[_global_option_parser()],
        help="挂载主题（无 selector 时列出候选）",
    )
    use_parser.add_argument("topic", nargs="?", default=None, help="topic_id 或 list 序号")
    use_parser.set_defaults(handler=cmd_use)

    list_parser = subparsers.add_parser(
        "list", parents=[_global_option_parser()], help="主题列表（分页/过滤）"
    )
    list_parser.add_argument("--page", type=int, default=None, help="页码（默认 1）")
    list_parser.add_argument("--page-size", dest="page_size", type=int, default=None, help="每页条数（默认 10）")
    list_parser.add_argument("--all", action="store_true", help="全展（忽略 --page/--page-size）")
    list_parser.add_argument("--filter", dest="filter", default=None, help="topic_id/title 大小写不敏感子串过滤")
    list_parser.set_defaults(handler=cmd_list)

    view_parser = subparsers.add_parser(
        "view", parents=[_global_option_parser()], help="查看主题 md 全文或跳链历史"
    )
    view_parser.add_argument("topic", help="topic_id 或 list 序号")
    view_parser.add_argument("--history", action="store_true", help="输出逆拓扑跳摘要链")
    view_parser.set_defaults(handler=cmd_view)

    match_parser = subparsers.add_parser(
        "match", parents=[_global_option_parser()], help="关键词召回主题（0~100 打分）"
    )
    match_parser.add_argument("--topic", action="append", help="查询词（可多次；--recent 时可省略）")
    match_parser.add_argument("--threshold", type=float, default=35.0, help="召回阈值（默认 35）")
    match_parser.add_argument("--recent", action="store_true", help="直取最近 ≤5 个主题（免 --topic）")
    match_parser.set_defaults(handler=cmd_match)

    trace_parser = subparsers.add_parser(
        "trace", parents=[_global_option_parser()], help="条目全历史溯源（含跨主题递归）"
    )
    trace_parser.add_argument("topic", help="topic_id")
    trace_parser.add_argument("item", help="item_id")
    trace_parser.set_defaults(handler=cmd_trace)

    merge_parser = subparsers.add_parser(
        "merge", parents=[_global_option_parser()], help="跨主题合流（src 并入 dst）"
    )
    merge_parser.add_argument("src", help="源主题 topic_id")
    merge_parser.add_argument("dst", help="目标主题 topic_id")
    merge_parser.add_argument("--summary", default="", help="合流跳摘要")
    merge_parser.add_argument("--session-id", dest="session_id", default="", help="会话标识")
    merge_parser.add_argument("--actor", default="", help="操作者（默认空）")
    merge_parser.add_argument("--model", default="", help="本跳所用模型标识（可选）")
    merge_parser.add_argument("--next-focus", dest="next_focus", default="", help="待续焦点")
    merge_parser.set_defaults(handler=cmd_merge)

    delete_parser = subparsers.add_parser(
        "delete", parents=[_global_option_parser()], help="删除主题（预览 / --yes 执行）"
    )
    delete_parser.add_argument("expr", help="序号表达式（2,4~6 或 keep 1,3）或 --yes 时的 topic_id 列表")
    delete_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="仅预览（默认无 --yes 即预览）")
    delete_parser.add_argument("--yes", action="store_true", help="执行删除（仅接受显式 topic_id）")
    delete_parser.set_defaults(handler=cmd_delete)

    undo_parser = subparsers.add_parser(
        "undo", parents=[_global_option_parser()], help="回滚最近一步（LIMIT 3）"
    )
    undo_parser.add_argument("--force", action="store_true", help="仅放宽 post_hashes 校验（子跳检查恒强制）")
    undo_parser.set_defaults(handler=cmd_undo)

    doctor_parser = subparsers.add_parser(
        "doctor", parents=[_global_option_parser()], help="五维自愈巡检"
    )
    doctor_parser.add_argument("--rebuild", action="store_true", help="强制冷重建 SQLite 图库")
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
