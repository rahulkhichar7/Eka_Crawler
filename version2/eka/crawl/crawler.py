"""Worker process + per-site crawler.

Each worker process runs `threads` threads; each thread takes one site from the
task queue, crawls it to the end (or a limit), then takes the next. All per-site
state is local to that thread, so there is no cross-process locking per URL.
"""
import logging
import os
import queue
import signal
import threading
import time
from urllib.parse import urlsplit

import requests

from eka.core.files import safe_filename, setup_logging
from eka.core.html import charset_from_content_type, extract_links, page_lang, parse
from eka.core.http import HttpClient, TooLarge
from eka.core.proxy import load_proxy
from eka.core.resources import apply_process_limits, wait_for_ram
from eka.core.urls import has_skipped_extension, is_pdf_url, normalize_url, site_key, url_key
from eka.core.warc import RotatingWarcWriter
from eka.crawl.frontier import DiskFrontier, MemoryFrontier

log = logging.getLogger("eka.crawl")

ROBOTS_MAX_BYTES = 512 * 1024
MAX_ROBOTS_DELAY = 60.0
PDF_TAGS = ("a", "iframe", "embed", "object", "link")


class SiteCrawler:
    """Breadth-first crawl of one site with hard limits on RAM and time."""

    def __init__(self, site, start_urls, cfg, client, warc, events, stop, blocklist):
        self.cfg = cfg
        self.site = site
        self.start_urls = start_urls
        self.client = client
        self.warc = warc
        self.events = events
        self.stop = stop
        self.blocklist = blocklist
        self.max_bytes = int(cfg.max_page_mb * 1024 * 1024)
        name = safe_filename(site)
        if cfg.frontier == "disk":
            self.state_path = os.path.join(cfg.state_dir, "frontier", name + ".sqlite")
            self.frontier = DiskFrontier.open(self.state_path, cfg.frontier_cap, cfg.resume)
        else:
            self.state_path = os.path.join(cfg.state_dir, name + ".json")
            self.frontier = MemoryFrontier.open(self.state_path, cfg.frontier_cap, cfg.resume)
        self.robots = None
        self.delay = cfg.crawl_delay
        self.fallback_done = not cfg.http_fallback

    @property
    def counters(self):
        return self.frontier.counters

    # ---------------------------------------------------------------- main loop
    def run(self):
        self.events.put(("domain_start", self.site))
        try:
            resumed = len(self.frontier) > 0
            if not resumed:
                for u in self.start_urls:
                    self._enqueue(u, 0, check_site=False)
            self._load_robots()
            log.info(f"[{self.site}] start: {len(self.frontier)} queued "
                     f"({'resumed' if resumed else 'fresh'}), delay {self.delay:.2f}s")
            next_request = 0.0
            limit = self.cfg.max_pages_per_domain
            while len(self.frontier) and not self.stop.is_set():
                if limit and self.counters["pages"] >= limit:
                    log.info(f"[{self.site}] page limit {limit} reached")
                    break
                wait_for_ram(self.cfg.min_free_ram_mb, self.stop, who=f"[{self.site}]")
                item = self.frontier.pop()
                if item is None:
                    break
                url, depth = item
                if self.robots is not None and not self.robots.can_fetch(url, self.cfg.user_agent):
                    log.debug(f"robots.txt disallows {url}")
                    continue
                if self.stop.wait(max(0.0, next_request - time.monotonic())):
                    self.frontier.unpop(url, depth)
                    break
                next_request = time.monotonic() + self.delay
                self._process(url, depth)
                self.frontier.commit()

            if self.stop.is_set():
                self.frontier.save(self.state_path)
                status = "interrupted"
            else:
                status = "finished"
            c = self.counters
            log.info(f"[{self.site}] {status}: {c['pages']} pages, {c['pdfs']} pdf links, "
                     f"{c['fails']} failed, {len(self.frontier)} left")
            if status == "finished":
                self.frontier.discard(self.state_path)
            self.events.put(("domain_done", self.site, c["pages"], c["fails"], status))
        finally:
            self.frontier.close()

    def _process(self, url, depth):
        kind, info = self._fetch(url)
        if kind == "fail":
            self.counters["fails"] += 1
            self.events.put(("fail", self.site, url, info))
            if (not self.fallback_done and self.counters["pages"] == 0 and depth == 0
                    and url.startswith("https://") and info in ("ConnectionError", "ssl_error",
                                                                 "ConnectTimeout")):
                self.fallback_done = True
                http_url = "http://" + url[8:]
                self.frontier.add(http_url, 0, force=True)
                log.info(f"[{self.site}] https failed ({info}); trying http")
                self._load_robots(http_url)  # robots.txt of the http site
            return
        if kind == "skip":
            log.debug(f"skip {url}: {info}")
            return
        final_url, resp, body = info
        if final_url != url:
            self.frontier.mark(url_key(final_url))
        doc = parse(body, charset_from_content_type(resp.headers.get("Content-Type")))
        lang = page_lang(doc)
        if self.cfg.save_html:
            self.warc.write_response(final_url, resp.status_code, resp.reason, _http_version(resp),
                                     list(resp.raw.headers.items()), body,
                                     extra={"WARC-Identified-Content-Language": lang} if lang else None)
        self.counters["pages"] += 1
        self.events.put(("page", self.site, final_url, resp.status_code, len(body), lang))
        if doc is None:
            return
        links = extract_links(doc, final_url, tags=PDF_TAGS if self.cfg.collect_pdf_urls else ("a",))
        follow = depth < self.cfg.max_depth
        for link in links:
            if self.cfg.collect_pdf_urls and is_pdf_url(link):
                self._record_pdf(link, final_url, lang)
            elif follow:
                self._enqueue(link, depth + 1)

    def _record_pdf(self, link, parent, lang):
        url = normalize_url(link, keep_query=True)
        if url is None or self.counters["pdfs"] >= self.cfg.max_pdf_urls_per_domain:
            return
        if self.blocklist is not None and self.blocklist.match(url):
            return
        if self.frontier.mark("pdf|" + url_key(url)):
            self.counters["pdfs"] += 1
            self.events.put(("pdf", self.site, url, parent, lang))

    # ---------------------------------------------------------------- queue
    def _enqueue(self, url, depth, check_site=True):
        url = normalize_url(url, self.cfg.keep_query) if url else None
        if url is None or has_skipped_extension(url):
            return
        if check_site and site_key(url) != self.site:
            return
        if self.blocklist is not None and self.blocklist.match(url):
            return
        self.frontier.add(url, depth)

    # ---------------------------------------------------------------- network
    def _fetch(self, url):
        """("page", (final_url, response, body)) | ("skip", why) | ("fail", why)."""
        try:
            with self.client.get(url, stop=self.stop) as r:
                final = normalize_url(r.url, self.cfg.keep_query) or r.url
                if site_key(final) != self.site:
                    return "skip", f"redirected off-site to {final}"
                if r.status_code != 200:
                    return "fail", f"http_{r.status_code}"
                ctype = r.headers.get("Content-Type", "").lower()
                if "html" not in ctype:
                    if self.cfg.collect_pdf_urls and "pdf" in ctype:
                        self._record_pdf(final, url, "")
                    return "skip", f"not html ({ctype or 'no content-type'})"
                try:
                    body = HttpClient.read_capped(r, self.max_bytes,
                                                  deadline_s=self.cfg.request_timeout * 4, stop=self.stop)
                except TooLarge as e:
                    return "skip", f"too large/slow: {e}"
                return "page", (final, r, body)
        except requests.exceptions.SSLError:
            return "fail", "ssl_error"
        except requests.exceptions.ConnectTimeout:
            return "fail", "ConnectTimeout"
        except requests.exceptions.RequestException as e:
            return "fail", type(e).__name__

    def _load_robots(self, first=None):
        if not self.cfg.respect_robots:
            return
        first = first or (self.start_urls[0] if self.start_urls else f"https://{self.site}/")
        parts = urlsplit(first)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        text = ""
        try:
            with self.client.get(robots_url, stop=self.stop) as r:
                if r.status_code == 200:
                    try:
                        raw = HttpClient.read_capped(r, ROBOTS_MAX_BYTES)
                    except TooLarge:
                        raw = b""
                    text = raw.decode("utf-8", errors="replace")
        except requests.exceptions.RequestException as e:
            log.info(f"[{self.site}] robots.txt unavailable ({type(e).__name__}); allowing all")
        try:
            from protego import Protego
            self.robots = Protego.parse(text)
            rd = self.robots.crawl_delay(self.cfg.user_agent)
            if rd:
                self.delay = max(self.delay, min(float(rd), MAX_ROBOTS_DELAY))
        except Exception as e:
            log.warning(f"[{self.site}] could not parse robots.txt: {e}")
            self.robots = None


