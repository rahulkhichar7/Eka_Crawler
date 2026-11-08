"""CPU / RAM controls shared by every multi-process pipeline."""
import logging
import os
import sys

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

log = logging.getLogger("eka.resources")


def add_resource_args(parser, workers=2, threads=None):
    g = parser.add_argument_group("Resources")
    g.add_argument("--workers", type=int, default=workers,
                   help="Worker processes (each uses at most ~1 CPU core).")
    if threads is not None:
        g.add_argument("--threads", type=int, default=threads,
                       help="Concurrent network tasks per worker.")
    g.add_argument("--cpus", default="", help="Pin to these CPU cores, e.g. '0,1' or '2-5'.")
    g.add_argument("--low-priority", default=True, action=_bool(),
                   help="Below-normal OS priority so the PC stays responsive.")
    g.add_argument("--min-free-ram-mb", type=int, default=1024,
                   help="Pause new work while free system RAM is below this. 0 = off.")
    return g


def _bool():
    import argparse
    return argparse.BooleanOptionalAction


def parse_cpu_list(text):
    """'0,2-4' -> [0, 2, 3, 4]."""
    cpus = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo > hi:
                raise ValueError(f"bad range '{part}'")
            cpus.update(range(lo, hi + 1))
        else:
            cpus.add(int(part))
    if not cpus:
        raise ValueError("empty CPU list")
    total = os.cpu_count() or 1
    wrong = [c for c in cpus if c < 0 or c >= total]
    if wrong:
        raise ValueError(f"this PC has cores 0-{total - 1}, got {wrong}")
    return sorted(cpus)


def apply_process_limits(cpus="", low_priority=True):
    """Pin the current process and lower its priority (best effort)."""
    if cpus:
        if psutil is None:
            log.warning("psutil not installed: --cpus ignored")
        else:
            try:
                psutil.Process().cpu_affinity(parse_cpu_list(cpus))
            except Exception as e:
                log.warning(f"could not pin to CPUs {cpus}: {e}")
    if low_priority:
        try:
            if sys.platform == "win32":
                if psutil is not None:
                    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                os.nice(10)
        except Exception as e:
            log.warning(f"could not lower priority: {e}")


def free_ram_mb():
    if psutil is None:
        return None
    return psutil.virtual_memory().available / (1024 * 1024)


def process_ram_mb(pids):
    if psutil is None:
        return None
    total = 0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_info().rss
        except Exception:
            pass
    return total / (1024 * 1024)


def wait_for_ram(min_free_mb, stop=None, poll=5.0, who=""):
    """Block while free RAM < min_free_mb. Returns early if `stop` is set."""
    if not min_free_mb or min_free_mb <= 0:
        return
    import time
    warned = False
    while stop is None or not stop.is_set():
        free = free_ram_mb()
        if free is None or free >= min_free_mb:
            return
        if not warned:
            log.warning(f"{who} only {free:.0f} MB RAM free (< {min_free_mb}); pausing")
            warned = True
        if stop is not None:
            stop.wait(poll)
        else:
            time.sleep(poll)


def validate_resource_args(args, parser):
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if getattr(args, "threads", 1) < 1:
        parser.error("--threads must be at least 1")
    if args.cpus:
        try:
            parse_cpu_list(args.cpus)
        except ValueError as e:
            parser.error(f"--cpus: {e}")
