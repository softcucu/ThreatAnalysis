"""Optional progress reporting helpers for runtime and pipeline execution."""

from __future__ import annotations

import sys
import threading
import time
from typing import Protocol, TextIO


class ProgressReporter(Protocol):
    def emit(self, message: str) -> None:
        """Publish one progress line."""


class ProgressPrinter:
    """Thread-safe console progress printer.

    Progress is written to stderr by default so command stdout can remain
    machine-readable.
    """

    def __init__(self, *, enabled: bool = False, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def emit(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            print(f"[{time.strftime('%H:%M:%S')}] {message}", file=self.stream, flush=True)
