"""Worker process and the per-domain crawler.

One worker process runs `cfg.threads` threads. Each thread takes one domain at a
time from the shared task queue and crawls it to the end (or to a limit) before
taking the next one. All per-domain state is local to that thread, so there is
no cross-process locking on the hot path.
"""
import json
import logging
import os
import queue
import signal
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

import requests
from lxml import etree
from lxml import html as lxml_html

from utils import (apply_process_limits, free_ram_mb, has_skipped_extension,
                   normalize_url, safe_filename, setup_logging, site_key, url_key)
from writers import RotatingWarcWriter

log = logging.getLogger("crawler")

RETRY_STATUS = {429, 500, 502, 503, 504}
ROBOTS_MAX_BYTES = 512 * 1024
MAX_ROBOTS_DELAY = 60.0


class DomainCrawler:
    """Breadth-first crawl of one site with hard limits on memory and time."""

    def __init__(self, seed, cfg, warc, events, stop):
        self.cfg = cfg
        self.seed = seed
        self.site = site_key(seed)
        self.warc = warc
        self.events = events
        self.stop = stop
        self.max_bytes = int(cfg.max_page_mb * 1024 * 1024)
        self.state_path = os.path.join(cfg.state_dir, safe_filename(self.site) + ".json")
        self.frontier = deque()   # (url, depth) waiting to be fetched
        self.seen = set()         # url_key of everything ever queued: no duplicates
        self.pages = 0
        self.fails = 0
        self.session = None
        self.robots = None
        self.delay = cfg.crawl_delay

    # ---------------------------------------------------------------- main loop
    def run(self):
        self.events.put(("domain_start", self.site))
        self.session = self._make_session()
        try:
            if not (self.cfg.resume and self._load_state()):
                start = normalize_url(self.seed, self.cfg.keep_query)
                self._enqueue(start, 0)
            self._load_robots()
            log.info(f"[{self.site}] start: {len(self.frontier)} queued, {self.pages} already saved, "
                     f"delay {self.delay:.2f}s")
            next_request = 0.0
            limit = self.cfg.max_pages_per_domain

            while self.frontier and not self.stop.is_set():
                if limit and self.pages >= limit:
                    log.info(f"[{self.site}] page limit {limit} reached")
                    break
                self._wait_for_ram()
                url, depth = self.frontier.popleft()
                if self.robots is not None and not self.robots.can_fetch(url, self.cfg.user_agent):
                    log.debug(f"robots.txt disallows {url}")
                    continue
                # politeness delay; returns early if Ctrl+C is pressed
                if self.stop.wait(max(0.0, next_request - time.monotonic())):
                    self.frontier.appendleft((url, depth))
                    break
                next_request = time.monotonic() + self.delay
                self._process(url, depth)

            if self.stop.is_set():
                self._save_state()
                status = "interrupted"
            else:
                self._clear_state()
                status = "finished"
            log.info(f"[{self.site}] {status}: {self.pages} pages, {self.fails} failed, "
                     f"{len(self.frontier)} left in queue")
            self.events.put(("domain_done", self.site, self.pages, self.fails, status))
        finally:
            self.session.close()

    def _process(self, url, depth):
        kind, info = self._fetch(url)
        if kind == "fail":
            self.fails += 1
            self.events.put(("fail", self.site, url, info))
            return
        if kind == "skip":
            log.debug(f"skip {url}: {info}")
            return
        final_url, resp, body = info
        if final_url != url:
            self.seen.add(url_key(final_url))  # don't fetch the redirect target again
        self.warc.write_response(final_url, resp.status_code, resp.reason,
                                 _http_version(resp), list(resp.raw.headers.items()), body)
        self.pages += 1
        self.events.put(("page", self.site, final_url, resp.status_code, len(body)))
        log.debug(f"saved {final_url} ({len(body)} bytes, depth {depth})")
        if depth < self.cfg.max_depth:
            for link in _extract_links(body, final_url):
                self._enqueue(link, depth + 1)

    # ---------------------------------------------------------------- queue
    def _enqueue(self, url, depth):
        url = normalize_url(url, self.cfg.keep_query) if url else None
        if url is None or site_key(url) != self.site or has_skipped_extension(url):
            return
        key = url_key(url)
        if key in self.seen:
            return
        if len(self.frontier) >= self.cfg.max_frontier_per_domain:
            return  # queue full: drop the link instead of growing RAM
        self.seen.add(key)
        self.frontier.append((url, depth))

    # ---------------------------------------------------------------- network
    def _make_session(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        })
        s.verify = self.cfg.verify_ssl
        s.max_redirects = 10
        return s

    def _fetch(self, url):
        """Returns ("page", (final_url, response, body)) | ("skip", why) | ("fail", why)."""
        timeout = self.cfg.request_timeout
        attempts = self.cfg.retries + 1
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                with self.session.get(url, timeout=(timeout, timeout), stream=True,
                                      allow_redirects=True) as r:
                    final = normalize_url(r.url, self.cfg.keep_query) or r.url
                    if site_key(final) != self.site:
                        return "skip", f"redirected off-site to {final}"
                    if r.status_code in RETRY_STATUS and not last:
                        self.stop.wait(2 * attempt)
                        continue
                    if r.status_code != 200:
                        return "fail", f"http_{r.status_code}"
                    ctype = r.headers.get("Content-Type", "").lower()
                    if "html" not in ctype:
                        return "skip", f"not html ({ctype or 'no content-type'})"
                    length = r.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > self.max_bytes:
                        return "skip", f"too large ({length} bytes)"
                    body = self._read_capped(r, timeout)
                    if body is None:
                        return "skip", "too large or too slow"
                    return "page", (final, r, body)
            except requests.exceptions.SSLError:
                return "fail", "ssl_error (try --no-verify-ssl)"
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if last or self.stop.is_set():
                    return "fail", type(e).__name__
                self.stop.wait(2 * attempt)
            except requests.exceptions.RequestException as e:
                return "fail", type(e).__name__
        return "fail", "retries_exhausted"

    def _read_capped(self, r, timeout):
        """Read at most max_bytes; give up if the whole body takes too long."""
        deadline = time.monotonic() + timeout * 4
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > self.max_bytes or time.monotonic() > deadline or self.stop.is_set():
                return None
        return bytes(buf)

    def _load_robots(self):
        if not self.cfg.respect_robots:
            return
        parts = urlsplit(self.seed)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        text = ""
        try:
            with self.session.get(robots_url, timeout=self.cfg.request_timeout, stream=True) as r:
                if r.status_code == 200:
                    raw = self._read_capped_n(r, ROBOTS_MAX_BYTES)
                    text = raw.decode("utf-8", errors="replace")
        except requests.exceptions.RequestException as e:
            log.info(f"[{self.site}] robots.txt unavailable ({type(e).__name__}), allowing all")
        try:
            from protego import Protego
            self.robots = Protego.parse(text)
            robots_delay = self.robots.crawl_delay(self.cfg.user_agent)
            if robots_delay:
                self.delay = max(self.delay, min(float(robots_delay), MAX_ROBOTS_DELAY))
        except Exception as e:
            log.warning(f"[{self.site}] could not parse robots.txt: {e}")
            self.robots = None

    @staticmethod
    def _read_capped_n(r, n):
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) >= n:
                break
        return bytes(buf[:n])

    def _wait_for_ram(self):
        floor = self.cfg.min_free_ram_mb
        if floor <= 0:
            return
        warned = False
        while not self.stop.is_set():
            free = free_ram_mb()
            if free is None or free >= floor:
                return
            if not warned:
                log.warning(f"[{self.site}] only {free:.0f} MB RAM free (< {floor}); pausing")
                warned = True
            self.stop.wait(5)

    # ---------------------------------------------------------------- resume state
    def _load_state(self):
        if not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
            self.frontier = deque((u, d) for u, d in state["frontier"])
            self.seen = set(state["seen"])
            self.pages = state.get("pages", 0)
            self.fails = state.get("fails", 0)
            return bool(self.frontier)
        except Exception as e:
            log.warning(f"[{self.site}] bad state file, starting fresh: {e}")
            self.frontier, self.seen = deque(), set()
            return False

    def _save_state(self):
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seed": self.seed, "pages": self.pages, "fails": self.fails,
                       "frontier": list(self.frontier), "seen": list(self.seen)}, f)
        os.replace(tmp, self.state_path)

    def _clear_state(self):
        try:
            os.remove(self.state_path)
        except FileNotFoundError:
            pass


