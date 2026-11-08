"""main.py pdf-urls <source>  -  build lists of PDF URLs (nothing is downloaded here).

    from-warc     PDF links inside WARC files you already crawled
    shodhganga    every thesis PDF on shodhganga.inflibnet.ac.in
    ndl-iitkgp    NDL (ndl.iitkgp.ac.in) document pages -> real PDF URLs
    sansad        eparlib.sansad.in API -> JSON + PDF URLs
    archive-org   archive.org text items per language (metadata CSV)
    merge         combine any PDF URL lists, drop already-downloaded ones -> one list

The crawler also writes PDF links while crawling: output/crawl/pdf_urls.csv
(use  main.py crawl --no-save-html  for a PDF-link-only crawl of any site).
"""
import argparse
import csv
import os

from eka.core.files import iter_url_lines
from eka.core.http import HttpClient, add_http_args
from eka.core.urls import normalize_url, url_key


def register(sub):
    p = sub.add_parser("pdf-urls", help="Collect PDF URLs from many sources.", description=__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)

    w = s.add_parser("from-warc", help="PDF links inside crawled WARC files.")
    w.add_argument("--warc", nargs="+", required=True, help="WARC files or folders")
    w.add_argument("--out", default="output/pdf_urls/from_warc.csv")
    w.add_argument("--workers", type=int, default=2)
    w.set_defaults(func=lambda a: __import__("eka.pdf_urls.from_warc", fromlist=["run"]).run(
        a.warc, a.out, a.workers))

    g = s.add_parser("shodhganga", help="All thesis PDFs from Shodhganga (resumable).")
    g.add_argument("--out", default="output/pdf_urls/shodhganga_pdf_links.txt")
    g.add_argument("--start-offset", type=int, default=None, help="default: continue from .state")
    g.add_argument("--end-offset", type=int, default=None)
    g.add_argument("--delay", type=float, default=1.0, help="seconds before each request (per thread)")
    g.add_argument("--threads", type=int, default=2)
    add_http_args(g, user_agent="Mozilla/5.0 (compatible; EkaCurator/2.0)")
    g.set_defaults(func=_shodhganga)

    n = s.add_parser("ndl-iitkgp", help="Resolve NDL document pages to PDF URLs.")
    n.add_argument("--in", dest="inputs", nargs="+", required=True, help="page URL lists")
    n.add_argument("--out", default="output/pdf_urls/ndl_iitkgp.csv")
    n.add_argument("--threads", type=int, default=4)
    n.add_argument("--delay", type=float, default=0.5)
    add_http_args(n)
    n.set_defaults(func=_ndl)

    a = s.add_parser("sansad", help="eparlib.sansad.in records -> JSON + PDF URLs.")
    a.add_argument("--out-dir", default="output/pdf_urls/sansad")
    a.add_argument("--start", type=int, default=1)
    a.add_argument("--end", type=int, default=1_302_000)
    a.add_argument("--rows", type=int, default=1000)
    a.add_argument("--delay", type=float, default=1.0)
    add_http_args(a, timeout=60)
    a.set_defaults(func=_sansad)

    r = s.add_parser("archive-org", help="archive.org text items per language.")
    r.add_argument("--out-dir", default="output/pdf_urls/archive_org")
    r.add_argument("--languages", default="", help="comma list (default: 25 Indic + English languages)")
    r.add_argument("--full-metadata", action="store_true", help="also save /metadata/<id> JSON")
    r.add_argument("--threads", type=int, default=4)
    r.add_argument("--delay", type=float, default=0.5)
    r.add_argument("--limit", type=int, default=0, help="max new items per language (0 = all)")
    add_http_args(r, timeout=60, retries=3)
    r.set_defaults(func=_archive)

    m = s.add_parser("merge", help="Combine PDF URL lists; drop already-downloaded URLs.")
    m.add_argument("--in", dest="inputs", nargs="+", required=True, help="txt/csv files or folders")
    m.add_argument("--column", nargs="*", default=None,
                   help="CSV columns holding URLs (default: pdf_url if present, else every http cell)")
    m.add_argument("--exclude", nargs="*", default=[],
                   help="lists of URLs already downloaded (txt, or a downloader manifest.csv)")
    m.add_argument("--out", required=True, help="txt, one URL per line")
    m.add_argument("--pdf-only", action="store_true", help="keep only URLs whose path ends in .pdf")
    m.set_defaults(func=_merge)


def _shodhganga(a):
    from eka.pdf_urls import shodhganga
    return shodhganga.run(HttpClient.from_args(a), a.out, a.start_offset, a.end_offset, a.delay, a.threads)


def _ndl(a):
    from eka.pdf_urls import ndl_iitkgp
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    return ndl_iitkgp.run(HttpClient.from_args(a), a.inputs, a.out, a.threads, a.delay)


def _sansad(a):
    from eka.pdf_urls import sansad
    return sansad.run(HttpClient.from_args(a), a.out_dir, a.start, a.end, a.rows, a.delay)


def _archive(a):
    from eka.pdf_urls import archive_org
    langs = [l.strip() for l in a.languages.split(",") if l.strip()] or archive_org.LANGUAGES
    return archive_org.run(HttpClient.from_args(a), a.out_dir, langs, a.full_metadata, a.delay,
                           a.threads, a.limit)


def _columns_for(path, wanted):
    if wanted:
        return wanted
    if path.lower().endswith((".csv", ".tsv")):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            header = f.readline().lower()
        for c in ("pdf_url", "url", "pdf link", "pdf_link"):
            if c in [h.strip().strip('"') for h in header.replace("\t", ",").split(",")]:
                return [c]
    return None


def _expand(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.lower().endswith((".txt", ".csv", ".tsv")) and not f.endswith(".done"):
                        yield os.path.join(root, f)
        else:
            yield p


def _merge(a):
    done = set()
    for path in _expand(a.exclude):
        cols = _columns_for(path, ["url"] if path.endswith("manifest.csv") else None)
        for u in iter_url_lines([path], cols):
            done.add(url_key(u.strip()))
    seen, kept, skipped_done, bad, dupes = set(), 0, 0, 0, 0
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as out:
        for path in _expand(a.inputs):
            for u in iter_url_lines([path], _columns_for(path, a.column)):
                n = normalize_url(u, keep_query=True)
                if n is None:
                    bad += 1
                    continue
                if a.pdf_only and not n.lower().split("?")[0].endswith(".pdf"):
                    bad += 1
                    continue
                k = url_key(n)
                if k in seen:
                    dupes += 1
                    continue
                seen.add(k)
                if k in done:
                    skipped_done += 1
                    continue
                out.write(n + "\n")
                kept += 1
    print(f"{kept} new PDF URLs -> {a.out}  ({skipped_done} already downloaded, {bad} invalid, "
          f"{dupes} duplicates removed)")
    return 0
