"""main.py text <warc|commoncrawl>  -  WARC/WET -> clean text files (parquet or jsonl).

warc         your own crawled WARCs (output/crawl/warc) -> one text file per WARC.
commoncrawl  download Common Crawl WET (ready text) or WARC (HTML) files of a snapshot,
             turn them into text files, optionally delete the raw download.

Output rows: URIS, DATA (text), LANG, WARC_DATE  - the input format of  main.py dataset combine.
One worker process per input file; a finished output file is skipped next time (resumable).
"""
import argparse
import concurrent.futures as cf
import gzip
import os
import signal
import time

from eka.core.files import safe_filename
from eka.core.http import HttpClient, add_http_args
from eka.core.resources import add_resource_args, apply_process_limits, validate_resource_args
from eka.text.extract import Sink, extractor_name, html_to_text, pick_format

CC = "https://data.commoncrawl.org/"


def register(sub):
    p = sub.add_parser("text", help="Extract clean text from WARC / Common Crawl.",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)

    w = s.add_parser("warc", help="Your WARC files -> text (trafilatura).")
    w.add_argument("--warc", nargs="+", default=["output/crawl/warc"], help="WARC files or folders")
    w.add_argument("--out-dir", default="output/text/crawl")
    _common(w)
    w.set_defaults(func=cmd_warc)

    c = s.add_parser("commoncrawl", help="Common Crawl snapshot -> text.")
    c.add_argument("--snapshot", required=True, help="e.g. CC-MAIN-2025-18")
    c.add_argument("--kind", choices=["wet", "warc"], default="wet",
                   help="wet = CC's own extracted text (fast); warc = HTML through trafilatura (better)")
    c.add_argument("--max-files", type=int, default=10, help="how many files of the snapshot (0 = all)")
    c.add_argument("--skip-files", type=int, default=0, help="start at this index in the path list")
    c.add_argument("--languages", default="",
                   help="WET only: keep records whose CC language tag contains one of these, e.g. 'hin,ben,tam'")
    c.add_argument("--raw-dir", default="output/commoncrawl/raw")
    c.add_argument("--out-dir", default="output/text/commoncrawl")
    c.add_argument("--keep-raw", action="store_true", help="keep downloaded .gz files (default: delete)")
    add_http_args(c, timeout=120, retries=3)
    _common(c)
    c.set_defaults(func=cmd_commoncrawl)


def _common(p):
    p.add_argument("--format", choices=["auto", "parquet", "jsonl"], default="auto",
                   help="auto = parquet if pyarrow is installed, else jsonl")
    p.add_argument("--min-chars", type=int, default=200, help="drop texts shorter than this")
    p.add_argument("--blocklist", default="", help="drop URLs in these blocklist categories (off)")
    add_resource_args(p, workers=2)


# ------------------------------------------------------------------ workers (run in child processes)
_BL = None


def _init_worker(cpus, low_priority, blocklist):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    apply_process_limits(cpus, low_priority)
    global _BL
    if blocklist:
        from eka.blocklist.blocklist import Blocklist
        _BL = Blocklist.load(None, blocklist)


def _warc_to_text(job):
    src, base, fmt, min_chars = job
    from eka.core.warc import iter_responses
    sink = Sink(base, fmt)
    kept = dropped = 0
    t0 = time.time()
    try:
        from eka.core.html import charset_from_content_type
        for url, http, body, wh in iter_responses(src, html_only=True):
            if _BL is not None and _BL.match(url):
                dropped += 1
                continue
            if wh.get_header("WARC-Type") == "conversion":
                text = body.decode("utf-8", "replace")
            else:
                text = html_to_text(body, encoding=charset_from_content_type(http.get("content-type")))
            if len(text) < min_chars:
                dropped += 1
                continue
            sink.write(url, text, wh.get_header("WARC-Identified-Content-Language") or "",
                       wh.get_header("WARC-Date") or "")
            kept += 1
        path = sink.close()
        return src, path, kept, dropped, time.time() - t0, None
    except Exception as e:
        sink.abort()
        return src, None, kept, dropped, time.time() - t0, str(e)


def _wet_to_text(job):
    src, base, fmt, min_chars, langs = job
    from warcio.archiveiterator import ArchiveIterator
    sink = Sink(base, fmt)
    kept = dropped = 0
    t0 = time.time()
    try:
        with open(src, "rb") as f:
            for rec in ArchiveIterator(f):
                if rec.rec_type != "conversion":
                    continue
                url = rec.rec_headers.get_header("WARC-Target-URI") or ""
                lang = rec.rec_headers.get_header("WARC-Identified-Content-Language") or ""
                if langs and not any(l in lang for l in langs):
                    dropped += 1
                    continue
                if _BL is not None and _BL.match(url):
                    dropped += 1
                    continue
                text = rec.content_stream().read().decode("utf-8", "replace")
                if len(text) < min_chars:
                    dropped += 1
                    continue
                sink.write(url, text, lang, rec.rec_headers.get_header("WARC-Date") or "")
                kept += 1
        path = sink.close()
        return src, path, kept, dropped, time.time() - t0, None
    except Exception as e:
        sink.abort()
        return src, None, kept, dropped, time.time() - t0, str(e)


