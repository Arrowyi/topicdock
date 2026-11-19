# ===================== L1: StoreResolver & LockGuard =====================

def atomic_write_text(path: Path, content: str) -> None:
    """原子写文本：同目录 .tmp → flush+fsync → os.replace → 父目录 fsync。"""
    path = Path(path)
    tmp_path = path.parent / (path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass  # 部分文件系统不支持目录 fsync；os.replace 本身已是原子操作
    finally:
        os.close(dir_fd)


def sha256_text(content: str) -> str:
    """UTF-8 编码后 SHA-256，64 位小写 hex。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """文件字节流 SHA-256；文件缺失返回 'MISSING'。"""
    file_path = Path(path)
    if not file_path.is_file():
        return "MISSING"
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileLock:
    """跨平台文件锁上下文管理器（POSIX fcntl.flock / Windows msvcrt.locking）。

    - 排他模式（默认）: LOCK_EX / LK_LOCK(阻塞) 或 LK_NBLCK(非阻塞)。
    - 共享模式: LOCK_SH（Windows 的 msvcrt 无共享锁，退化为同区域排他锁近似）。
    - 进入时确保锁文件存在（不存在则创建），退出时解锁并关闭 fd。
    """

    def __init__(self, lock_path: Path, shared: bool = False) -> None:
        self.lock_path = Path(lock_path)
        self.shared = shared
        self._fd: int | None = None

    def acquire(self, shared: bool | None = None, blocking: bool = True) -> "FileLock":
        """获取锁；blocking=False 时不可得则抛 OSError（BlockingIOError）。"""
        if shared is not None:
            self.shared = shared
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.lock_path.exists():
            create_fd = os.open(self.lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
            os.close(create_fd)
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            if os.name == "nt" and msvcrt is not None:
                # Windows: 仅提供区域排他锁；共享模式按同样方式近似。
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, 1)
            elif fcntl is not None:
                flags = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(fd, flags)
            else:  # pragma: no cover
                raise RuntimeError("当前平台无可用的文件锁原语")
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        """释放锁；未持锁时为幂等空操作。"""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt" and msvcrt is not None:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass
class StoreContext:
    """已定位的 store：root 目录 + 各约定路径。"""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "topicdock.db"

    @property
    def topics_dir(self) -> Path:
        return self.root / "topics"

    @property
    def hops_dir(self) -> Path:
        return self.root / "hops"

    @property
    def meta_path(self) -> Path:
        return self.root / ".topicdock-meta.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".topicdock.lock"

    @property
    def gitignore_path(self) -> Path:
        return self.root / ".gitignore"

    def meta_json(self) -> dict:
        """读 meta；缺失或 JSON 损坏（含非 dict）时返回默认值。"""
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _default_meta()
        return data if isinstance(data, dict) else _default_meta()

    def write_meta(self, meta: dict) -> None:
        """原子写 meta JSON。"""
        atomic_write_text(
            self.meta_path,
            json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def shared_lock(self) -> FileLock:
        """以 lock_path 为锁文件的共享锁上下文管理器。"""
        return FileLock(self.lock_path, shared=True)

    def exclusive_lock(self) -> FileLock:
        """以 lock_path 为锁文件的排他锁上下文管理器。"""
        return FileLock(self.lock_path, shared=False)


def _pointer_store_dir(pointer_path: Path) -> Path | None:
    """从 topicdock 指针 JSON 读 store_dir；缺失/非法/无该键返回 None。"""
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and "store_dir" in data:
        return Path(str(data["store_dir"])).expanduser().resolve()
    return None


def _is_store_dir(dir_path: Path) -> bool:
    """目录是 store ⇔ 含 topicdock.db 或含 topics/ 子目录。"""
    return dir_path.is_dir() and (
        (dir_path / "topicdock.db").exists() or (dir_path / "topics").is_dir()
    )


def _is_initialized_store(dir_path: Path) -> bool:
    """main 门禁谓词（独立于冒泡寻址的 _is_store_dir，不复用不放宽）。"""
    path = Path(dir_path)
    return path.is_dir() and any(
        (path / name).exists()
        for name in ("topics", "hops", "topicdock.db", ".topicdock-meta.json")
    )


def resolve_store(start_dir: Path | None, explicit_dir: str | None) -> StoreContext | None:
    """定位生效 store：explicit > 逐级向上（每级先指针后 .topicdock）> 全局回退。

    - explicit_dir 非空 → 直接返回该目录的 StoreContext（不检查存在性）。
    - 从 start_dir（None 时取 cwd）逐级向上直至文件系统根，命中即停。
    - 全程未命中 → 查 ~/.agents/topicdock/config.json 或 ~/.topicdock/；仍无 → None。
    """
    if explicit_dir:
        return StoreContext(Path(explicit_dir).expanduser().resolve())
    current = (Path(start_dir) if start_dir is not None else Path.cwd()).resolve()
    while True:
        pointer_dir = _pointer_store_dir(current / ".agents" / "topicdock.json")
        if pointer_dir is not None:
            return StoreContext(pointer_dir)
        local_store = current / ".topicdock"
        if _is_store_dir(local_store):
            return StoreContext(local_store)
        if current.parent == current:
            break
        current = current.parent
    home = Path.home()
    global_pointer = _pointer_store_dir(home / ".agents" / "topicdock" / "config.json")
    if global_pointer is not None:
        return StoreContext(global_pointer)
    if _is_store_dir(home / ".topicdock"):
        return StoreContext(home / ".topicdock")
    return None


