"""Output writers.

RotatingWarcWriter - used inside each worker process. Every worker writes its own
                     .warc.gz files, so page bodies never travel between processes.
RunLog             - used only by the main process. Writes the small text files
                     (visited.tsv, failed.tsv, done.txt).
"""
import os
import threading
import time
from io import BytesIO

# Headers that describe the transfer, not the content. requests already undid
# gzip/chunked encoding, so keeping them would make the WARC record lie.
_DROP_HEADERS = {"content-encoding", "transfer-encoding", "content-length"}


class RotatingWarcWriter:
    """Thread-safe WARC writer that starts a new file at max_bytes (compressed)."""

    def __init__(self, directory, prefix, max_bytes):
        from warcio.warcwriter import WARCWriter
        from warcio.statusandheaders import StatusAndHeaders
        self._WARCWriter = WARCWriter
        self._StatusAndHeaders = StatusAndHeaders
        self.directory = directory
        self.prefix = prefix
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._index = 0
        os.makedirs(directory, exist_ok=True)

    def _open_next(self):
        if self._file is not None:
            self._file.close()
        path = os.path.join(self.directory, f"{self.prefix}-{self._index:05d}.warc.gz")
        self._index += 1
        self._file = open(path, "wb")
        self._writer = self._WARCWriter(self._file, gzip=True)

    def write_response(self, url, status, reason, http_version, headers, body):
        clean = [(k, v) for k, v in headers if k.lower() not in _DROP_HEADERS]
        clean.append(("Content-Length", str(len(body))))
        mime = next((v for k, v in headers if k.lower() == "content-type"), "")
        http_headers = self._StatusAndHeaders(f"{status} {reason or ''}".strip(), clean,
                                              protocol=http_version)
        with self._lock:
            if self._file is None or self._file.tell() >= self.max_bytes:
                self._open_next()
            record = self._writer.create_warc_record(
                url, "response",
                payload=BytesIO(body),
                http_headers=http_headers,
                warc_headers_dict={"WARC-Identified-Payload-Type": mime.split(";")[0].strip()},
            )
            self._writer.write_record(record)
            self._file.flush()

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


class RunLog:
    """Text outputs of a run, written by the main process only."""

    def __init__(self, output_dir, resume):
        self.dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.done_path = os.path.join(output_dir, "done.txt")
        if not resume and os.path.exists(self.done_path):
            os.replace(self.done_path, self.done_path + f".{int(time.time())}.bak")
        self.done_hosts = set()
        if os.path.exists(self.done_path):
            with open(self.done_path, encoding="utf-8") as f:
                self.done_hosts = {line.strip() for line in f if line.strip()}
        self._visited = open(os.path.join(output_dir, "visited.tsv"), "a", encoding="utf-8")
        self._failed = open(os.path.join(output_dir, "failed.tsv"), "a", encoding="utf-8")
        self._done = open(self.done_path, "a", encoding="utf-8")
        self._last_flush = time.monotonic()

    def visited(self, url, status, nbytes):
        self._visited.write(f"{url}\t{status}\t{nbytes}\n")
        self._maybe_flush()

    def failed(self, url, reason):
        self._failed.write(f"{url}\t{reason}\n")
        self._maybe_flush()

    def mark_done(self, host):
        self.done_hosts.add(host)
        self._done.write(host + "\n")
        self._done.flush()

    def _maybe_flush(self):
        now = time.monotonic()
        if now - self._last_flush > 2:
            self.flush()
            self._last_flush = now

    def flush(self):
        for f in (self._visited, self._failed, self._done):
            f.flush()

    def close(self):
        for f in (self._visited, self._failed, self._done):
            f.close()
