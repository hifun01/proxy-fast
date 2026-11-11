"""Atomic, private local state and a cross-platform process lock."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from .cloud import ProxyError


def default_state_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "proxy-fast"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "proxy-fast"


class Store:
    def __init__(self, path: Path, account_id: str):
        self.path, self.account_id = Path(path), account_id
        self.path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.chmod(0o700)
        self.file = self.path / "state.json"
        if self.file.exists():
            try:
                self.data = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProxyError(f"State is unreadable: {self.file}. Preserve it for resource recovery.") from exc
            if self.data.get("account_id") != account_id or self.data.get("schema") != 1:
                raise ProxyError("State belongs to another account or unsupported schema; use a different --state-dir.")
        else:
            self.data = {"schema": 1, "account_id": account_id, "owner_id": uuid.uuid4().hex, "phase": "off"}
            self.save()

    def save(self):
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear_resources(self):
        self.data = {k: self.data[k] for k in ("schema", "account_id", "owner_id")}
        self.data["phase"] = "off"
        self.save()


@contextlib.contextmanager
def locked(root: Path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    with (root / "controller.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ProxyError("Another proxy-fast command is running for this state directory.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ProxyError("Another proxy-fast command is running for this state directory.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
