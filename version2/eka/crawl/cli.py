"""main.py crawl  -  bounded, resumable, multi-process site crawler.

    python main.py crawl --dry-run
    python main.py crawl --workers 2 --threads 8
    python main.py crawl --no-save-html               # PDF-link discovery only
    python main.py crawl --frontier disk              # very large sites
"""
import logging
import multiprocessing as mp
import os
import queue
import signal
import time

from eka.core.files import TsvAppender, fmt_duration, iter_url_lines, setup_logging
from eka.core.resources import apply_process_limits, free_ram_mb, process_ram_mb
from eka.core.urls import has_skipped_extension, normalize_url, site_key
from eka.crawl.config import add_config_args, config_from_args, estimate_ram_mb
from eka.crawl.crawler import worker_main

log = logging.getLogger("eka.crawl")

MAX_START_URLS_PER_SITE = 1000


def register(sub):
    p = sub.add_parser("crawl", help="Crawl sites from seed.txt into WARC files (+ collect PDF URLs).",
                       description=__doc__, formatter_class=_fmt())
    add_config_args(p)
    p.set_defaults(func=lambda args: run(config_from_args(args, p)))


def _fmt():
    import argparse
    return argparse.RawDescriptionHelpFormatter


def say(msg):
    print(msg, flush=True)
    log.info(msg)


def load_seeds(path, keep_query):
    """site -> [start URLs]. Bare domains get https://. Several URLs of one site are all
    used as start points (this is how a crawl is restarted from known deep URLs)."""
    sites, invalid, dupes = {}, 0, 0
    for raw in iter_url_lines([path]):
        line = raw.split()[0]
        if "://" not in line:
            line = "https://" + line
        url = normalize_url(line, keep_query)
        if url is None or has_skipped_extension(url):
            invalid += 1
            continue
        urls = sites.setdefault(site_key(url), [])
        if url in urls:
            dupes += 1
        elif len(urls) < MAX_START_URLS_PER_SITE:
            urls.append(url)
    return sites, dupes, invalid


class RunLog:
    """Text outputs, written by the main process only."""

    def __init__(self, out, resume):
        os.makedirs(out, exist_ok=True)
        self.done_path = os.path.join(out, "done.txt")
        if not resume and os.path.exists(self.done_path):
            os.replace(self.done_path, self.done_path + f".{int(time.time())}.bak")
        self.done = set()
        if os.path.exists(self.done_path):
            with open(self.done_path, encoding="utf-8") as f:
                self.done = {l.strip() for l in f if l.strip()}
        self.visited = TsvAppender(os.path.join(out, "visited.tsv"), ["url", "status", "bytes", "lang"])
        self.failed = TsvAppender(os.path.join(out, "failed.tsv"), ["url", "reason"])
        self.pdfs = TsvAppender(os.path.join(out, "pdf_urls.csv"),
                                ["pdf_url", "parent_url", "parent_lang", "site"])
        self._done_f = open(self.done_path, "a", encoding="utf-8")

    def mark_done(self, site):
        self.done.add(site)
        self._done_f.write(site + "\n")
        self._done_f.flush()

    def close(self):
        for w in (self.visited, self.failed, self.pdfs):
            w.close()
        self._done_f.close()


class Stats:
    def __init__(self, total):
        self.total = total
        self.t0 = time.monotonic()
        self.pages = self.failed = self.bytes = self.pdfs = 0
        self.finished = self.interrupted = self.errors = 0
        self.active = set()

    def line(self, pids):
        el = time.monotonic() - self.t0
        mem, free = process_ram_mb(pids), free_ram_mb()
        return (f"[{fmt_duration(el)}] sites {self.finished}/{self.total} done, {len(self.active)} active"
                f" | pages {self.pages} ({self.pages / el if el else 0:.1f}/s, {self.bytes / 1e6:.0f} MB)"
                f" | pdf links {self.pdfs} | failed {self.failed}"
                + (f" | crawler RAM {mem:.0f} MB" if mem is not None else "")
                + (f" | free RAM {free / 1024:.1f} GB" if free is not None else ""))


def handle(msg, st, rl):
    kind = msg[0]
    if kind == "page":
        _, site, url, status, nbytes, lang = msg
        st.pages += 1
        st.bytes += nbytes
        rl.visited.write([url, status, nbytes, lang])
    elif kind == "pdf":
        _, site, url, parent, lang = msg
        st.pdfs += 1
        rl.pdfs.write([url, parent, lang, site])
    elif kind == "fail":
        st.failed += 1
        rl.failed.write([msg[2], msg[3]])
    elif kind == "domain_start":
        st.active.add(msg[1])
    elif kind == "domain_done":
        _, site, pages, fails, status = msg
        st.active.discard(site)
        if status == "finished":
            st.finished += 1
            rl.mark_done(site)
        elif status == "interrupted":
            st.interrupted += 1
        else:
            st.errors += 1
            log.warning(f"{site}: {status}")


