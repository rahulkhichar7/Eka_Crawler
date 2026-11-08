"""Extract PDF links from already-crawled WARC files (no network).

Each WARC is processed by one worker process, which writes its own part file
(parts/<warc>.csv). Finished WARCs are skipped on the next run. At the end the
parts are merged into one de-duplicated CSV.
"""
import concurrent.futures as cf
import csv
import os

from eka.core.files import safe_filename
from eka.core.html import extract_links, page_lang, parse
from eka.core.urls import is_pdf_url, normalize_url, url_key
from eka.core.warc import iter_responses, list_warc_files

TAGS = ("a", "iframe", "embed", "object", "link")


def _one_warc(args):
    path, part_path = args
    tmp = part_path + ".tmp"
    n = 0
    seen = set()
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for url, _, body, _ in iter_responses(path, html_only=True):
                doc = parse(body)
                lang = page_lang(doc)
                for link in extract_links(doc, url, tags=TAGS):
                    if not is_pdf_url(link):
                        continue
                    pdf = normalize_url(link, keep_query=True)
                    if pdf and url_key(pdf) not in seen:
                        seen.add(url_key(pdf))
                        w.writerow([pdf, url, lang])
                        n += 1
        os.replace(tmp, part_path)
        return path, n, None
    except Exception as e:
        return path, n, str(e)


def run(warc_paths, out_csv, workers=2, merge=True):
    files = list_warc_files(warc_paths)
    if not files:
        print("No WARC files found.")
        return 1
    parts_dir = os.path.join(os.path.dirname(os.path.abspath(out_csv)), "parts")
    os.makedirs(parts_dir, exist_ok=True)
    jobs = []
    for p in files:
        part = os.path.join(parts_dir, safe_filename(os.path.relpath(p)) + ".csv")
        if not os.path.exists(part):
            jobs.append((p, part))
    print(f"{len(files)} WARC files, {len(files) - len(jobs)} already done, {len(jobs)} to scan "
          f"with {workers} workers")
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (path, n, err) in enumerate(ex.map(_one_warc, jobs), 1):
            print(f"  [{i}/{len(jobs)}] {os.path.basename(path)}: {n} pdf links"
                  + (f"  ERROR {err}" if err else ""), flush=True)
    if merge:
        merge_parts(parts_dir, out_csv)
    return 0


def merge_parts(parts_dir, out_csv):
    seen = set()
    total = 0
    with open(out_csv, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(["pdf_url", "parent_url", "parent_lang"])
        for name in sorted(os.listdir(parts_dir)):
            if not name.endswith(".csv"):
                continue
            with open(os.path.join(parts_dir, name), encoding="utf-8", newline="") as f:
                for row in csv.reader(f):
                    if row and url_key(row[0]) not in seen:
                        seen.add(url_key(row[0]))
                        w.writerow(row)
                        total += 1
    print(f"Merged {total} unique PDF URLs -> {out_csv}")
