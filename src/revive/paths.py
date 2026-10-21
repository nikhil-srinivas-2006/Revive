"""Application path boundaries; these are not an OS sandbox."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .errors import ReviveError


def check_plain_path(path: Path):
    if ".." in path.parts or (
        os.name == "nt"
        and (str(path).startswith(("\\\\", "//")) or ":" in str(path)[2:])
    ):
        raise ReviveError(
            "PATH_UNSAFE", "Traversal, network and device paths are unsupported."
        )
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReviveError(
                "PATH_UNSAFE",
                "Links and cloud placeholders are unsupported in this milestone.",
            )


def within(root: Path, path: Path) -> Path:
    candidate = path.absolute()
    if not candidate.is_relative_to(root) or candidate == root:
        raise ReviveError("PATH_UNSAFE", "Path is outside its authorized directory.")
    check_plain_path(candidate)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ReviveError(
            "PATH_UNSAFE", "Resolved path is outside its authorized directory."
        )
    return candidate


def signature(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


@contextmanager
def source_reader(root: Path, path: Path):
    path = within(root, path)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ReviveError(
                "FILE_BUSY", "Source cannot be opened exclusively for reading.", True
            )
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        try:
            final = kernel.GetFinalPathNameByHandleW
            final.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            final.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            count = final(handle, buffer, len(buffer), 0)
            if not count or count >= len(buffer):
                raise ReviveError("PATH_UNSAFE", "Unable to verify source handle.")
            within(root, Path(buffer.value.removeprefix("\\\\?\\")))
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ReviveError("PATH_UNSAFE", "Source must be a regular file.")
        if signature(path.stat()) != signature(before):
            raise ReviveError("SOURCE_CHANGED", "Source changed while opening.", True)
        yield stream


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        check_plain_path(self.path)
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                if self.stream.read(1) == b"":
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ReviveError(
                "ALREADY_RUNNING", "Another monitor owns this application directory."
            ) from exc
        return self

    def __exit__(self, *args):
        self.stream.close()
