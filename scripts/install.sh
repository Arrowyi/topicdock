#!/usr/bin/env bash
# TopicDock 一句话安装脚本：探测 Python → 获取源码 → 差异安装 → 直跑已装引擎自检。
#
# 用法（终端直接执行，或在 AI Agent 聊天里原文粘贴发送）：
#   curl -fsSL https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh | bash
#   ... | bash -s -- --client cursor
#
# 环境变量：
#   TOPICDOCK_SRC_DIR  非空时直接使用该本地源码目录（测试注入点；脚本绝不删除它）。
#
# 兼容 macOS 自带 bash 3.2：不使用空数组展开等 3.2 陷阱写法。
set -euo pipefail

REPO_URL="https://github.com/Arrowyi/topicdock.git"
ARCHIVE_URL="https://github.com/Arrowyi/topicdock/archive/refs/heads/main.tar.gz"
INSTALL_SH_URL="https://raw.githubusercontent.com/Arrowyi/topicdock/main/scripts/install.sh"

VALID_CLIENTS="claude cursor opencode codex all"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

usage() {
    cat <<'EOF'
TopicDock 安装脚本
用法: install.sh [--client claude|cursor|opencode|codex|all]
  不带 --client 时自动升级本机已装客户端；一个都没装过会打印示例命令并以退出码 2 结束。
EOF
}

# ---------- 参数解析 ----------
CLIENT=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --client)
            if [ "$#" -lt 2 ]; then
                err "install.sh: --client 需要一个值（claude|cursor|opencode|codex|all）"
                exit 2
            fi
            CLIENT="$2"
            shift 2
            ;;
        --client=*)
            CLIENT="${1#--client=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            err "install.sh: 未知参数: $1"
            exit 2
            ;;
    esac
done

if [ -n "$CLIENT" ]; then
    case " $VALID_CLIENTS " in
        *" $CLIENT "*) ;;
        *)
            err "install.sh: 非法 --client 值: ${CLIENT}（可选 claude|cursor|opencode|codex|all）"
            exit 2
            ;;
    esac
fi

# ---------- Python 探测（≥ 3.9） ----------
version_ok() {
    # $1 = 形如 "Python 3.13.13" 的版本输出
    local ver rest major minor
    ver="${1#Python }"
    ver="${ver%% *}"
    case "$ver" in
        [0-9]*.[0-9]*) ;;
        *) return 1 ;;
    esac
    major="${ver%%.*}"
    rest="${ver#*.}"
    minor="${rest%%.*}"
    case "$major$minor" in
        *[!0-9]*) return 1 ;;
    esac
    if [ "$major" -gt 3 ]; then
        return 0
    fi
    if [ "$major" -eq 3 ] && [ "$minor" -ge 9 ]; then
        return 0
    fi
    return 1
}

PY=""
for candidate in python3 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version_line="$("$candidate" --version 2>&1 || true)"
        if version_ok "$version_line"; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    err "请先安装 Python ≥ 3.9（macOS 可 brew install python）"
    exit 1
fi

