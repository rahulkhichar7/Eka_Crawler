"""main.py seeds <build|from-warc|check-robots>

build        merge any number of txt/csv files or folders into one clean, de-duplicated
             seed list (one https://host per line). Optional: drop sites already in a
             master list (= "new seeds only"), keep only some TLDs, drop keywords,
             apply blocklists, append the new ones to the master list.
from-warc    find new domains inside crawled WARC files (page URLs + every outgoing link).
check-robots split seeds into allowed / blocked-by-robots.txt / unreachable.
"""
import concurrent.futures as cf
import logging
import os
import sys
import time

from eka.blocklist.blocklist import add_blocklist_args, blocklist_from_args
from eka.core.files import iter_url_lines
from eka.core.http import HttpClient, add_http_args
from eka.core.urls import clean_host, scheme_of, site_key

log = logging.getLogger("eka.seeds")

# the keyword filter used in the Oct/Nov seed pipeline
WIKI_GOOGLE = ["wikipedia", "wikimedia", "wiktionary", "wikibooks", "wikinews", "wikiquote",
               "wikisource", "wikiversity", "wikivoyage", "wikidata", "wikicommons", "wikifunctions",
               "mediawiki", "wikispecies", "wikitech", "toolforge", "wikilivres", "wikijunior",
               "wikileaks", "google"]


