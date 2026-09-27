"""Serial JSONL writer: one complete JSON object per line, flushed on every write (§3.2).

The writer never raises into the model path. Failures are counted and exposed through
:meth:`health`; callers (the live workload) stop issuing paid calls when ``failed`` grows.
"""

from __future__ import annotations

import atexit
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from aiecon.config import now_ms
from aiecon.spec.envelope import RawEnvelope


class JsonlWriter:
    def __init__(
        self,
        raw_dir: Path | str,
        *,
        clock: Callable[[], int] = now_ms,
        file_prefix: str = "events",
    ):
        self.raw_dir = Path(raw_dir)
        self.clock = clock
        self.file_prefix = file_prefix
        self._lock = threading.Lock()
        self._handles: dict[Path, IO[str]] = {}
        self.written = 0
        self.failed = 0
        self.last_error_class: str | None = None
        atexit.register(self.close)

    def path_for(self, ts_ms: int) -> Path:
        day = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        return self.raw_dir / day / f"{self.file_prefix}-{os.getpid()}.jsonl"

    def write(self, envelope: RawEnvelope) -> bool:
        """Append one envelope. Returns False (and counts) instead of raising."""

        try:
            line = envelope.model_dump_json()
            path = self.path_for(envelope.observed_at_ms)
            with self._lock:
                handle = self._handles.get(path)
                if handle is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = path.open("a", encoding="utf-8", newline="\n")
                    self._handles[path] = handle
                handle.write(line + "\n")
                handle.flush()
                self.written += 1
            return True
        except Exception as exc:  # noqa: BLE001 - must never propagate into the model path
            with self._lock:
                self.failed += 1
                self.last_error_class = type(exc).__name__
            return False

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "written": self.written,
                "failed": self.failed,
                "last_error_class": self.last_error_class,
                "open_files": len(self._handles),
            }

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._handles.clear()
