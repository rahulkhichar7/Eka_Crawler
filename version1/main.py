"""Eka Crawler entry point.

    python main.py                       # defaults from config.py
    python main.py --workers 4 --threads 8
    python main.py --dry-run             # show plan and RAM estimate only
    python main.py --help                # every option
"""
import logging
import multiprocessing as mp
import os
import queue
import signal
import sys
import time

from config import parse_args
from crawler import worker_main
from utils import (apply_process_limits, estimate_ram_mb, fmt_duration, free_ram_mb,
                   load_seeds, process_ram_mb, setup_logging, site_key)
from writers import RunLog

log = logging.getLogger("crawler")


def say(msg):
    print(msg, flush=True)
    log.info(msg)


class Stats:
    def __init__(self, total):
        self.total = total
        self.started = time.monotonic()
        self.pages = self.failed = self.bytes = 0
        self.finished = self.interrupted = self.errors = 0
        self.active = set()

    def line(self, pids):
        elapsed = time.monotonic() - self.started
        rate = self.pages / elapsed if elapsed else 0
        mem = process_ram_mb(pids)
        free = free_ram_mb()
        mem_txt = f" | crawler RAM {mem:.0f} MB" if mem is not None else ""
        free_txt = f" | free RAM {free / 1024:.1f} GB" if free is not None else ""
        return (f"[{fmt_duration(elapsed)}] domains {self.finished}/{self.total} done, "
                f"{len(self.active)} active | pages {self.pages} ({rate:.1f}/s, "
                f"{self.bytes / 1e6:.0f} MB) | failed {self.failed}{mem_txt}{free_txt}")


def handle_event(msg, stats, runlog):
    kind = msg[0]
    if kind == "page":
        _, site, url, status, nbytes = msg
        stats.pages += 1
        stats.bytes += nbytes
        runlog.visited(url, status, nbytes)
    elif kind == "fail":
        _, site, url, reason = msg
        stats.failed += 1
        runlog.failed(url, reason)
    elif kind == "domain_start":
        stats.active.add(msg[1])
    elif kind == "domain_done":
        _, site, pages, fails, status = msg
        stats.active.discard(site)
        if status == "finished":
            stats.finished += 1
            runlog.mark_done(site)
        elif status == "interrupted":
            stats.interrupted += 1
        else:
            stats.errors += 1
            log.warning(f"{site}: {status}")


def main():
    cfg = parse_args()
    os.makedirs(cfg.logs_dir, exist_ok=True)
    setup_logging(os.path.join(cfg.logs_dir, "main.log"), cfg.log_level, console=True)

    if not os.path.exists(cfg.seed_file):
        say(f"Seed file not found: {cfg.seed_file}")
        return 1
    seeds, dupes, invalid = load_seeds(cfg.seed_file, cfg.keep_query)
    runlog = RunLog(cfg.output_dir, cfg.resume)
    todo = [s for s in seeds if site_key(s) not in runlog.done_hosts]

    cores = os.cpu_count() or 1
    say(f"Seeds: {len(seeds)} unique sites ({dupes} duplicates, {invalid} invalid skipped); "
        f"{len(seeds) - len(todo)} already done; {len(todo)} to crawl.")
    say(f"Resources: {cfg.workers} worker processes x {cfg.threads} threads = "
        f"{cfg.workers * cfg.threads} sites at once | CPU cores: "
        f"{cfg.cpus or 'any'} (PC has {cores}) | est. peak RAM ~{estimate_ram_mb(cfg):.0f} MB")
    say(f"Limits: depth {cfg.max_depth}, {cfg.max_pages_per_domain or 'unlimited'} pages/site, "
        f"{cfg.crawl_delay}s delay, max page {cfg.max_page_mb} MB, output -> {cfg.output_dir}")
    if cfg.workers > cores:
        say(f"Warning: --workers {cfg.workers} is more than your {cores} CPU cores.")

    if cfg.dry_run or not todo:
        runlog.close()
        if not todo:
            say("Nothing to crawl. Use --no-resume to crawl everything again.")
        return 0

    apply_process_limits(cfg)
    ctx = mp.get_context("spawn")  # same behaviour on Windows, Linux and macOS
    task_queue = ctx.Queue()
    events = ctx.Queue(maxsize=20000)  # bounded: workers wait if main falls behind
    stop = ctx.Event()

    for seed in todo:
        task_queue.put(seed)
    for _ in range(cfg.workers * cfg.threads):
        task_queue.put(None)  # one "no more work" marker per thread

    run_id = time.strftime("%Y%m%d-%H%M%S")
    procs = [ctx.Process(target=worker_main, name=f"worker-{i}",
                         args=(i, cfg, run_id, task_queue, events, stop))
             for i in range(cfg.workers)]

    ctrl = {"presses": 0}

    def on_interrupt(signum, frame):
        ctrl["presses"] += 1
        if ctrl["presses"] == 1:
            print(f"\nStopping: saving each site's progress (up to {cfg.shutdown_grace:.0f}s). "
                  f"Press Ctrl+C again to force quit.", flush=True)
            stop.set()
        else:
            print("\nForce quitting.", flush=True)

    signal.signal(signal.SIGINT, on_interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_interrupt)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_interrupt)

    for p in procs:
        p.start()
    pids = [os.getpid()] + [p.pid for p in procs]
    say(f"Started {len(procs)} workers. Press Ctrl+C to stop (progress is saved).")

    stats = Stats(len(todo))
    last_progress = time.monotonic()
    deadline = None
    try:
        while any(p.is_alive() for p in procs):
            try:
                handle_event(events.get(timeout=0.5), stats, runlog)
                for _ in range(1000):  # drain bursts quickly
                    handle_event(events.get_nowait(), stats, runlog)
            except queue.Empty:
                pass
            now = time.monotonic()
            if stop.is_set() and deadline is None:
                deadline = now + cfg.shutdown_grace
            if ctrl["presses"] > 1 or (deadline is not None and now > deadline):
                for p in procs:
                    if p.is_alive():
                        p.terminate()
                break
            if now - last_progress >= cfg.progress_every:
                print(stats.line(pids), flush=True)
                runlog.flush()
                last_progress = now
    finally:
        try:
            while True:
                handle_event(events.get_nowait(), stats, runlog)
        except Exception:
            pass
        for p in procs:
            p.join(timeout=5)
        task_queue.cancel_join_thread()
        events.cancel_join_thread()
        runlog.close()

    say(stats.line(pids))
    if stop.is_set():
        say(f"Stopped. {stats.interrupted} sites saved mid-crawl; run the same command again to resume.")
    else:
        say(f"Done. {stats.finished} sites finished, {stats.errors} errors.")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