def _run_pool(args, fn, jobs):
    validate_resource_args(args, argparse.ArgumentParser())
    done = 0
    with cf.ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                                initargs=(args.cpus, args.low_priority, args.blocklist)) as ex:
        try:
            for src, path, kept, dropped, secs, err in ex.map(fn, jobs):
                done += 1
                print(f"  [{done}/{len(jobs)}] {os.path.basename(src)}: {kept} texts, {dropped} dropped, "
                      f"{secs:.0f}s" + (f"  ERROR {err}" if err else f" -> {os.path.basename(path)}"),
                      flush=True)
        except KeyboardInterrupt:
            print("Stopping (finished files are kept; run again to continue)...")
            ex.shutdown(wait=False, cancel_futures=True)
            return 1
    return 0


def cmd_warc(args):
    from eka.core.warc import list_warc_files
    fmt = pick_format(args.format)
    files = list_warc_files(args.warc)
    jobs = []
    for src in files:
        base = os.path.join(args.out_dir, safe_filename(os.path.basename(src)))
        if not os.path.exists(base + (".parquet" if fmt == "parquet" else ".jsonl")):
            jobs.append((src, base, fmt, args.min_chars))
    print(f"{len(files)} WARC files, {len(files) - len(jobs)} already done; extractor={extractor_name()}, "
          f"format={fmt}, workers={args.workers}")
    if extractor_name() != "trafilatura":
        print("  note: trafilatura not installed - using the simpler lxml text fallback")
    return _run_pool(args, _warc_to_text, jobs) if jobs else 0


def cmd_commoncrawl(args):
    fmt = pick_format(args.format)
    client = HttpClient.from_args(args)
    os.makedirs(args.raw_dir, exist_ok=True)
    index = os.path.join(args.raw_dir, f"{args.snapshot}.{args.kind}.paths")
    if not os.path.exists(index):
        url = f"{CC}crawl-data/{args.snapshot}/{args.kind}.paths.gz"
        print(f"Downloading index {url}")
        with client.get(url) as r:
            r.raise_for_status()
            data = gzip.decompress(r.content)
        with open(index, "wb") as f:
            f.write(data)
    with open(index, encoding="utf-8") as f:
        paths = [l.strip() for l in f if l.strip()]
    paths = paths[args.skip_files:]
    if args.max_files:
        paths = paths[:args.max_files]
    langs = [l.strip() for l in args.languages.split(",") if l.strip()]
    out_dir = os.path.join(args.out_dir, args.snapshot)
    print(f"{args.snapshot}: {len(paths)} {args.kind.upper()} files -> {out_dir} (format {fmt})")

    todo = []
    for p in paths:
        base = os.path.join(out_dir, os.path.basename(p))
        if not os.path.exists(base + (".parquet" if fmt == "parquet" else ".jsonl")):
            todo.append((p, base))
    print(f"  {len(paths) - len(todo)} already processed")

    # download one file, process it in the pool, delete raw unless --keep-raw;
    # downloads run in a thread pool sized like the worker pool so they overlap with processing
    validate_resource_args(args, argparse.ArgumentParser())
    fn = _wet_to_text if args.kind == "wet" else _warc_to_text
    stats = {"ok": 0, "failed": 0}

    def fetch(item):
        p, base = item
        raw = os.path.join(args.raw_dir, args.snapshot, os.path.basename(p))
        if not os.path.exists(raw):
            ok, info = client.download(CC + p, raw)
            if not ok:
                return item, None, info
        return item, raw, None

    from collections import deque
    with cf.ThreadPoolExecutor(max(1, args.workers)) as dl, \
            cf.ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                                   initargs=(args.cpus, args.low_priority, args.blocklist)) as ex:
        downloads = deque()   # at most `workers` downloads in flight
        pending = []          # at most 2 x workers files waiting for / in processing
        items = iter(todo)

        def submit_next_download():
            item = next(items, None)
            if item is not None:
                downloads.append(dl.submit(fetch, item))

        for _ in range(args.workers):
            submit_next_download()
        while downloads:
            (p, base), raw, err = downloads.popleft().result()
            if err:
                print(f"  download failed {p}: {err}")
                stats["failed"] += 1
            else:
                job = (raw, base, fmt, args.min_chars, langs) if args.kind == "wet" else \
                      (raw, base, fmt, args.min_chars)
                pending.append(ex.submit(fn, job))
            while len(pending) >= args.workers * 2:
                pending = _drain(pending, args, stats, block=True)
            pending = _drain(pending, args, stats)
            submit_next_download()
        _drain(pending, args, stats, all_=True)
    print(f"Done: {stats}")
    return 0


def _drain(pending, args, stats, block=False, all_=False):
    if not pending:
        return pending
    if all_:
        done = pending
    elif block:
        done, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
    else:
        done = [f for f in pending if f.done()]
    for fut in list(done):
        src, path, kept, dropped, secs, err = fut.result()
        stats["failed" if err else "ok"] += 1
        print(f"  {os.path.basename(src)}: {kept} texts, {dropped} dropped, {secs:.0f}s"
              + (f" ERROR {err}" if err else ""), flush=True)
        if not err and not args.keep_raw:
            try:
                os.remove(src)
            except OSError:
                pass
    return [f for f in pending if f not in done]