def _http_version(resp):
    return {10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}.get(getattr(resp.raw, "version", 11), "HTTP/1.1")


def _extract_links(body, base_url):
    try:
        doc = lxml_html.fromstring(body)
    except (etree.ParserError, ValueError):
        return []
    base = doc.xpath("//base/@href")
    if base:
        base_url = urljoin(base_url, base[0].strip())
    links = []
    for href in doc.xpath("//a/@href"):
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            links.append(urljoin(base_url, href))
        except ValueError:
            continue
    return links


# ==================================================================== worker process

def worker_main(worker_id, cfg, run_id, task_queue, events, stop):
    """Entry point of each worker process."""
    # Ctrl+C is handled by the main process only; it tells us via `stop`.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    setup_logging(os.path.join(cfg.logs_dir, f"worker_{worker_id}.log"), cfg.log_level)
    apply_process_limits(cfg)
    if not cfg.verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    warc = RotatingWarcWriter(cfg.warc_dir, f"{run_id}-w{worker_id:02d}",
                              cfg.warc_max_mb * 1024 * 1024)

    def thread_loop():
        while not stop.is_set():
            try:
                seed = task_queue.get(timeout=1)
            except queue.Empty:
                continue
            if seed is None:  # no more domains
                return
            try:
                DomainCrawler(seed, cfg, warc, events, stop).run()
            except Exception as e:
                log.exception(f"crawler for {seed} crashed")
                events.put(("domain_done", site_key(seed), 0, 0, f"error: {e}"))

    threads = [threading.Thread(target=thread_loop, name=f"w{worker_id}-t{i}", daemon=True)
               for i in range(cfg.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    warc.close()
    log.info(f"worker {worker_id} exiting")
    events.put(("worker_exit", worker_id))