def run(cfg):
    os.makedirs(cfg.logs_dir, exist_ok=True)
    setup_logging(os.path.join(cfg.logs_dir, "main.log"), cfg.log_level)
    if not os.path.exists(cfg.seed_file):
        say(f"Seed file not found: {cfg.seed_file}")
        return 1
    sites, dupes, invalid = load_seeds(cfg.seed_file, cfg.keep_query)
    rl = RunLog(cfg.output_dir, cfg.resume)

    blocked = 0
    if cfg.blocklist or cfg.blocklist_keywords:
        from eka.blocklist.blocklist import Blocklist
        bl = Blocklist.load(None, cfg.blocklist or "unsafe", cfg.blocklist_keywords)
        for s in list(sites):
            if bl.match(sites[s][0]):
                del sites[s]
                blocked += 1
        del bl
    todo = [(s, u) for s, u in sites.items() if s not in rl.done]

    cores = os.cpu_count() or 1
    say(f"Seeds: {len(sites)} sites ({dupes} duplicate lines, {invalid} invalid"
        + (f", {blocked} blocklisted" if blocked else "") + f"); {len(sites) - len(todo)} already done; "
        f"{len(todo)} to crawl.")
    say(f"Resources: {cfg.workers} workers x {cfg.threads} threads = {cfg.workers * cfg.threads} sites at once"
        f" | cores {cfg.cpus or 'any'} (PC has {cores}) | est. peak RAM ~{estimate_ram_mb(cfg):.0f} MB")
    say(f"Limits: depth {cfg.max_depth}, {cfg.max_pages_per_domain or 'unlimited'} pages/site, "
        f"{cfg.crawl_delay}s delay, frontier {cfg.frontier}, max page {cfg.max_page_mb} MB | "
        f"save html: {cfg.save_html}, pdf links: {cfg.collect_pdf_urls} | proxy: {cfg.proxy}"
        f" | output -> {cfg.output_dir}")
    if cfg.workers > cores:
        say(f"Warning: --workers {cfg.workers} > your {cores} CPU cores.")
    if cfg.dry_run or not todo:
        rl.close()
        if not todo:
            say("Nothing to crawl. Use --no-resume to crawl everything again.")
        return 0

    apply_process_limits(cfg.cpus, cfg.low_priority)
    ctx = mp.get_context("spawn")
    task_queue, events, stop = ctx.Queue(), ctx.Queue(maxsize=20000), ctx.Event()
    for t in todo:
        task_queue.put(t)
    for _ in range(cfg.workers * cfg.threads):
        task_queue.put(None)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    procs = [ctx.Process(target=worker_main, name=f"worker-{i}",
                         args=(i, cfg, run_id, task_queue, events, stop)) for i in range(cfg.workers)]
    ctrl = {"n": 0}

    def on_interrupt(signum, frame):
        ctrl["n"] += 1
        if ctrl["n"] == 1:
            print(f"\nStopping: saving each site's progress (up to {cfg.shutdown_grace:.0f}s). "
                  f"Ctrl+C again to force quit.", flush=True)
            stop.set()
        else:
            print("\nForce quitting.", flush=True)

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_interrupt)

    for p in procs:
        p.start()
    pids = [os.getpid()] + [p.pid for p in procs]
    say(f"Started {len(procs)} workers. Ctrl+C to stop (progress is saved).")

    st = Stats(len(todo))
    last = time.monotonic()
    deadline = None
    try:
        while any(p.is_alive() for p in procs):
            try:
                handle(events.get(timeout=0.5), st, rl)
                for _ in range(2000):
                    handle(events.get_nowait(), st, rl)
            except queue.Empty:
                pass
            now = time.monotonic()
            if stop.is_set() and deadline is None:
                deadline = now + cfg.shutdown_grace
            if ctrl["n"] > 1 or (deadline is not None and now > deadline):
                for p in procs:
                    if p.is_alive():
                        p.terminate()
                break
            if now - last >= cfg.progress_every:
                print(st.line(pids), flush=True)
                last = now
    finally:
        try:
            while True:
                handle(events.get_nowait(), st, rl)
        except Exception:
            pass
        for p in procs:
            p.join(timeout=5)
        task_queue.cancel_join_thread()
        events.cancel_join_thread()
        rl.close()

    say(st.line(pids))
    if stop.is_set():
        say(f"Stopped. {st.interrupted} sites saved mid-crawl; run the same command again to resume.")
    else:
        say(f"Done. {st.finished} sites finished, {st.errors} errors.")
    return 0
