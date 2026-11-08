"""
All crawler settings live here.

Every field below can be overridden from the command line, e.g.

    python main.py --workers 4 --threads 8 --max-pages-per-domain 500

Run `python main.py --help` to see every option with its default.
To change a default permanently, edit the value in the Config class.
"""
import argparse
import os
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # ---------------------------------------------------------------- input / output
    seed_file: str = field(default="seed.txt", metadata={
        "help": "File with one domain or start URL per line (# starts a comment)."})
    output_dir: str = field(default="output", metadata={
        "help": "Folder for WARC files, logs, URL lists and resume state."})

    # ---------------------------------------------------------------- resources
    workers: int = field(default=2, metadata={
        "help": "Worker processes. Each one uses at most about 1 CPU core."})
    threads: int = field(default=8, metadata={
        "help": "Domains crawled at the same time inside each worker. "
                "Total domains in flight = workers x threads."})
    cpus: str = field(default="", metadata={
        "help": "Pin the crawler to these CPU cores, e.g. '0,1' or '2-5'. Empty = OS decides."})
    low_priority: bool = field(default=True, metadata={
        "help": "Run at below-normal OS priority so the rest of the PC stays responsive."})
    min_free_ram_mb: int = field(default=1024, metadata={
        "help": "Pause fetching while the system has less free RAM than this (MB). 0 = off."})

    # ---------------------------------------------------------------- crawl limits
    max_depth: int = field(default=5, metadata={
        "help": "How many links away from the start page to follow."})
    max_pages_per_domain: int = field(default=2000, metadata={
        "help": "Stop a domain after saving this many HTML pages. 0 = no limit."})
    max_frontier_per_domain: int = field(default=20000, metadata={
        "help": "Max URLs waiting in one domain's queue. Extra links are dropped. Caps RAM."})
    max_page_mb: float = field(default=5.0, metadata={
        "help": "Skip any response bigger than this (MB). Never loads more than this into RAM."})
    request_timeout: float = field(default=15.0, metadata={
        "help": "Seconds to wait for a server (connect and each read)."})
    retries: int = field(default=1, metadata={
        "help": "Extra attempts for timeouts, connection errors, 429 and 5xx."})
    crawl_delay: float = field(default=1.0, metadata={
        "help": "Seconds between two requests to the same domain."})
    respect_robots: bool = field(default=True, metadata={
        "help": "Obey robots.txt rules (and its Crawl-delay if longer than --crawl-delay)."})
    keep_query: bool = field(default=False, metadata={
        "help": "Keep ?query strings in URLs. Off by default: they often create endless URL variants."})
    verify_ssl: bool = field(default=True, metadata={
        "help": "Check HTTPS certificates. Use --no-verify-ssl for sites with broken certificates."})
    user_agent: str = field(default="EkaCurator/1.0", metadata={
        "help": "User-Agent header sent with every request."})

    # ---------------------------------------------------------------- output
    warc_max_mb: int = field(default=500, metadata={
        "help": "Start a new .warc.gz file when the current one reaches this size (MB, compressed)."})

    # ---------------------------------------------------------------- run control
    resume: bool = field(default=True, metadata={
        "help": "Skip finished domains and continue interrupted ones. --no-resume starts over."})
    progress_every: float = field(default=10.0, metadata={
        "help": "Seconds between progress lines in the console."})
    shutdown_grace: float = field(default=30.0, metadata={
        "help": "After Ctrl+C, seconds to wait for workers to save state before killing them."})
    log_level: str = field(default="INFO", metadata={
        "help": "DEBUG logs every URL; INFO logs domains and errors."})
    dry_run: bool = field(default=False, metadata={
        "help": "Print the plan and the estimated RAM use, then exit without crawling."})

    # ---------------------------------------------------------------- derived paths
    @property
    def warc_dir(self):
        return os.path.join(self.output_dir, "warc")

    @property
    def logs_dir(self):
        return os.path.join(self.output_dir, "logs")

    @property
    def state_dir(self):
        return os.path.join(self.output_dir, "state")


def parse_args(argv=None) -> Config:
    parser = argparse.ArgumentParser(
        description="Eka Crawler - bounded, resumable, multi-process web crawler.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for f in fields(Config):
        flag = "--" + f.name.replace("_", "-")
        help_text = f.metadata.get("help", "")
        if f.type is bool:
            parser.add_argument(flag, dest=f.name, default=f.default, help=help_text,
                                action=argparse.BooleanOptionalAction)
        else:
            parser.add_argument(flag, dest=f.name, default=f.default, help=help_text, type=f.type)
    cfg = Config(**vars(parser.parse_args(argv)))
    _validate(cfg, parser)
    return cfg


def _validate(cfg: Config, parser):
    def bad(msg):
        parser.error(msg)

    if cfg.workers < 1:
        bad("--workers must be at least 1")
    if cfg.threads < 1:
        bad("--threads must be at least 1")
    if cfg.max_depth < 0:
        bad("--max-depth cannot be negative")
    if cfg.max_pages_per_domain < 0:
        bad("--max-pages-per-domain cannot be negative (0 = no limit)")
    if cfg.max_frontier_per_domain < 1:
        bad("--max-frontier-per-domain must be at least 1")
    if cfg.max_page_mb <= 0:
        bad("--max-page-mb must be positive")
    if cfg.request_timeout <= 0:
        bad("--request-timeout must be positive")
    if cfg.crawl_delay < 0:
        bad("--crawl-delay cannot be negative")
    if cfg.warc_max_mb < 1:
        bad("--warc-max-mb must be at least 1")
    if cfg.retries < 0:
        bad("--retries cannot be negative")
    cfg.log_level = cfg.log_level.upper()
    if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        bad("--log-level must be DEBUG, INFO, WARNING or ERROR")
    if cfg.cpus:
        from utils import parse_cpu_list
        try:
            parse_cpu_list(cfg.cpus)
        except ValueError as e:
            bad(f"--cpus: {e}")
