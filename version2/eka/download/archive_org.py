"""Download archive.org items (PDF + OCR text by default).

Input: CSVs from  main.py pdf-urls archive-org  (identifier column), or txt identifiers.
For each item: GET https://archive.org/metadata/<id>, pick files whose format matches
--formats, download them to <out>/<id>/ and write metadata.json next to them.
--require-text skips items that have no OCR/plain-text file (the old rule).
Resumable through <out>/done.txt.
"""
import concurrent.futures as cf
import csv
import json
import os
import threading
from urllib.parse import quote

from eka.core.files import DoneLog, iter_url_lines, safe_filename

TEXT_HINTS = ("djvutxt", "txt", "text", "ocr", "hocr")


def _identifiers(inputs):
    for path in inputs:
        if path.lower().endswith(".csv"):
            with open(path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("identifier"):
                        yield row["identifier"].strip()
        else:
            yield from iter_url_lines([path])


def run(client, inputs, out_dir, formats=("pdf", "djvutxt", "txt"), require_text=True,
        threads=4, max_mb=500):
    os.makedirs(out_dir, exist_ok=True)
    done = DoneLog(os.path.join(out_dir, "done.txt"))
    ids = [i for i in dict.fromkeys(_identifiers(inputs)) if i not in done]
    print(f"archive.org: {len(ids)} items to fetch ({len(done)} already done)")
    lock = threading.Lock()
    stats = {"items": 0, "files": 0, "skipped": 0, "failed": 0}
    wanted = tuple(f.lower() for f in formats)

    def job(ident):
        try:
            with client.get(f"https://archive.org/metadata/{quote(ident)}") as r:
                meta = r.json()
        except Exception as e:
            return ident, "failed", str(e)
        files = meta.get("files", [])
        has_text = any(any(h in (f.get("format", "") + f.get("name", "")).lower() for h in TEXT_HINTS)
                       for f in files)
        if require_text and not has_text:
            return ident, "skipped", "no_text"
        chosen = [f for f in files if any(w in f.get("format", "").lower() or
                                          f.get("name", "").lower().endswith("." + w) for w in wanted)]
        if not chosen:
            return ident, "skipped", "no_matching_format"
        folder = os.path.join(out_dir, safe_filename(ident))
        os.makedirs(folder, exist_ok=True)
        n = 0
        for f in chosen:
            name = f["name"]
            dest = os.path.join(folder, safe_filename(name, 200))
            if os.path.exists(dest):
                n += 1
                continue
            ok, info = client.download(f"https://archive.org/download/{quote(ident)}/{quote(name)}",
                                       dest, max_bytes=int(max_mb * 1024 * 1024))
            n += ok
        with open(os.path.join(folder, "metadata.json"), "w", encoding="utf-8") as fh:
            json.dump(meta.get("metadata", {}), fh, ensure_ascii=False, indent=1)
        return ident, "ok", n

    with cf.ThreadPoolExecutor(threads) as ex:
        for i, (ident, status, info) in enumerate(ex.map(job, ids), 1):
            with lock:
                if status == "ok":
                    stats["items"] += 1
                    stats["files"] += info
                    done.add(ident, "ok")
                elif status == "skipped":
                    stats["skipped"] += 1
                    done.add(ident, info)
                else:
                    stats["failed"] += 1
            if i % 20 == 0:
                print(f"  {i}/{len(ids)} {stats}", flush=True)
    done.close()
    print(f"Done: {stats} -> {out_dir}")
    return 0
