"""Cross-platform identity and hashing bound to one opened file handle."""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int
    device: int
    inode: int

def _windows_file_id(stream) -> tuple[int, int]:
    import ctypes
    import msvcrt
    from ctypes import wintypes
    class Info(ctypes.Structure):
        _fields_ = [("attrs", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME), ("access_time", wintypes.FILETIME),
                    ("write_time", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_hi", wintypes.DWORD), ("size_lo", wintypes.DWORD),
                    ("links", wintypes.DWORD), ("index_hi", wintypes.DWORD), ("index_lo", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    info = Info()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed", stream.name)
    return int(info.volume), (int(info.index_hi) << 32) | int(info.index_lo)

def stream_identity(stream) -> FileIdentity:
    """Read identity from the descriptor/Windows handle that supplies the bytes."""
    stat = os.fstat(stream.fileno())
    device, inode = int(stat.st_dev), int(stat.st_ino)
    if os.name == "nt":
        device, inode = _windows_file_id(stream)
    if not device or not inode:
        raise OSError(f"stable file identity unavailable for {stream.name}")
    return FileIdentity(stat.st_size, stat.st_mtime_ns, device, inode)

def file_identity(path: Path) -> FileIdentity:
    with path.open("rb") as stream:
        return stream_identity(stream)

def hash_file_handle(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, FileIdentity]:
    """Open once, hash that descriptor, and reject in-place mutation during hashing."""
    with path.open("rb") as stream:
        before = stream_identity(stream)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
        after = stream_identity(stream)
    if before != after:
        raise OSError(f"file changed while hashing: {path}")
    return digest.hexdigest(), after
