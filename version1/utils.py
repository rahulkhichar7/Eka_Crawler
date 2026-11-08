"""Small helpers: URL handling, seed loading, logging and OS resource limits."""
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit, urlunsplit

try:
    import psutil  # optional: RAM guard, CPU pinning, priority
except ImportError:  # pragma: no cover
    psutil = None

log = logging.getLogger("crawler")

# File types that are never HTML. Skipped before any request is made.
SKIP_EXTENSIONS = frozenset("""
    .pdf .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .rtf .csv .txt .xml .json
    .zip .rar .7z .gz .tgz .tar .bz2 .xz .iso .dmg .exe .msi .apk .bin .deb .rpm
    .jpg .jpeg .png .gif .bmp .webp .svg .ico .tif .tiff .psd .heic
    .mp3 .wav .ogg .flac .aac .m4a .wma .mp4 .avi .mov .mkv .wmv .flv .webm .m4v .3gp
    .css .js .woff .woff2 .ttf .eot .otf .swf .dwg .kml .kmz .shp
""".split())


# ------------------------------------------------------------------ URLs

def normalize_url(url, keep_query=False):
    """Return a canonical absolute http(s) URL, or None if the URL is unusable.

    - lowercases scheme and host, drops default ports, drops #fragment
    - empty path becomes "/"
    - drops ?query unless keep_query is True
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = parts.path or "/"
    query = parts.query if keep_query else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def site_key(url_or_host):
    """host[:port] without a leading 'www.' so example.com and www.example.com are one site.
    Expects a URL already passed through normalize_url (default ports removed)."""
    host = urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host
    host = host.rsplit("@", 1)[-1].lower()
    return host[4:] if host.startswith("www.") else host


def url_key(url):
    """Identity of a URL for de-duplication: ignores http/https and 'www.'."""
    rest = url.split("://", 1)[-1]
    return rest[4:] if rest.startswith("www.") else rest


def has_skipped_extension(url):
    path = urlsplit(url).path.lower()
    return os.path.splitext(path)[1] in SKIP_EXTENSIONS


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:200]


def load_seeds(path, keep_query=False):
    """Read seed file. Returns (seeds, duplicates, invalid). One seed per site."""
    seeds, seen = [], set()
    duplicates = invalid = 0
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "://" not in line:
                line = "https://" + line
            url = normalize_url(line, keep_query)
            if url is None:
                invalid += 1
                continue
            key = site_key(url)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            seeds.append(url)
    return seeds, duplicates, invalid


# ------------------------------------------------------------------ resources

def parse_cpu_list(text):
    """'0,2-4' -> [0, 2, 3, 4]. Raises ValueError on bad input."""
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


def apply_process_limits(cfg):
    """Pin the current process to cfg.cpus and lower its priority (best effort)."""
    if cfg.cpus:
        if psutil is None:
            log.warning("psutil not installed: --cpus ignored")
        else:
            try:
                psutil.Process().cpu_affinity(parse_cpu_list(cfg.cpus))
            except Exception as e:  # not supported on macOS
                log.warning(f"Could not pin to CPUs {cfg.cpus}: {e}")
    if cfg.low_priority:
        try:
            if sys.platform == "win32":
                if psutil is not None:
                    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                os.nice(10)
        except Exception as e:
            log.warning(f"Could not lower priority: {e}")


def free_ram_mb():
    """Available system RAM in MB, or None if psutil is missing."""
    if psutil is None:
        return None
    return psutil.virtual_memory().available / (1024 * 1024)


def process_ram_mb(pids):
    """Total resident memory of the given processes, in MB (None without psutil)."""
    if psutil is None:
        return None
    total = 0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_info().rss
        except Exception:
            pass
    return total / (1024 * 1024)


def estimate_ram_mb(cfg):
    """Rough worst-case RAM use of a run."""
    per_thread = cfg.max_page_mb * 3 + cfg.max_frontier_per_domain * 0.0006 + 2
    per_worker = 70 + cfg.threads * per_thread
    return 60 + cfg.workers * per_worker


# ------------------------------------------------------------------ logging

def setup_logging(log_path, level="INFO", console=False):
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(ch)
    # keep library noise out of our logs
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def fmt_duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
