"""Internet Archive (archive.org) -> item lists per language.

Uses the public Scrape API (no extra package, works with --proxy):
    https://archive.org/services/search/v1/scrape?q=mediatype:(texts) AND language:("hindi")
Writes <out_dir>/<language>_texts.csv with identifier, title, language, creator, date,
addeddate, collection, downloads, formats.  Existing identifiers are skipped (resumable).

--full-metadata also saves https://archive.org/metadata/<id> for every item to
<language>_metadata.jsonl (slow: one request per item).

Then download with:  main.py download archive-org --in archive_org/hindi_texts.csv
"""
import csv
import json
import os
import time
from urllib.parse import quote

SCRAPE = "https://archive.org/services/search/v1/scrape?q={q}&fields={fields}&count=1000"
FIELDS = ["identifier", "title", "language", "creator", "date", "addeddate", "collection",
          "downloads", "format"]

LANGUAGES = ["urdu", "sanskrit", "hindi", "bengali", "tamil", "telugu", "english", "punjabi",
             "kannada", "malayalam", "marathi", "sindhi", "gujarati", "nepali", "pashto", "assamese",
             "kashmiri", "burmese", "maithili", "konkani", "bodo", "dogri", "odia", "manipuri",
             "Mandarin"]


def _cell(v):
    if isinstance(v, list):
        return "; ".join(map(str, v))
    return "" if v is None else str(v)


def harvest_language(client, lang, out_dir, full_metadata=False, delay=0.5, limit=0):
    csv_path = os.path.join(out_dir, f"{lang.lower()}_texts.csv")
    existing = set()
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8", newline="") as f:
            existing = {r["identifier"] for r in csv.DictReader(f) if r.get("identifier")}
    q = quote(f'mediatype:(texts) AND language:("{lang}")')
    base = SCRAPE.format(q=q, fields=",".join(FIELDS))
    new = not os.path.exists(csv_path)
    meta_f = open(os.path.join(out_dir, f"{lang.lower()}_metadata.jsonl"), "a",
                  encoding="utf-8") if full_metadata else None
    saved = 0
    cursor = None
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS[:-1] + ["formats"])
        while True:
            url = base + (f"&cursor={quote(cursor)}" if cursor else "")
            with client.get(url) as r:
                r.raise_for_status()
                data = r.json()
            for it in data.get("items", []):
                ident = it.get("identifier")
                if not ident or ident in existing:
                    continue
                existing.add(ident)
                w.writerow([_cell(it.get(k)) for k in FIELDS])
                saved += 1
                if meta_f is not None:
                    try:
                        with client.get(f"https://archive.org/metadata/{quote(ident)}") as mr:
                            meta_f.write(json.dumps({"identifier": ident, "metadata": mr.json()},
                                                    ensure_ascii=False) + "\n")
                    except Exception as e:
                        print(f"    metadata {ident}: {e}")
                    time.sleep(delay)
                if limit and saved >= limit:
                    break
            f.flush()
            cursor = data.get("cursor")
            print(f"  [{lang}] saved {saved} (total in archive: {data.get('total', '?')})", flush=True)
            if not cursor or (limit and saved >= limit):
                break
            time.sleep(delay)
    if meta_f:
        meta_f.close()
    return lang, saved


def run(client, out_dir, languages, full_metadata=False, delay=0.5, threads=4, limit=0):
    import concurrent.futures as cf
    os.makedirs(out_dir, exist_ok=True)
    print(f"archive.org: {len(languages)} languages, {threads} threads -> {out_dir}")

    def job(lang):
        try:
            return harvest_language(client, lang, out_dir, full_metadata, delay, limit)
        except Exception as e:
            print(f"  [{lang}] ERROR {e}")
            return lang, 0

    with cf.ThreadPoolExecutor(threads) as ex:
        for lang, n in ex.map(job, languages):
            print(f"Finished {lang}: {n} new items")
    return 0
