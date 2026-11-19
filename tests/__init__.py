"""TopicDock 测试包：注入仓库根 scripts/ 到 sys.path，并在分片较新时自动重建产物。"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# scripts/topicdock.py 是构建产物（D5/F5 拆包，D-P36）：分片或 build.py 比产物新时
# 自动重建（须在 import topicdock 之前执行，两种 unittest 启动方式等效）。
import build as _td_build

if _td_build.is_stale():
    _td_build.build()
    print("tests: 分片较新，已自动重建 scripts/topicdock.py", file=sys.stderr)