def _http_version(resp):
    return {10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}.get(getattr(resp.raw, "version", 11), "HTTP/1.1")


# ==================================================================== worker process

def worker_main(worker_id, cfg, run_id, task_queue, events, stop):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # main process handles Ctrl+C
    setup_logging(os.path.join(cfg.logs_dir, f"worker_{worker_id}.log"), cfg.log_level, console_level=None)
    apply_process_limits(cfg.cpus, cfg.low_priority)
    client = HttpClient(user_agent=cfg.user_agent, timeout=cfg.request_timeout,
                        verify_ssl=cfg.verify_ssl, retries=cfg.retries, proxy=load_proxy(cfg.proxy),
                        headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    blocklist = None
    if cfg.blocklist or cfg.blocklist_keywords:
        from eka.blocklist.blocklist import Blocklist
        blocklist = Blocklist.load(None, cfg.blocklist or "unsafe", cfg.blocklist_keywords)
    warc = RotatingWarcWriter(cfg.warc_dir, f"{run_id}-w{worker_id:02d}",
                              cfg.warc_max_mb * 1024 * 1024) if cfg.save_html else None

    def loop():
        while not stop.is_set():
            try:
                task = task_queue.get(timeout=1)
            except queue.Empty:
                continue
            if task is None:
                return
            site, urls = task
            try:
                SiteCrawler(site, urls, cfg, client, warc, events, stop, blocklist).run()
            except Exception as e:
                log.exception(f"crawler for {site} crashed")
                events.put(("domain_done", site, 0, 0, f"error: {e}"))
            finally:
                client.close()

    threads = [threading.Thread(target=loop, name=f"w{worker_id}-t{i}", daemon=True)
               for i in range(cfg.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if warc:
        warc.close()
    log.info(f"worker {worker_id} exiting")
    events.put(("worker_exit", worker_id))
