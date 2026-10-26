import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from download_worker import DownloadWorker, MAX_DOWNLOAD_BYTES


class FakeResponse:
    def __init__(self, data: bytes, length: str | None = None, final_url: str = "https://example.com/file"):
        self.data = data
        self.headers = {"Content-Length": length} if length is not None else {}
        self.final_url = final_url
        self.stream = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, size=-1):
        return self.stream.read(size)


class DownloadWorkerTests(unittest.TestCase):
    def run_worker(self, url, destination, response=None):
        worker = DownloadWorker(url, str(destination))
        successes, failures = [], []
        worker.success.connect(successes.append)
        worker.failure.connect(failures.append)
        if response is None:
            worker.run()
        else:
            with patch("download_worker.urlopen", return_value=response):
                worker.run()
        return successes, failures

    def test_rejects_non_https_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            with patch("download_worker.urlopen") as open_url:
                successes, failures = self.run_worker("http://example.com/file", target)
            open_url.assert_not_called()
            self.assertFalse(successes)
            self.assertIn("HTTPS", failures[0])
            self.assertFalse(target.exists())

    def test_rejects_redirect_to_http(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            successes, failures = self.run_worker("https://example.com/file", target,
                FakeResponse(b"x", final_url="http://example.com/file"))
            self.assertFalse(successes)
            self.assertIn("HTTPS", failures[0])
            self.assertFalse(target.exists())

    def test_rejects_declared_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            successes, failures = self.run_worker("https://example.com/file", target,
                FakeResponse(b"x", length=str(MAX_DOWNLOAD_BYTES + 1)))
            self.assertFalse(successes)
            self.assertIn("25MB", failures[0])
            self.assertFalse(target.exists())

    def test_rejects_stream_oversize_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            existing = b"previous contents"
            target.write_bytes(existing)
            successes, failures = self.run_worker("https://example.com/file", target,
                FakeResponse(b"x" * (MAX_DOWNLOAD_BYTES + 1)))
            self.assertFalse(successes)
            self.assertIn("25MB", failures[0])
            self.assertEqual(target.read_bytes(), existing)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_success_writes_replacement_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            successes, failures = self.run_worker("https://example.com/file", target,
                FakeResponse(b"contents", length="8"))
            self.assertEqual(successes, [str(target)])
            self.assertFalse(failures)
            self.assertEqual(target.read_bytes(), b"contents")


if __name__ == "__main__":
    unittest.main()
