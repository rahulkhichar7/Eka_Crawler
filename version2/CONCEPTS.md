# Concepts used in Eka Crawler v2

This file explains *why* the code is built the way it is. Each section names the files
where the idea lives.

---

## 1. One entry point, many pipelines

`main.py` only builds an `argparse` tree: `main.py <pipeline> <command>`. Each pipeline
folder (`eka/seeds`, `eka/crawl`, …) has a `cli.py` with a `register(subparsers)` function.
Adding a new pipeline = a new folder with a `cli.py` and one line in `main.py`.

Shared code lives in `eka/core/` so every pipeline gets the same HTTP behaviour, proxy
support, URL cleaning, logging and resource limits.

## 2. Processes, threads and the GIL — how "number of cores" works

Python runs only one thread of Python code at a time per process (the GIL). So:

- **Processes** (`--workers`) are what use more CPU cores. One worker ≈ at most one core.
- **Threads** (`--threads`) are cheap and are used for *waiting*: a crawler thread spends
  ~99% of its time waiting for a server, so 8 threads in one process still use < 1 core.

The old crawlers started **one process per seed** (735 processes = 20-40 GB of RAM just for
Python interpreters). v2 starts a **fixed pool**: `workers` processes × `threads` threads,
however long `seed.txt` is. Processes are started with the `spawn` method so behaviour is
identical on Windows and Linux.

CPU-heavy jobs (WARC parsing, text extraction) use `ProcessPoolExecutor(max_workers=--workers)`,
one input file per task. Network-heavy jobs (downloads, Shodhganga, NDL) use threads.

`--cpus 0-3` pins the processes to cores (`psutil.cpu_affinity`), `--low-priority` lowers
their OS priority (`BELOW_NORMAL_PRIORITY_CLASS` on Windows, `nice` on Linux).
`core/resources.py`.

## 3. Bounded memory — every buffer has a limit

A crawler crashes RAM when something grows without a limit. v2 bounds all of them:

| Thing | Bound |
|---|---|
| processes | `1 + --workers` |
| sites in flight | `--workers × --threads` |
| URLs queued per site | `--max-frontier-per-domain` (memory) or on disk (SQLite) |
| one HTTP body in RAM | `--max-page-mb` (streamed, stopped as soon as it is bigger) |
| messages from workers to main | `Queue(maxsize=20000)` — **backpressure**: if main is slow, workers wait |
| download tasks waiting | `Queue(maxsize=threads*4)` — a million-URL list never becomes a million tasks |
| free system RAM | `--min-free-ram-mb`: work pauses below it |

**Backpressure** is the key idea: a producer that is faster than its consumer must be forced
to wait, otherwise the difference piles up in memory (that was the old unbounded
`warc_queue` with full page bodies).

## 4. The crawl frontier and de-duplication

The *frontier* is the queue of URLs a site still has to fetch (`crawl/frontier.py`).

- **De-duplicate when queueing, not when fetching.** A URL goes into the `seen` set the moment
  it is queued. A "Home" link that appears on all 10,000 pages is queued once. The old code
  checked `visited` (fetched) only, so every page re-queued every navigation link →
  exponential queue growth.
- **`deque`** instead of `list.pop(0)`: O(1) instead of O(n) per pop.
- **Memory frontier**: `deque` + `set`, capped; saved as JSON on Ctrl+C.
- **Disk frontier** (`--frontier disk`): one SQLite file per site with tables `queue` and
  `seen`. `INSERT OR IGNORE INTO seen` is the de-dup test; RAM stays ~2 MB per site no matter
  how big the site is. Committed after every page, so a crash loses at most one page.
  This is the "queue on disk" idea of the old DiskCache crawler, but it also keeps `seen`
  on disk and needs no extra package (SQLite is part of Python).
- **No shared visited set between processes.** One site is crawled by exactly one thread,
  so all its state is local — no `Manager().dict()` round-trip per link (the old global
  visited dict was both a CPU bottleneck and a never-ending RAM leak).

## 5. URL normalisation, site keys and host cleaning

`core/urls.py`.

- `normalize_url`: lowercase scheme/host, drop default port and `#fragment`, empty path → `/`,
  drop `?query` unless `--keep-query` (query strings create endless variants: calendars,
  session ids, sort orders).
- `site_key`: `host[:port]` without `www.` — `example.com` and `www.example.com` are one site.
- `url_key`: URL without scheme and `www.` — `http://` and `https://` versions are one URL.
- `clean_host` (seeds): repairs the messy real seed files: `http://%20%20www.x.in`,
  `http://   www.x.in`, `director@iirs.gov.in`, `(www.x.in`, `irctc..co.in`,
  `http://https:\\x.in`, IDN hosts (→ punycode); rejects invalid hostnames (labels ending in
  `-`, no TLD, IPs).
