#!/usr/bin/env python3
"""TopicDock — 零依赖单文件六层知识条目管理引擎。

Python ≥ 3.9，仅标准库，零第三方依赖。
层级: L1 寻址/锁/原子写 → L2 MarkdownCodec → L3 SQLiteEngine
      → L4 自愈/重建 → L5 命令实现 → L6 CLI。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX advisory locking
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows record locking
    import msvcrt
except ImportError:  # non-Windows
    msvcrt = None  # type: ignore[assignment]


def _default_meta() -> dict:
    """Fresh store meta payload（schema v1，尚无 hop）。"""
    return {"schema_version": 1, "max_hop_seq": 0}


