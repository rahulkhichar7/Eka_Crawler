"""Crawl settings. Every field is also a command-line flag:

    python main.py crawl --workers 4 --threads 8 --max-pages-per-domain 500
    python main.py crawl --help
"""
import argparse
import os
from dataclasses import dataclass, field, fields
from typing import Optional


@dataclass
class CrawlConfig:
    # ------------------------------------------------ input / output
    seed_file: str = field(default="seed.txt", metadata={
        "help": "One site or start URL per line. Several URLs of one site = several start points."})
    output_dir: str = field(default="output/crawl", metadata={
        "help": "WARCs, pdf_urls.csv, visited/failed lists, logs and resume state go here."})

    # ------------------------------------------------ resources
    workers: int = field(default=2, metadata={"help": "Worker processes (each <= ~1 CPU core)."})
    threads: int = field(default=8, metadata={
        "help": "Sites crawled at once per worker. Sites in flight = workers x threads."})
    cpus: str = field(default="", metadata={"help": "Pin to CPU cores, e.g. '0,1' or '2-5'."})
    low_priority: bool = field(default=True, metadata={"help": "Below-normal OS priority."})
    min_free_ram_mb: int = field(default=1024, metadata={
        "help": "Pause fetching while free system RAM is below this. 0 = off."})

    # ------------------------------------------------ what to crawl
    max_depth: int = field(default=5, metadata={"help": "Link hops from the start page(s)."})
    max_pages_per_domain: int = field(default=2000, metadata={
        "help": "Stop a site after this many HTML pages. 0 = no limit."})
    frontier: str = field(default="memory", metadata={
        "help": "'memory' (fast, capped queue) or 'disk' (SQLite per site: huge sites, flat RAM)."})
    max_frontier_per_domain: int = field(default=20000, metadata={
        "help": "Max queued URLs per site (memory mode). Disk mode uses 50x this."})
    keep_query: bool = field(default=False, metadata={
        "help": "Keep ?query in URLs (off: query strings create endless duplicates)."})
    respect_robots: bool = field(default=True, metadata={"help": "Obey robots.txt (+ Crawl-delay)."})
    crawl_delay: float = field(default=1.0, metadata={"help": "Seconds between requests to one site."})
    http_fallback: bool = field(default=True, metadata={
        "help": "If https://site fails to connect, try http://site once."})

    # ------------------------------------------------ what to save
    save_html: bool = field(default=True, metadata={
        "help": "Write pages to WARC. --no-save-html = only collect PDF URLs (PDF-link crawler)."})
    collect_pdf_urls: bool = field(default=True, metadata={
        "help": "Record every .pdf link (with parent page + its lang) to pdf_urls.csv. Never downloads."})
    max_pdf_urls_per_domain: int = field(default=200000, metadata={"help": "Cap per site."})
    warc_max_mb: int = field(default=500, metadata={"help": "Rotate WARC files at this size (compressed)."})

    # ------------------------------------------------ HTTP
    max_page_mb: float = field(default=5.0, metadata={"help": "Skip responses bigger than this."})
    request_timeout: float = field(default=15.0, metadata={"help": "Seconds for connect and each read."})
    retries: int = field(default=1, metadata={"help": "Extra tries on timeout/429/5xx."})
    verify_ssl: bool = field(default=True, metadata={"help": "Check HTTPS certificates."})
    user_agent: str = field(default="EkaCurator/2.0", metadata={"help": "User-Agent header."})
    proxy: str = field(default="none", metadata={
        "help": "none | http://host:port | file:proxies.txt | path.py:Class  (eka/core/proxy.py)"})

    # ------------------------------------------------ filtering (off by default)
    blocklist: str = field(default="", metadata={
        "help": "Skip sites/links in these blocklist categories, e.g. 'unsafe' or 'adult,gambling'."})
    blocklist_keywords: Optional[str] = field(default=None, metadata={
        "help": "Also block by keyword rules: strict | hard | soft.", "choices": ["strict", "hard", "soft"]})

    # ------------------------------------------------ run control
    resume: bool = field(default=True, metadata={
        "help": "Skip finished sites, continue interrupted ones. --no-resume starts over."})
    progress_every: float = field(default=10.0, metadata={"help": "Seconds between progress lines."})
    shutdown_grace: float = field(default=30.0, metadata={"help": "Seconds to wait after Ctrl+C."})
    log_level: str = field(default="INFO", metadata={"help": "DEBUG logs every URL."})
    dry_run: bool = field(default=False, metadata={"help": "Print plan + RAM estimate, then exit."})

    @property
    def warc_dir(self):
        return os.path.join(self.output_dir, "warc")

    @property
    def logs_dir(self):
        return os.path.join(self.output_dir, "logs")

    @property
    def state_dir(self):
        return os.path.join(self.output_dir, "state")

    @property
    def frontier_cap(self):
        return self.max_frontier_per_domain * (50 if self.frontier == "disk" else 1)


def add_config_args(parser):
    for f in fields(CrawlConfig):
        flag = "--" + f.name.replace("_", "-")
        kw = {"dest": f.name, "default": f.default, "help": f.metadata.get("help", "")}
        if f.type is bool:
            kw["action"] = argparse.BooleanOptionalAction
        else:
            kw["type"] = str if f.type == Optional[str] else f.type
            if "choices" in f.metadata:
                kw["choices"] = f.metadata["choices"]
        parser.add_argument(flag, **kw)


def config_from_args(args, parser):
    names = {f.name for f in fields(CrawlConfig)}
    cfg = CrawlConfig(**{k: v for k, v in vars(args).items() if k in names})
    bad = parser.error
    if cfg.workers < 1 or cfg.threads < 1:
        bad("--workers and --threads must be at least 1")
    if cfg.frontier not in ("memory", "disk"):
        bad("--frontier must be memory or disk")
    if cfg.max_depth < 0 or cfg.max_pages_per_domain < 0:
        bad("--max-depth / --max-pages-per-domain cannot be negative")
    if cfg.max_frontier_per_domain < 1 or cfg.max_page_mb <= 0 or cfg.request_timeout <= 0:
        bad("frontier, page size and timeout must be positive")
    if cfg.crawl_delay < 0 or cfg.retries < 0 or cfg.warc_max_mb < 1:
        bad("bad --crawl-delay / --retries / --warc-max-mb")
    if not cfg.save_html and not cfg.collect_pdf_urls:
        bad("--no-save-html together with --no-collect-pdf-urls would save nothing")
    cfg.log_level = cfg.log_level.upper()
    if cfg.cpus:
        from eka.core.resources import parse_cpu_list
        try:
            parse_cpu_list(cfg.cpus)
        except ValueError as e:
            bad(f"--cpus: {e}")
    return cfg


def estimate_ram_mb(cfg):
    per_url = 0.0006 if cfg.frontier == "memory" else 0.00005
    per_thread = cfg.max_page_mb * 3 + cfg.max_frontier_per_domain * per_url + 2
    per_worker = 90 + cfg.threads * per_thread  # + blocklist if used
    if cfg.blocklist or cfg.blocklist_keywords:
        per_worker += 60
    return 70 + cfg.workers * per_worker
