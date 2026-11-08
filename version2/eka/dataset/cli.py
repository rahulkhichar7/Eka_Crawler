"""main.py dataset combine  -  many text parquets -> big final parquets with one schema.

Final schema (same as the old Eka / ParamAnanta combiners):
    ID (int64, global running id), SOURCE (EC = Eka Crawl, CC = Common Crawl, PDF, ...),
    TIME (timestamp), REMARK (input file name), URI, DATA

- streams row-group by row-group (never loads a whole input file into RAM)
- output rotates at --max-gb (size of inputs added), files named ID_<first>_<last>.parquet
- resumable: finished inputs are listed in <out-dir>/added_files.txt, and the next ID is
  stored in <out-dir>/state.json so a second run continues the numbering
- --dedupe-uri drops rows whose URI was already written (SQLite on disk, flat RAM)
"""
import argparse
import json
import os
import re
import sqlite3
from datetime import datetime


def register(sub):
    p = sub.add_parser("dataset", help="Combine text parquets into the final dataset schema.")
    s = p.add_subparsers(dest="cmd", required=True)
    c = s.add_parser("combine", help="Combine parquet/jsonl text files.", description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_argument("--in", dest="inputs", nargs="+", required=True, help="files or folders")
    c.add_argument("--out-dir", default="output/dataset")
    c.add_argument("--source", default="EC", help="SOURCE column value (EC, CC, PDF, ...)")
    c.add_argument("--time", default="file",
                   help="'file' = date in the file name (CC-style ...-20181209211547-...) or file mtime; "
                        "'now'; or a fixed date like 2025-07-25")
    c.add_argument("--start-id", type=int, default=None, help="first ID (default: continue state.json, else 1)")
    c.add_argument("--max-gb", type=float, default=100, help="start a new output file after this much input")
    c.add_argument("--compression", default="snappy", choices=["snappy", "zstd", "brotli", "gzip", "none"])
    c.add_argument("--uri-column", default="URIS", help="URI column name in the inputs (URIS or URI)")
    c.add_argument("--text-column", default="DATA")
    c.add_argument("--dedupe-uri", action="store_true", help="skip rows whose URI was already written")
    c.set_defaults(func=cmd_combine)


_TS = re.compile(r"(?<!\d)(20\d{12})(?!\d)")


def _file_time(path, mode):
    if mode == "now":
        return datetime.now().replace(microsecond=0)
    if mode != "file":
        return datetime.strptime(mode[:10], "%Y-%m-%d").replace(hour=23, minute=59)
    m = _TS.search(os.path.basename(path))
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    return datetime.fromtimestamp(os.path.getmtime(path)).replace(microsecond=0)


def _inputs(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in names if n.endswith((".parquet", ".jsonl"))]
        elif p.endswith((".parquet", ".jsonl")):
            files.append(p)
    return sorted(set(files), key=os.path.getmtime)


def _batches(path, uri_col, text_col, batch=20000):
    """Yield (uris, texts) lists from parquet (row groups) or jsonl."""
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        names = pf.schema_arrow.names
        ucol = uri_col if uri_col in names else ("URI" if "URI" in names else "URIS")
        for b in pf.iter_batches(batch_size=batch, columns=[ucol, text_col]):
            yield b.column(0).to_pylist(), b.column(1).to_pylist()
    else:
        uris, texts = [], []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                uris.append(row.get(uri_col) or row.get("URI") or row.get("URIS"))
                texts.append(row.get(text_col))
                if len(uris) >= batch:
                    yield uris, texts
                    uris, texts = [], []
        if uris:
            yield uris, texts


def cmd_combine(args):
    import pyarrow as pa
    import pyarrow.parquet as pq

    os.makedirs(args.out_dir, exist_ok=True)
    added_path = os.path.join(args.out_dir, "added_files.txt")
    state_path = os.path.join(args.out_dir, "state.json")
    added = set()
    if os.path.exists(added_path):
        with open(added_path, encoding="utf-8") as f:
            added = {l.strip() for l in f if l.strip()}
    state = {"next_id": 1}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    next_id = args.start_id if args.start_id is not None else state["next_id"]

    files = [p for p in _inputs(args.inputs) if os.path.abspath(p) not in added]
    print(f"{len(files)} new input files ({len(added)} already added); first ID {next_id}")
    if not files:
        return 0

    seen = None
    if args.dedupe_uri:
        seen = sqlite3.connect(os.path.join(args.out_dir, "uris.sqlite"))
        seen.execute("CREATE TABLE IF NOT EXISTS u(k TEXT PRIMARY KEY) WITHOUT ROWID")

    schema = pa.schema([("ID", pa.int64()), ("SOURCE", pa.string()), ("TIME", pa.timestamp("s")),
                        ("REMARK", pa.string()), ("URI", pa.string()), ("DATA", pa.string())])
    comp = None if args.compression == "none" else args.compression
    max_bytes = args.max_gb * 1024 ** 3
    writer = tmp_path = None
    first_id = None
    in_bytes = 0
    in_current = []   # inputs whose rows sit in the open (unfinished) output file
    added_f = open(added_path, "a", encoding="utf-8")

    def commit_inputs():
        # an input counts as added only once its rows are in a closed, final file
        for p in in_current:
            added_f.write(os.path.abspath(p) + "\n")
        added_f.flush()
        in_current.clear()
        with open(state_path, "w") as f:
            json.dump({"next_id": next_id}, f)

    def close_writer(last_id):
        nonlocal writer
        writer.close()
        final = os.path.join(args.out_dir, f"ID_{first_id}_{last_id}.parquet")
        os.replace(tmp_path, final)
        print(f"  wrote {final}", flush=True)
        writer = None
        commit_inputs()

    try:
        for path in files:
            t = _file_time(path, args.time)
            remark = os.path.basename(path).rsplit(".", 1)[0]
            rows_in = rows_out = 0
            for uris, texts in _batches(path, args.uri_column, args.text_column):
                rows_in += len(uris)
                keep = [(u, d) for u, d in zip(uris, texts) if d]
                if seen is not None:
                    fresh = []
                    for u, d in keep:
                        if seen.execute("INSERT OR IGNORE INTO u(k) VALUES(?)", (u,)).rowcount:
                            fresh.append((u, d))
                    keep = fresh
                if not keep:
                    continue
                n = len(keep)
                table = pa.table({
                    "ID": pa.array(range(next_id, next_id + n), pa.int64()),
                    "SOURCE": pa.array([args.source] * n, pa.string()),
                    "TIME": pa.array([t] * n, pa.timestamp("s")),
                    "REMARK": pa.array([remark] * n, pa.string()),
                    "URI": pa.array([u for u, _ in keep], pa.string()),
                    "DATA": pa.array([d for _, d in keep], pa.string()),
                }, schema=schema)
                if writer is None:
                    first_id = next_id
                    tmp_path = os.path.join(args.out_dir, f"ID_{first_id}_temp.parquet")
                    writer = pq.ParquetWriter(tmp_path, schema, compression=comp)
                writer.write_table(table)
                next_id += n
                rows_out += n
            in_bytes += os.path.getsize(path)
            in_current.append(path)
            print(f"  {os.path.basename(path)}: {rows_out}/{rows_in} rows (IDs up to {next_id - 1})", flush=True)
            if writer is not None and in_bytes >= max_bytes:
                close_writer(next_id - 1)
                if seen is not None:
                    seen.commit()
                in_bytes = 0
            elif writer is None:
                commit_inputs()  # input had no rows at all
        if writer is not None:
            close_writer(next_id - 1)
        if seen is not None:
            seen.commit()
    finally:
        # on a crash the open temp file is incomplete: its inputs are NOT marked added,
        # the URI de-dup table is NOT committed, and they are redone next run.
        added_f.close()
        if seen is not None:
            seen.close()
    print(f"Done. Next ID: {next_id}")
    return 0
