#!/usr/bin/env python3
"""TopicDock 构建脚本（D5/F5 拆包后）：按文件名序纯拼接 scripts/layers/*.py，
生成单文件引擎 scripts/topicdock.py——分发与测试的唯一可执行形态。

- 分片是唯一事实源：build 只做「按名排序 → 直接拼接」，零改写、零重排。
- 产物为生成文件（gitignore），禁止手改；语法自检通过才落盘（失败不替换产物）。
- `--check` 只按字节比对产物与分片，不落盘（漂移时退出码 1）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_LAYERS_DIR = _SCRIPTS_DIR / "layers"
_OUTPUT_PATH = _SCRIPTS_DIR / "topicdock.py"


def layer_paths() -> list[Path]:
    """按文件名序返回全部分片；目录缺失或为空时报错退出。"""
    if not _LAYERS_DIR.is_dir():
        raise SystemExit(f"缺少分片目录: {_LAYERS_DIR}")
    paths = sorted(_LAYERS_DIR.glob("*.py"))
    if not paths:
        raise SystemExit(f"分片目录为空: {_LAYERS_DIR}")
    return paths


def build_data() -> bytes:
    """纯拼接全部分片（零改写、零重排；按字节读写防换行符归一），返回产物全文。"""
    return b"".join(path.read_bytes() for path in layer_paths())


def is_stale() -> bool:
    """产物缺失，或任一分片 / build.py 比产物新 → 需要重建。"""
    if not _OUTPUT_PATH.is_file():
        return True
    output_mtime = _OUTPUT_PATH.stat().st_mtime_ns
    sources = [*layer_paths(), Path(__file__).resolve()]
    return any(path.stat().st_mtime_ns > output_mtime for path in sources)


def build(check: bool = False) -> Path:
    """重建产物（语法自检通过才落盘）；check=True 只按字节比对（漂移退出码 1）。"""
    data = build_data()
    if check:
        current = _OUTPUT_PATH.read_bytes() if _OUTPUT_PATH.is_file() else None
        if current != data:
            sys.stderr.write("scripts/topicdock.py 与 layers/ 分片存在漂移，请运行 build.py 重建\n")
            raise SystemExit(1)
        return _OUTPUT_PATH
    compile(data.decode("utf-8"), str(_OUTPUT_PATH), "exec")  # 先过语法关，失败不落盘
    tmp_path = _OUTPUT_PATH.with_name(f"{_OUTPUT_PATH.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_bytes(data)
        os.chmod(tmp_path, 0o755)  # 产物带 shebang，保持可执行（防 os.replace 继承 644）
        os.replace(tmp_path, _OUTPUT_PATH)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return _OUTPUT_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build",
        description="按文件名序拼接 scripts/layers/ 分片，生成 scripts/topicdock.py",
    )
    parser.add_argument(
        "--check", action="store_true", help="只比对产物与分片是否一致（不落盘）"
    )
    args = parser.parse_args(argv)
    if args.check:
        build(check=True)
        print("build: 产物与 layers/ 分片一致")
    else:
        path = build()
        print(f"build: 已生成 {path}（{len(path.read_text(encoding='utf-8').splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
