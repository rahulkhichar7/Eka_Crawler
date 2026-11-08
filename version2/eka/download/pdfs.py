"""Download a list of file URLs (PDFs by default) - resumable, polite, bounded.

- N threads pull from a small bounded queue (a million URLs never sit in RAM as tasks).
- Per-host delay: two requests to the same host are at least --per-host-delay apart.
- Streams to <file>.part and renames at the end; partial files never look finished.
- Accepts a file if Content-Type says PDF *or* the body starts with %PDF.
- manifest.csv records every URL's result; finished and permanently-failed URLs are
  skipped on the next run, temporary failures (timeouts, 5xx) are retried.
"""
import csv
import hashlib
import logging
import os
import queue
import signal
import threading
import time
from urllib.parse import urlsplit

from eka.core.files import iter_url_lines, safe_filename
from eka.core.resources import wait_for_ram

log = logging.getLogger("eka.download")

PERMANENT = ("http_400", "http_401", "http_403", "http_404", "http_410", "http_451",
             "wrong_type", "too_large", "empty", "invalid_url")


def target_path(out_dir, url, ext=".pdf", by_host=True):
    """<out>/<host>/<readable-path>__<8-char hash of the full URL><ext>
    The hash keeps two different URLs from ever sharing a file name."""
    p = urlsplit(url)
    stem = safe_filename((p.path.strip("/") or "index") + (("_" + p.query) if p.query else ""), 120)
    root, e = os.path.splitext(stem)
    if ext and e.lower() == ext:
        stem = root
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
    name = f"{stem}__{digest}{ext or e}"
    return os.path.join(out_dir, safe_filename(p.netloc or "unknown"), name) if by_host \
        else os.path.join(out_dir, name)


def _load_manifest(path):
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                st = row.get("status", "")
                if st == "ok" or st.startswith(PERMANENT):
                    done[row["url"]] = st
    return done


def _iter_jobs(inputs, url_column, referer_column):
    for path in inputs:
        low = path.lower()
        if low.endswith((".csv", ".tsv")) and (url_column or referer_column):
            delim = "\t" if low.endswith(".tsv") else ","
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f, delimiter=delim):
                    url = (row.get(url_column or "pdf_url") or "").strip()
                    if url:
                        yield url, (row.get(referer_column) or "").strip() if referer_column else ""
        else:
            cols = None
            if low.endswith((".csv", ".tsv")):
                cols = ["pdf_url"] if "pdf_url" in open(path, encoding="utf-8-sig").readline() else None
            for url in iter_url_lines([path], cols):
                yield url, ""


def run(client, inputs, out_dir, threads=8, per_host_delay=0.5, max_mb=200, any_type=False,
        auto_referer=True, url_column=None, referer_column=None, by_host=True, min_free_ram_mb=1024):
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.csv")
    done = _load_manifest(manifest_path)
    new = not os.path.exists(manifest_path)
    mf = open(manifest_path, "a", encoding="utf-8", newline="")
    mw = csv.writer(mf)
    if new:
        mw.writerow(["url", "path", "bytes", "status"])
    mlock = threading.Lock()
    stop = threading.Event()
    q = queue.Queue(maxsize=threads * 4)
    host_next, host_lock = {}, threading.Lock()
    stats = {"ok": 0, "skipped": 0, "failed": 0, "already": 0}
    t0 = time.monotonic()

    def polite(host):
        while not stop.is_set():
            with host_lock:
                now = time.monotonic()
                nxt = host_next.get(host, 0)
                if now >= nxt:
                    host_next[host] = now + per_host_delay
                    return
                wait = nxt - now
            stop.wait(wait)

    def worker():
        while True:
            item = q.get()
            if item is None:
                return
            url, referer = item
            if stop.is_set():
                continue
            wait_for_ram(min_free_ram_mb, stop, who="download")
            path = target_path(out_dir, url, "" if any_type else ".pdf", by_host)
            if os.path.exists(path):
                ok, info = True, os.path.getsize(path)
            else:
                polite(urlsplit(url).hostname or "")
                ref = referer or (url.rsplit("/", 1)[0] + "/" if auto_referer else None)
                ok, info = client.download(url, path, max_bytes=int(max_mb * 1024 * 1024),
                                           content_types=None if any_type else ("pdf",),
                                           magic=None if any_type else b"%PDF", referer=ref, stop=stop)
            status = "ok" if ok else str(info)
            if status == "stopped":
                continue
            with mlock:
                mw.writerow([url, path if ok else "", info if ok else 0, status])
                stats["ok" if ok else ("skipped" if status.startswith(PERMANENT) else "failed")] += 1

    ts = [threading.Thread(target=worker, name=f"dl-{i}", daemon=True) for i in range(threads)]
    for t in ts:
        t.start()

    def on_int(sig, frame):
        if not stop.is_set():
            print("\nStopping after current downloads (Ctrl+C again to force)...", flush=True)
            stop.set()
        else:
            os._exit(1)
    old = signal.signal(signal.SIGINT, on_int)

    last = time.monotonic()
    try:
        seen = set()
        for url, ref in _iter_jobs(inputs, url_column, referer_column):
            if stop.is_set():
                break
            if url in seen:
                continue
            seen.add(url)
            if url in done:
                stats["already"] += 1
                continue
            if not url.startswith(("http://", "https://")):
                continue
            while not stop.is_set():
                try:
                    q.put((url, ref), timeout=0.5)
                    break
                except queue.Full:
                    pass
            if time.monotonic() - last > 10:
                with mlock:
                    mf.flush()
                el = time.monotonic() - t0
                print(f"  ok {stats['ok']} | skipped {stats['skipped']} | failed {stats['failed']} | "
                      f"already done {stats['already']} | {stats['ok'] / el:.1f} files/s", flush=True)
                last = time.monotonic()
    finally:
        for _ in ts:
            q.put(None)
        for t in ts:
            t.join()
        signal.signal(signal.SIGINT, old)
        mf.close()
    print(f"Done: {stats} -> {out_dir} (manifest: {manifest_path})")
    return 0