# ---------- 源码获取 ----------
SRC=""
SCRIPT_TMP=""
cleanup() {
    if [ -n "$SCRIPT_TMP" ] && [ -d "$SCRIPT_TMP" ]; then
        rm -rf "$SCRIPT_TMP"
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

if [ -n "${TOPICDOCK_SRC_DIR:-}" ]; then
    # 测试注入点：直接使用本地源码，绝不删除该目录。
    SRC="$TOPICDOCK_SRC_DIR"
    if [ ! -f "$SRC/scripts/install.py" ]; then
        err "install.sh: TOPICDOCK_SRC_DIR 下找不到 scripts/install.py: $SRC"
        exit 1
    fi
else
    SCRIPT_TMP="$(mktemp -d "${TMPDIR:-/tmp}/topicdock-install.XXXXXX")"
    if command -v git >/dev/null 2>&1; then
        say "正在下载 TopicDock 源码（git clone）…"
        if ! git clone --depth 1 "$REPO_URL" "$SCRIPT_TMP/topicdock" >/dev/null 2>&1; then
            err "install.sh: git clone 失败: $REPO_URL"
            exit 1
        fi
        SRC="$SCRIPT_TMP/topicdock"
    else
        say "正在下载 TopicDock 源码（tarball）…"
        if ! curl -fsSL "$ARCHIVE_URL" | tar xz -C "$SCRIPT_TMP"; then
            err "install.sh: 源码下载失败: $ARCHIVE_URL"
            exit 1
        fi
        SRC="$SCRIPT_TMP/topicdock-main"
        if [ ! -f "$SRC/scripts/install.py" ]; then
            err "install.sh: 下载的源码目录结构异常: $SRC"
            exit 1
        fi
    fi
fi

# ---------- 差异安装 ----------
FAILED_CLIENTS=""
INSTALLED_CLIENTS=""

run_install_one() {
    local client="$1"
    say "→ 安装/升级 $client"
    if "$PY" "$SRC/scripts/install.py" --client "$client"; then
        INSTALLED_CLIENTS="$INSTALLED_CLIENTS $client"
        return 0
    fi
    err "install.sh: $client 安装失败"
    FAILED_CLIENTS="$FAILED_CLIENTS $client"
    return 1
}

detect_installed() {
    FOUND_CLIENTS=""
    if [ -d "$HOME/.claude/skills/topicdock" ]; then
        FOUND_CLIENTS="$FOUND_CLIENTS claude"
    fi
    if [ -d "$HOME/.config/opencode/skills/topicdock" ]; then
        FOUND_CLIENTS="$FOUND_CLIENTS opencode"
    fi
    if [ -d "$HOME/.cursor/skills/topicdock" ]; then
        FOUND_CLIENTS="$FOUND_CLIENTS cursor"
    fi
    if [ -d "$PWD/.agents/skills/topicdock" ]; then
        FOUND_CLIENTS="$FOUND_CLIENTS codex"
    fi
}

if [ -n "$CLIENT" ]; then
    if [ "$CLIENT" = "all" ]; then
        run_install_one all || true
    else
        run_install_one "$CLIENT" || true
    fi
else
    detect_installed
    if [ -z "$FOUND_CLIENTS" ]; then
        err "未检测到已安装的 TopicDock 客户端。请带 --client 重新安装，例如："
        err "  curl -fsSL $INSTALL_SH_URL | bash -s -- --client claude"
        err "  curl -fsSL $INSTALL_SH_URL | bash -s -- --client cursor"
        err "  curl -fsSL $INSTALL_SH_URL | bash -s -- --client opencode"
        err "  curl -fsSL $INSTALL_SH_URL | bash -s -- --client codex"
        exit 2
    fi
    for installed in $FOUND_CLIENTS; do
        run_install_one "$installed" || true
    done
fi

# ---------- 验证与报告 ----------
engine_for() {
    case "$1" in
        claude)
            printf '%s' "$HOME/.claude/skills/topicdock/scripts/topicdock.py"
            ;;
        opencode)
            printf '%s' "$HOME/.config/opencode/skills/topicdock/scripts/topicdock.py"
            ;;
        cursor)
            printf '%s' "$HOME/.cursor/skills/topicdock/scripts/topicdock.py"
            ;;
        codex)
            printf '%s' "$PWD/.agents/skills/topicdock/scripts/topicdock.py"
            ;;
    esac
}

# 只验证本次成功安装/升级的客户端；--client all 时四个引擎副本各验证一次。
VERIFY_CLIENTS=""
if [ -n "$CLIENT" ]; then
    if [ "$CLIENT" = "all" ]; then
        if [ -n "$INSTALLED_CLIENTS" ]; then
            VERIFY_CLIENTS="claude opencode cursor codex"
        fi
    elif [ -n "$INSTALLED_CLIENTS" ]; then
        VERIFY_CLIENTS="$CLIENT"
    fi
else
    VERIFY_CLIENTS="$INSTALLED_CLIENTS"
fi

VERIFIED=""
VERIFY_FAILED=""
for vclient in $VERIFY_CLIENTS; do
    engine="$(engine_for "$vclient")"
    if [ ! -f "$engine" ]; then
        err "install.sh: 未找到 $vclient 的已装引擎副本: $engine"
        VERIFY_FAILED="$VERIFY_FAILED $vclient"
        continue
    fi
    if doctor_output="$("$PY" "$engine" doctor --json 2>&1)"; then
        VERIFIED="$VERIFIED $vclient"
    else
        err "install.sh: $vclient 自检失败（doctor --json 非 0）"
        err "$doctor_output"
        VERIFY_FAILED="$VERIFY_FAILED $vclient"
    fi
done

say ""
say "TopicDock 安装完成"
if [ -n "$INSTALLED_CLIENTS" ]; then
    say "  已安装/升级:${INSTALLED_CLIENTS}"
fi
if [ -n "$VERIFIED" ]; then
    say "  自检通过（doctor --json exit 0）:${VERIFIED}"
fi
if [ -n "$VERIFY_FAILED" ]; then
    err "  自检失败:${VERIFY_FAILED}"
fi
if [ -n "$FAILED_CLIENTS" ]; then
    err "  安装失败:${FAILED_CLIENTS}"
fi

if [ -n "$FAILED_CLIENTS" ] || [ -n "$VERIFY_FAILED" ]; then
    err "TopicDock 安装未全部成功，请根据上方错误修复后重试。"
    exit 1
fi

say ""
say "在 Agent 中输入 /topicdock 开始使用。"
exit 0