- Non-HTML extensions (`.pdf .jpg .zip .mp4 …`) are never requested by the crawler.

## 6. Politeness

- **robots.txt** is fetched once per site (Protego parser) and every URL is checked.
  A `Crawl-delay` longer than `--crawl-delay` wins (capped at 60 s).
- **One request at a time per site** with `--crawl-delay` seconds between them
  (default 1 s; the old code used 0–0.01 s).
- **Downloads**: `--per-host-delay` between two requests to the same host, even with many threads.
- `Retry-After` is honoured on 429/503; retries back off (2 s, 4 s, …).
- **https → http fallback**: many Indian sites have no working https; if the start URL fails
  to connect over https, the crawler tries http once (and re-reads that site's robots.txt).

## 7. Streaming and size caps

`core/http.py`. Every request uses `stream=True`: headers arrive first, the body is read only
if wanted. The crawler checks `Content-Type` and `Content-Length` *before* reading, then reads
in 64 KB chunks and gives up past `--max-page-mb` or after 4 × timeout (slow-drip servers).
The old code downloaded every PDF and video completely into RAM just to look at its type.

Downloads stream straight to disk into a unique `<file>.<pid>-<thread>.part` and are renamed
when complete (`os.replace` is atomic), so a half-downloaded file never looks finished and two
threads never write the same temp file.

## 8. Text encoding

lxml treats bytes without a declared charset as Latin-1 — every Hindi/Tamil page without a
`<meta charset>` would become `à¤¨à¤®...`. `core/html.py` decides the encoding as:
HTTP `charset` → `<meta charset>` → "is it valid UTF-8?" → let lxml guess.

## 9. WARC files

WARC (ISO 28500) is the standard web-archive format: each record stores the request URL,
HTTP status line + headers and the body. `core/warc.py`:

- Each worker writes **its own** WARC series (`<run-id>-w<NN>-<n>.warc.gz`), so bodies never
  cross process boundaries and there is no single writer bottleneck.
- Files rotate at `--warc-max-mb` measured on disk (compressed).
- `Content-Encoding`/`Transfer-Encoding` are removed from stored headers and `Content-Length`
  set to the real length, because `requests` already decoded the body.
- The page language (`<html lang>`) is stored as `WARC-Identified-Content-Language`.
- Reading (`iter_responses`) streams the file record by record — never `f.read()` of a whole
  multi-GB WARC (the old extractors loaded each WARC fully into a `BytesIO`).

## 10. Resumability patterns

Every long job can be stopped and continued. Four simple patterns are used:

1. **Done log** (`core/files.DoneLog`): an append-only text file of finished keys
   (sites, identifiers, pages). On start it is loaded into a set and those keys are skipped.
2. **One output file per input** (text, pdf-urls from-warc, Common Crawl): if the output exists,
   the input is done. Outputs are written as `.tmp` and renamed at the end, so only complete
   files exist.
3. **State files**: crawl frontier (`state/<site>.json` or `.sqlite`), Shodhganga browse offset
   (`<out>.state`), dataset next ID (`state.json`).
4. **Manifest** (downloads): every URL's result; `ok` and permanent failures (404, wrong type,
   too large) are skipped next time, temporary ones (timeouts, 5xx) are retried.

`dataset combine` marks an input as added only after the output file containing its rows
is closed — a crash can never leave inputs "added" whose rows were lost.

## 11. Ctrl+C on Windows

On Windows Ctrl+C is delivered to *every* process of the console. Workers ignore SIGINT; only
the main process reacts: it sets a shared `stop` Event. Every wait in the crawler is
`stop.wait(seconds)` instead of `time.sleep`, so it wakes immediately; each thread finishes
its current request, saves its site, and exits. After `--shutdown-grace` seconds (or a second
Ctrl+C) remaining workers are terminated. Queue feeder threads are cancelled so the process
cannot hang on exit.

## 12. Seeds lifecycle

```
curated sheets / lists ──┐
crawled WARC outlinks ───┼─► seeds build ──► seed.txt (master) ──► crawl
new URL dumps ───────────┘        ▲  --exclude master  → only new sites ("feed file")
                                  └─ --update-master   → master keeps growing
```

This is the Oct–Nov `all_seed_<date>.txt` (master) + `feedfile_<date>.txt` (new only) flow,
as flags. `seeds from-warc` is the discovery step: hosts of every page *and every outgoing
link* in your WARCs (optionally only `.in`), minus the master.

`seed.txt` in this folder = all 40+ seed files of the old folders (2.37 M lines) → 327,656
unique sites (by `site_key`).

