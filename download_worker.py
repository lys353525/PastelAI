"""HTTPS-only bounded download worker."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import PySide6.QtCore as QtCore
    QThread = QtCore.QThread
    Signal = QtCore.Signal
except ImportError:
    class _Signal:
        def __init__(self, *args): self._slots = []
        def connect(self, slot): self._slots.append(slot)
        def emit(self, *args):
            for slot in self._slots: slot(*args)
    class QThread(threading.Thread):
        def __init__(self, parent=None): super().__init__(daemon=True)
        def wait(self, timeout=None): self.join(None if timeout is None else timeout / 1000)
    def Signal(*args): return _Signal()

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

class DownloadWorker(QThread):
    success = Signal(str)
    failure = Signal(str)
    completed = Signal(str, str)
    failed = Signal(str)
    def __init__(self, url: str, destination: str, parent=None):
        super().__init__(parent)
        self.url, self.destination = url, destination
    def run(self):
        temporary = None
        try:
            if urlparse(self.url).scheme.lower() != "https":
                raise ValueError("HTTPS 다운로드만 허용됩니다.")
            request = Request(self.url, headers={"User-Agent": "PastelAIStudio/1.0"})
            with urlopen(request, timeout=30) as response:
                if urlparse(response.geturl()).scheme.lower() != "https":
                    raise ValueError("HTTPS 이외의 주소로 이동한 다운로드는 차단됩니다.")
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("다운로드 파일은 25MB 이하여야 합니다.")
                target = Path(self.destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".pastel-download-", dir=str(target.parent))
                size = 0
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk: break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("다운로드 파일은 25MB 이하여야 합니다.")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                temporary = None
                self.completed.emit(str(target), "다운로드가 완료되었습니다.")
                self.success.emit(str(target))
        except Exception as exc:
            self.failed.emit(str(exc))
            self.failure.emit(str(exc))
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)
