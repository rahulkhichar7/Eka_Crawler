"""Per-site URL frontier (queue of URLs to fetch + set of URLs already queued).

MemoryFrontier  deque + set, capped. Saved to JSON on Ctrl+C. Fast; RAM grows with the cap.
DiskFrontier    SQLite file per site (stdlib, no extra package). Queue AND seen-set live
                on disk, so a site with millions of URLs keeps RAM flat. Survives crashes
                (last committed page), resumes automatically.

Both de-duplicate when a URL is QUEUED (not when fetched), so a link that appears on
every page is queued once.
"""
import json
import os
import sqlite3
from collections import deque

from eka.core.urls import url_key


class MemoryFrontier:
    def __init__(self, cap):
        self.cap = cap
        self.q = deque()
        self.seen = set()
        self.counters = {"pages": 0, "fails": 0, "pdfs": 0}

    def add(self, url, depth, force=False):
        key = url_key(url)
        if not force:
            if key in self.seen or len(self.q) >= self.cap:
                return False
        self.seen.add(key)
        self.q.append((url, depth))
        return True

    def mark(self, key):
        """Record a key; True if it was new (used for PDF links)."""
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def pop(self):
        return self.q.popleft() if self.q else None

    def unpop(self, url, depth):
        self.q.appendleft((url, depth))

    def __len__(self):
        return len(self.q)

    def commit(self):
        pass

    # ---- persistence
    @classmethod
    def open(cls, state_path, cap, resume):
        f = cls(cap)
        if resume and os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as fh:
                    st = json.load(fh)
                f.q = deque((u, d) for u, d in st["frontier"])
                f.seen = set(st["seen"])
                f.counters.update(st.get("counters", {}))
            except Exception:
                f = cls(cap)
        return f

    def save(self, state_path):
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"frontier": list(self.q), "seen": list(self.seen),
                       "counters": self.counters}, fh)
        os.replace(tmp, state_path)

    def discard(self, state_path):
        for p in (state_path, state_path + ".tmp"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def close(self):
        pass


class DiskFrontier:
    def __init__(self, path, cap):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.cap = cap
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-2000")  # ~2 MB page cache
        self.db.execute("CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY, url TEXT, depth INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()
        self._len = self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        self.counters = {"pages": 0, "fails": 0, "pdfs": 0}
        for k, v in self.db.execute("SELECT k, v FROM meta"):
            if k in self.counters:
                self.counters[k] = int(v)

    def add(self, url, depth, force=False):
        if not force:
            if self._len >= self.cap:
                return False
            cur = self.db.execute("INSERT OR IGNORE INTO seen(k) VALUES(?)", (url_key(url),))
            if cur.rowcount == 0:
                return False
        self.db.execute("INSERT INTO queue(url, depth) VALUES(?, ?)", (url, depth))
        self._len += 1
        return True

    def mark(self, key):
        return self.db.execute("INSERT OR IGNORE INTO seen(k) VALUES(?)", (key,)).rowcount == 1

    def pop(self):
        row = self.db.execute("SELECT id, url, depth FROM queue ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        self.db.execute("DELETE FROM queue WHERE id = ?", (row[0],))
        self._len -= 1
        return row[1], row[2]

    def unpop(self, url, depth):
        first = self.db.execute("SELECT MIN(id) FROM queue").fetchone()[0]
        self.db.execute("INSERT INTO queue(id, url, depth) VALUES(?, ?, ?)",
                        ((first if first is not None else 1) - 1, url, depth))
        self._len += 1

    def __len__(self):
        return self._len

    def commit(self):
        self.db.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                            [(k, str(v)) for k, v in self.counters.items()])
        self.db.commit()

    @classmethod
    def open(cls, path, cap, resume):
        if not resume:
            cls._remove_files(path)
        return cls(path, cap)

    def save(self, _state_path=None):
        self.commit()

    def discard(self, _state_path=None):
        self.close()
        self._remove_files(self.path)

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    @staticmethod
    def _remove_files(path):
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
