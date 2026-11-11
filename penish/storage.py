from pathlib import Path
from typing import BinaryIO, Protocol


class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def open(self, key: str) -> BinaryIO: ...
    def path(self, key: str) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def public_url(self, key: str) -> str: ...


# R2 (or any object store) can implement Storage later. path() is the one
# method that only makes sense locally; callers that need it are local-only.
class LocalStorage:
    def __init__(self, root: Path, url_prefix: str = "/media/pub") -> None:
        self.root = root.resolve()
        self.url_prefix = url_prefix.rstrip("/")

    def path(self, key: str) -> Path:
        # Keys can come from slugs or filenames, so refuse anything that
        # resolves outside the root (../, absolute paths, symlink hops).
        p = (self.root / key).resolve()
        if p == self.root or not p.is_relative_to(self.root):
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename so a reader never sees a half-written file.
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def open(self, key: str) -> BinaryIO:
        return self.path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def public_url(self, key: str) -> str:
        self.path(key)
        return f"{self.url_prefix}/{key.lstrip('/')}"