def register(sub):
    p = sub.add_parser("seeds", help="Build / clean / expand seed (domain) lists.")
    s = p.add_subparsers(dest="cmd", required=True)

    b = s.add_parser("build", help="Merge txt/csv seed sources into one unique seed list.")
    b.add_argument("--in", dest="inputs", nargs="+", required=True,
                   help="txt/csv files or folders (CSV: every cell starting with http is used)")
    b.add_argument("--out", required=True)
    b.add_argument("--exclude", nargs="*", default=[],
                   help="master list(s) of sites already crawled; those are left out")
    b.add_argument("--update-master", default=None,
                   help="append the new sites to this master file (keeps the master growing)")
    b.add_argument("--tld", default="", help="keep only hosts ending with these, e.g. '.in,.org'")
    b.add_argument("--exclude-keywords", default="",
                   help="drop hosts containing any of these words (comma list)")
    b.add_argument("--skip-wiki-google", action="store_true",
                   help="drop wikipedia/wikimedia/.../google hosts (old Oct-Nov rule)")
    b.add_argument("--scheme", choices=["auto", "https", "http"], default="auto",
                   help="auto = https unless a source only ever wrote http://")
    add_blocklist_args(b)
    b.set_defaults(func=cmd_build)

    w = s.add_parser("from-warc", help="Discover domains linked from crawled WARC files.")
    w.add_argument("--warc", nargs="+", required=True, help="WARC files or folders")
    w.add_argument("--out", required=True)
    w.add_argument("--tld", default="", help="keep only these endings, e.g. '.in'")
    w.add_argument("--exclude", nargs="*", default=[], help="master list(s) to subtract")
    w.add_argument("--workers", type=int, default=2)
    w.set_defaults(func=cmd_from_warc)

    r = s.add_parser("check-robots", help="Which seeds allow crawling their home page?")
    r.add_argument("--in", dest="inputs", nargs="+", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--threads", type=int, default=16)
    add_http_args(r, timeout=10, retries=0)
    r.set_defaults(func=cmd_check_robots)


# ---------------------------------------------------------------- build
class SeedSet:
    """site_key -> (host, scheme). First spelling of a host wins; https wins over http."""

    def __init__(self, scheme_mode="auto"):
        self.mode = scheme_mode
        self.items = {}
        self.invalid = 0
        self.duplicates = 0

    def add_raw(self, raw):
        host = clean_host(raw)
        if host is None:
            self.invalid += 1
            return None
        key = site_key(host)
        scheme = scheme_of(raw) or "https"
        old = self.items.get(key)
        if old is None:
            self.items[key] = (host, scheme)
        else:
            self.duplicates += 1
            if scheme == "https" and old[1] == "http":
                self.items[key] = (old[0], "https")
        return key

    def lines(self):
        for key in sorted(self.items):
            host, scheme = self.items[key]
            if self.mode != "auto":
                scheme = self.mode
            yield f"{scheme}://{host}"


def load_keys(paths):
    keys = set()
    for raw in iter_url_lines(paths):
        h = clean_host(raw)
        if h:
            keys.add(site_key(h))
    return keys


def _filters(args):
    tlds = tuple(t.strip().lower() for t in args.tld.split(",") if t.strip())
    words = [w.strip().lower() for w in args.exclude_keywords.split(",") if w.strip()]
    if getattr(args, "skip_wiki_google", False):
        words += WIKI_GOOGLE
    return tlds, words


def cmd_build(args):
    t0 = time.time()
    seeds = SeedSet(args.scheme)
    n = 0
    for raw in iter_url_lines(args.inputs):
        seeds.add_raw(raw)
        n += 1
        if n % 500000 == 0:
            print(f"  read {n} lines, {len(seeds.items)} unique sites", flush=True)
    print(f"Read {n} lines -> {len(seeds.items)} unique sites "
          f"({seeds.duplicates} duplicates, {seeds.invalid} invalid lines)")

    tlds, words = _filters(args)
    master = load_keys(args.exclude) if args.exclude else set()
    bl = blocklist_from_args(args)
    removed = {"master": 0, "tld": 0, "keyword": 0, "blocklist": 0}
    for key in list(seeds.items):
        host = seeds.items[key][0]
        reason = None
        if key in master:
            reason = "master"
        elif tlds and not host.endswith(tlds):
            reason = "tld"
        elif words and any(w in host for w in words):
            reason = "keyword"
        elif bl is not None and bl.match(host):
            reason = "blocklist"
        if reason:
            removed[reason] += 1
            del seeds.items[key]
    if any(removed.values()):
        print("Removed: " + ", ".join(f"{k} {v}" for k, v in removed.items() if v))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    count = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for line in seeds.lines():
            f.write(line + "\n")
            count += 1
    print(f"Wrote {count} seeds -> {args.out} ({time.time() - t0:.1f}s)")

    if args.update_master and count:
        with open(args.update_master, "a", encoding="utf-8") as f:
            for line in seeds.lines():
                f.write(line + "\n")
        print(f"Appended {count} new sites to master {args.update_master}")
    return 0


# ---------------------------------------------------------------- from-warc
def _hosts_in_warc(path):
    """Runs in a worker process. Returns the set of hosts seen in one WARC file."""
    from eka.core.html import extract_links, parse
    from eka.core.warc import iter_responses
    hosts = set()
    try:
        for url, _, body, _ in iter_responses(path, html_only=True):
            h = clean_host(url)
            if h:
                hosts.add(h)
            for link in extract_links(parse(body), url, tags=("a", "link", "iframe", "area")):
                if link.startswith(("http://", "https://")):
                    h = clean_host(link)
                    if h:
                        hosts.add(h)
    except Exception as e:
        return path, hosts, str(e)
    return path, hosts, None


def cmd_from_warc(args):
    from eka.core.warc import list_warc_files
    files = list_warc_files(args.warc)
    if not files:
        print("No WARC files found.")
        return 1
    print(f"Scanning {len(files)} WARC files with {args.workers} workers ...")
    tlds = tuple(t.strip().lower() for t in args.tld.split(",") if t.strip())
    master = load_keys(args.exclude) if args.exclude else set()
    seeds = SeedSet()
    with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, (path, hosts, err) in enumerate(ex.map(_hosts_in_warc, files), 1):
            if err:
                print(f"  ! {os.path.basename(path)}: {err}")
            for h in hosts:
                if (not tlds or h.endswith(tlds)) and site_key(h) not in master:
                    seeds.add_raw("https://" + h)
            print(f"  [{i}/{len(files)}] {os.path.basename(path)}: {len(hosts)} hosts, "
                  f"{len(seeds.items)} new so far", flush=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for line in seeds.lines():
            f.write(line + "\n")
    print(f"Wrote {len(seeds.items)} domains -> {args.out}")
    return 0


# ---------------------------------------------------------------- check-robots
def cmd_check_robots(args):
    from protego import Protego
    client = HttpClient.from_args(args)
    seeds = list(dict.fromkeys(iter_url_lines(args.inputs)))
    os.makedirs(args.out_dir, exist_ok=True)

    def check(seed):
        url = seed if "://" in seed else "https://" + seed
        host = clean_host(url)
        if not host:
            return seed, "invalid", ""
        robots = f"{url.split('://')[0]}://{host}/robots.txt"
        try:
            with client.get(robots) as r:
                text = r.text[:512 * 1024] if r.status_code == 200 else ""
        except Exception as e:
            return seed, "unreachable", type(e).__name__
        rp = Protego.parse(text)
        ok = rp.can_fetch(url, args.user_agent)
        return seed, "allowed" if ok else "blocked", ""

    outs = {k: open(os.path.join(args.out_dir, f"{k}.txt"), "w", encoding="utf-8")
            for k in ("allowed", "blocked", "unreachable", "invalid")}
    counts = dict.fromkeys(outs, 0)
    with cf.ThreadPoolExecutor(max_workers=args.threads) as ex:
        for i, (seed, status, why) in enumerate(ex.map(check, seeds), 1):
            outs[status].write(seed + (f"\t{why}" if why else "") + "\n")
            counts[status] += 1
            if i % 200 == 0:
                print(f"  {i}/{len(seeds)} {counts}", flush=True)
    for f in outs.values():
        f.close()
    print(f"Done: {counts} -> {args.out_dir}")
    return 0