## 13. PDF URL discovery

PDF links are **recorded, not fetched**, while crawling: every `.pdf` link in `<a>`,
`<iframe>`, `<embed>`, `<object>`, `<link>` — on any host — goes to `pdf_urls.csv` with its parent
page and that page's language. The language of the parent page is the best cheap hint of the
PDF's language. A PDF served without `.pdf` in the URL is detected by `Content-Type`.

Special sources need their own logic (`eka/pdf_urls/`):

- **Shodhganga**: browse listing (100 per page) → thesis handle pages → `/bitstream/…pdf`.
- **NDL IIT KGP**: the PDF hides behind an iframe viewer; follow page → iframe (with
  `Referer`) → regex `PDFViewerApplication.open({"url": …})`.
- **Sansad eparlib**: JSON REST API in ranges; every string that looks like a PDF link is pulled
  out of the JSON recursively (schema-agnostic).
- **archive.org**: the Scrape API (`/services/search/v1/scrape`, cursor-paged) lists items per
  language; `/metadata/<id>` lists an item's files; PDF + OCR text formats are downloaded.

`pdf-urls merge` = the old `PDFUrlCleaner`: union of any CSV/TXT lists minus already-downloaded
lists (including the downloader's `manifest.csv`).

## 14. Blocklists

`eka/blocklist/blocklist.py`, data in `blocklists/`. Matching order for a URL:

1. **allow-list** (`sources/allow.txt` + UT1 `liste_blanche`): host or any parent domain
   (`gov.in` protects every `*.gov.in`).
2. **domain lists**: the host and each parent domain (`a.b.example.com` → `b.example.com` →
   `example.com`).
3. **URL lists**: exact normalised URL (with and without query).
4. **labels** (Dolma subreddit names vs. the first host label) — opt-in only.
5. **keyword rules** (`keywords.json`): `strict` = substring of very specific words,
   `hard` = whole tokens, `soft` = loose substrings (many false positives).

Presets group categories. They were checked against the real 327k-site `seed.txt`:
`unsafe` removes 68 sites (all gambling / crypto-mining); the wider lists also hit
archive.org (in "torrent"), matrimony and university sites (in "dating"), news (in "drugs")
— which is why the lists are off by default and the presets are conservative.

## 15. Proxies as a plug-in (strategy pattern)

`core/proxy.py`. Every request asks a `ProxyProvider` object `get(url)` → proxy URL or `None`
and reports success/failure back. The default `NoProxy` returns `None`. Built-ins:
`SingleProxy`, `ProxyPool` (random / round-robin, failing proxies benched for `cooldown`
seconds), `FileProxyPool` (re-reads the file every `reload` seconds, so another process can
refresh it — the old "proxy updater" idea). Your own class is loaded from
`--proxy path/to/file.py:ClassName?arg=value`. Because the provider sees each URL, it can do
per-host stickiness, per-site proxies, API-refreshed pools… without touching pipeline code.

## 16. Text extraction

`eka/text/`. **trafilatura** (`fast=True`) removes boilerplate (menus, footers, ads) and keeps
the main text; if it is not installed a simple lxml fallback (drop script/style/nav/header/
footer, keep visible text) is used. Common Crawl offers two inputs:

- **WET** files: text already extracted by CC (fast, noisier), with CC's language tag
  `WARC-Identified-Content-Language` (ISO 639-3, e.g. `hin,eng`) → `--languages` filter.
- **WARC** files: raw HTML → trafilatura (slower, cleaner).

Common Crawl layout: a *snapshot* (`CC-MAIN-2025-18`) has `warc.paths.gz` / `wet.paths.gz`
listing ~90,000 files of ~1 GB / ~80 MB each under `https://data.commoncrawl.org/`.
v2 downloads at most `--workers` files at a time, processes them, then deletes the raw file
(unless `--keep-raw`) — disk use stays bounded.

## 17. Final dataset schema

`eka/dataset/cli.py`, same schema as the old combiners:

| column | meaning |
|---|---|
| `ID` | global running int64 (continues across runs via `state.json`) |
| `SOURCE` | `EC` (Eka crawl), `CC` (Common Crawl), `PDF`, … |
| `TIME` | crawl time: from the file name (`…-20181209211547-…`), a fixed date, or file mtime |
| `REMARK` | input file name |
| `URI`, `DATA` | page URL and text |

Inputs are streamed row-group by row-group (`ParquetFile.iter_batches`) — never a whole
file in RAM. Output files rotate at `--max-gb` and are named by their ID range. Optional
`--dedupe-uri` keeps a SQLite set of written URIs on disk.
