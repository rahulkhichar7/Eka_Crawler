"""HTML -> text, and row sinks (parquet or jsonl)."""
import json
import os

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

COLUMNS = ["URIS", "DATA", "LANG", "WARC_DATE"]


def extractor_name():
    return "trafilatura" if trafilatura is not None else "lxml-fallback"


def html_to_text(body, fast=True, encoding=None):
    if trafilatura is not None:
        try:
            return trafilatura.extract(body, fast=fast, deduplicate=False, include_comments=False) or ""
        except Exception:
            return ""
    from eka.core.html import parse, visible_text
    return visible_text(parse(body, encoding))


def pick_format(fmt):
    if fmt != "auto":
        return fmt
    try:
        import pyarrow  # noqa: F401
        return "parquet"
    except ImportError:
        return "jsonl"


class Sink:
    """Write rows to <base>.parquet (row groups of `batch` rows) or <base>.jsonl.
    The file is written as <name>.tmp and renamed on close() = only complete files exist."""

    def __init__(self, base, fmt="parquet", batch=5000, compression="snappy"):
        self.fmt = fmt
        self.path = base + (".parquet" if fmt == "parquet" else ".jsonl")
        self.tmp = self.path + ".tmp"
        self.batch = batch
        self.rows = []
        self.count = 0
        self._writer = None
        self._f = None
        self._compression = compression
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if fmt == "jsonl":
            self._f = open(self.tmp, "w", encoding="utf-8")

    def write(self, uri, text, lang="", date=""):
        self.count += 1
        if self.fmt == "jsonl":
            self._f.write(json.dumps({"URIS": uri, "DATA": text, "LANG": lang, "WARC_DATE": date},
                                     ensure_ascii=False) + "\n")
            return
        self.rows.append((uri, text, lang or "", date or ""))
        if len(self.rows) >= self.batch:
            self._flush()

    def _flush(self):
        if not self.rows:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq
        cols = list(zip(*self.rows))
        table = pa.table({c: list(v) for c, v in zip(COLUMNS, cols)})
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.tmp, table.schema, compression=self._compression)
        self._writer.write_table(table)
        self.rows = []

    def close(self):
        if self.fmt == "jsonl":
            self._f.close()
        else:
            self._flush()
            if self._writer is None:  # no rows: still write an empty file so the job counts as done
                import pyarrow as pa
                import pyarrow.parquet as pq
                pq.write_table(pa.table({c: pa.array([], pa.string()) for c in COLUMNS}), self.tmp)
            else:
                self._writer.close()
        os.replace(self.tmp, self.path)
        return self.path

    def abort(self):
        try:
            if self._f:
                self._f.close()
            if self._writer:
                self._writer.close()
        finally:
            if os.path.exists(self.tmp):
                os.remove(self.tmp)
