"""WARC reading/writing helpers (warcio)."""
import os
import threading
from io import BytesIO

# Transfer headers: requests already removed gzip/chunked encoding, so storing
# them would make the WARC record disagree with its payload.
_DROP_HEADERS = {"content-encoding", "transfer-encoding", "content-length"}


def list_warc_files(paths):
    """All .warc / .warc.gz files under the given files/folders (sorted)."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                out += [os.path.join(root, f) for f in files if f.endswith((".warc", ".warc.gz"))]
        elif p.endswith((".warc", ".warc.gz")) and os.path.exists(p):
            out.append(p)
    return sorted(out)


def iter_responses(path, html_only=True, max_bytes=20 * 1024 * 1024):
    """Yield (url, http_headers_dict, body_bytes, warc_headers) for response records.

    Streams the file (never loads the whole WARC into RAM).
    """
    from warcio.archiveiterator import ArchiveIterator
    with open(path, "rb") as f:
        for rec in ArchiveIterator(f):
            if rec.rec_type not in ("response", "resource", "conversion"):
                continue
            url = rec.rec_headers.get_header("WARC-Target-URI")
            if not url:
                continue
            http = {}
            if rec.http_headers is not None:
                http = {k.lower(): v for k, v in rec.http_headers.headers}
            ctype = http.get("content-type") or rec.rec_headers.get_header("Content-Type") or ""
            if html_only and rec.rec_type != "conversion" and "html" not in ctype.lower():
                continue
            body = rec.content_stream().read(max_bytes + 1)
            if len(body) > max_bytes:
                continue
            yield url, http, body, rec.rec_headers


class RotatingWarcWriter:
    """Thread-safe WARC writer that starts a new file at max_bytes (compressed)."""

    def __init__(self, directory, prefix, max_bytes):
        from warcio.warcwriter import WARCWriter
        from warcio.statusandheaders import StatusAndHeaders
        self._WARCWriter = WARCWriter
        self._SAH = StatusAndHeaders
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

    def write_response(self, url, status, reason, http_version, headers, body, extra=None):
        clean = [(_ascii(k), _ascii(v)) for k, v in headers if k.lower() not in _DROP_HEADERS]
        clean.append(("Content-Length", str(len(body))))
        mime = next((v for k, v in headers if k.lower() == "content-type"), "")
        http_headers = self._SAH(f"{status} {reason or ''}".strip(), clean, protocol=http_version)
        warc_headers = {"WARC-Identified-Payload-Type": mime.split(";")[0].strip()}
        if extra:
            warc_headers.update(extra)
        with self._lock:
            if self._file is None or self._file.tell() >= self.max_bytes:
                self._open_next()
            record = self._writer.create_warc_record(url, "response", payload=BytesIO(body),
                                                     http_headers=http_headers,
                                                     warc_headers_dict=warc_headers)
            self._writer.write_record(record)
            self._file.flush()

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def _ascii(s):
    # some servers send non-latin-1 header bytes; warcio would crash on them
    return str(s).encode("ascii", "replace").decode("ascii")
