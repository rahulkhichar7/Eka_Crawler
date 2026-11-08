"""File helpers: logging, URL-list readers, resumable 'done' logs, safe names."""
import csv
import hashlib
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def setup_logging(log_path, level="INFO", console_level="WARNING"):
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(processName)s/%(threadName)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console_level:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(console_level)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(ch)
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def safe_filename(text, max_len=150):
    """Readable, filesystem-safe name. Long names get a short hash so they stay unique."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    if len(name) > max_len:
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
        name = name[: max_len - 11] + "_" + digest
    return name or hashlib.sha1(text.encode()).hexdigest()[:16]


def iter_url_lines(paths, columns=None):
    """Yield URLs from .txt (one per line) and .csv/.tsv files.

    For CSV/TSV: if `columns` is given, read those columns; otherwise every cell
    that starts with http(s):// is yielded (handles the old curated sheets).
    """
    for path in _expand(paths):
        low = path.lower()
        if low.endswith((".csv", ".tsv")):
            delim = "\t" if low.endswith(".tsv") else ","
            with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
                reader = csv.reader(f, delimiter=delim)
                header = next(reader, None)
                idx = None
                if columns and header:
                    lower = [h.strip().lower() for h in header]
                    idx = [lower.index(c.lower()) for c in columns if c.lower() in lower]
                elif header:
                    yield from (c.strip() for c in header if c.strip().lower().startswith("http"))
                for row in reader:
                    cells = [row[i] for i in idx if i < len(row)] if idx is not None else row
                    for c in cells:
                        c = c.strip()
                        if c and (idx is not None or c.lower().startswith("http")):
                            yield c
        else:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        yield line


def _expand(paths):
    """Files as given; folders expanded to their .txt/.csv/.tsv files (recursive)."""
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in sorted(files):
                    if name.lower().endswith((".txt", ".csv", ".tsv")):
                        yield os.path.join(root, name)
        elif os.path.exists(p):
            yield p
        else:
            logging.getLogger("eka").warning(f"not found, skipped: {p}")


class DoneLog:
    """Append-only file of finished keys. Makes any batch job resumable:

        done = DoneLog("output/done.txt")
        if key in done: skip
        ... work ...
        done.add(key)
    """

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._set = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._set = {l.rstrip("\n").split("\t", 1)[0] for l in f if l.strip()}
        self._f = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def __contains__(self, key):
        return key in self._set

    def __len__(self):
        return len(self._set)

    def add(self, key, extra=""):
        with self._lock:
            self._set.add(key)
            self._f.write(key + (f"\t{extra}" if extra else "") + "\n")
            if time.monotonic() - self._last > 2:
                self._f.flush()
                self._last = time.monotonic()

    def close(self):
        with self._lock:
            self._f.close()


class TsvAppender:
    """Thread-safe appending writer for result tables."""

    def __init__(self, path, header):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", encoding="utf-8", newline="")
        self._w = csv.writer(self._f, delimiter="\t" if path.endswith(".tsv") else ",")
        if new:
            self._w.writerow(header)
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def write(self, row):
        with self._lock:
            self._w.writerow(row)
            if time.monotonic() - self._last > 2:
                self._f.flush()
                self._last = time.monotonic()

    def close(self):
        with self._lock:
            self._f.close()


def fmt_duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
