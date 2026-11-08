# How Eka Crawler works

## The big picture

```
                         main.py  (1 process)
          ┌──────────────────────────────────────────────┐
          │ reads seed.txt, removes duplicates/finished  │
          │ owns visited.tsv, failed.tsv, done.txt       │
          │ prints progress, handles Ctrl+C              │
          └───────┬──────────────────────────▲───────────┘
     task queue   │ one seed URL per site    │  event queue (bounded, small messages:
     (+ "None"    │                          │  "page saved", "failed", "site done")
      end marks)  ▼                          │
     ┌────────────────────────┐    ┌────────────────────────┐
     │ worker 0 (process)     │    │ worker 1 (process)     │   … --workers of these
     │  thread 0: site A      │    │  thread 0: site D      │
     │  thread 1: site B      │    │  thread 1: site E      │   … --threads per worker
     │  thread 2: site C      │    │  thread 2: site F      │
     │  writes its own WARCs  │    │  writes its own WARCs  │
     └────────────────────────┘    └────────────────────────┘
```

- **Fixed number of processes.** `--workers` processes are started once, whatever
  the length of `seed.txt`. 729 seeds still means 2 processes by default.
- **Threads for the network waiting.** Inside each worker, `--threads` threads each
  take one site from the task queue, crawl it to the end, then take the next one.
  Fetching is mostly waiting on servers, so threads are cheap. Python's GIL keeps
  one worker at about 1 core even with many threads, which is why `--workers` is
  the number of cores the crawler can use.
- **One site = one thread.** A site is never crawled by two threads at once. This
  makes the per-site delay simple and needs no shared "visited" set between processes.

## Life of a site (crawler.py → `DomainCrawler.run`)

1. **Session.** One `requests.Session` per site, so the TCP and TLS connection is reused.
2. **robots.txt.** Fetched once (max 512 KB) and parsed with Protego. If it sets a
   `Crawl-delay` longer than `--crawl-delay`, the longer one is used (capped at 60 s).
3. **Loop (breadth-first):**
   - Take the next `(url, depth)` from the **frontier** (a `deque`, O(1) pop).
   - Skip it if robots.txt disallows it.
   - Wait until `--crawl-delay` seconds have passed since the last request to this
     site. The wait is `stop.wait(...)`, so Ctrl+C interrupts it immediately.
   - If free RAM is below `--min-free-ram-mb`, pause here until it recovers.
   - **Fetch** (see below). If it is HTML, write it to the WARC, report it to main,
     and, if `depth < --max-depth`, extract links.
4. **Stop** when the frontier is empty, `--max-pages-per-domain` is reached, or
   Ctrl+C was pressed.
5. **Finish.** On a normal finish, main writes the site to `done.txt`. On Ctrl+C,
   the frontier and seen-set are saved to `output/state/<site>.json`.

### Link handling

Every link goes through `_enqueue`:

1. `urljoin` with the page URL (respecting `<base href>`). `mailto:`, `javascript:`,
   `#…` and similar are dropped.
2. `normalize_url`: lowercase host, drop default port, drop `#fragment`, and drop
   `?query` (unless `--keep-query`), with empty path → `/`.
3. Dropped if it is on another site (`www.` is ignored when comparing), or if it
   ends in a non-HTML extension (`.pdf`, `.jpg`, `.zip`, …). No request is made for those.
4. Dropped if it was **ever queued before**. The `seen` set is updated when a URL
   is *queued*, not when it is fetched, so a nav link shared by every page is queued once.
5. Dropped if the frontier already holds `--max-frontier-per-domain` URLs.

Steps 4 and 5 are what keep memory flat. The old version appended every unvisited
link from every page, so its queues grew exponentially.

### Fetching (`_fetch`)

- `stream=True`: headers arrive first and the body is not downloaded yet.
- Off-site redirect → skip. Non-200 → `failed.tsv` (429/5xx retried `--retries` times).
- `Content-Type` not HTML → skip **without downloading the body**.
- `Content-Length` above `--max-page-mb` → skip.
- The body is read in 64 KB chunks and dropped as soon as it passes `--max-page-mb`,
  or if the whole download takes more than 4 × `--request-timeout`. This protects
  against servers that send a large body slowly.
- SSL errors are reported as `ssl_error` (see `--no-verify-ssl`).

### WARC writing (writers.py → `RotatingWarcWriter`)

Each worker writes its own files (`<run-id>-w<worker>-<n>.warc.gz`), guarded by a
lock shared by its threads. Page bodies therefore never cross a process boundary
and there is no single writer to fall behind. A new file starts when the
compressed size reaches `--warc-max-mb`. `requests` has already removed gzip and
chunked encoding, so `Content-Encoding` and `Transfer-Encoding` are dropped from
the stored headers and `Content-Length` is set to the real body length. The
record therefore matches its payload.

## Main process (main.py)

- Builds the task queue: one seed per unfinished site, then one `None` end marker
  per thread, so every thread knows when to exit.
- Starts workers with the `spawn` method, so behaviour is the same on Windows and Linux.
- Reads the **event queue** (bounded at 20 000 messages). It holds only small tuples
  such as URL, status and size. If main ever falls behind, workers briefly block
  instead of piling up RAM.
- Writes `visited.tsv`, `failed.tsv` and `done.txt`, flushing about every 2 s.
- Prints progress, including the real total RAM of all crawler processes (psutil).

## Ctrl+C and shutdown

On Windows, Ctrl+C is sent to **every** process in the console. Workers ignore
SIGINT (`signal.SIG_IGN`), and only main reacts:

1. **First Ctrl+C:** main sets a shared `stop` event. Every wait in the crawler
   (`stop.wait`) returns at once, and each thread finishes its current request (at
   most `--request-timeout`). It then saves its site's state and exits. Main keeps
   reading events until all workers are gone.
2. If workers are still alive after `--shutdown-grace` seconds, or on a **second
   Ctrl+C**, main terminates them.
3. Queue feeder threads are cancelled so the process never hangs on exit.

## Resume

- `done.txt` lists finished sites, which are skipped.
- `state/<site>.json` holds the frontier, the seen-set and the counters of an
  interrupted site. That site continues from there and the file is deleted when it finishes.
- WARC file names start with a run id (timestamp), so a resumed run never
  overwrites old WARCs.
- `--no-resume` backs up `done.txt` and ignores saved state.

## Why memory and CPU stay bounded

| Resource | Bound |
|---|---|
| Processes | `1 + --workers` (independent of seed count) |
| Sites in flight | `--workers × --threads` |
| Queue per site | `--max-frontier-per-domain` URLs |
| Seen-set per site | ≤ pages saved + frontier cap (when a page limit is set) |
| One response in RAM | ≤ `--max-page-mb` |
| Messages to main | ≤ 20 000 small tuples |
| CPU | ≤ `--workers` cores, optionally pinned with `--cpus`, below-normal priority |
| Request rate | ≤ 1 request per `--crawl-delay` seconds per site |
| Free RAM floor | fetching pauses below `--min-free-ram-mb` |

## What changed from the old version

| Old | New |
|---|---|
| One process per seed line (~735 processes) | Fixed pool: `--workers` processes × `--threads` |
| Shared `Manager().dict()` for visited (IPC on every link) | Per-site local `set` |
| Links queued again and again; `list.pop(0)` | Queued once (`seen`), capped `deque` |
| Whole response downloaded before checking type | Streamed, type/size checked first, hard cap |
| Unbounded queue of page bodies to one gzip writer | Each worker writes its own WARCs; bounded small events |
| New connection per request, 0.01 s delay | Session per site, 1 s delay, robots Crawl-delay |
| `WARC_SIZE_LIMIT = 500*1024` (500 KB, not MB) | `--warc-max-mb 500` measured on disk |
| Ctrl+C: tracebacks, ~24 min of joins, hangs | Workers ignore SIGINT, bounded grace period, state saved |
| Worker logs lost on Windows | `logs/worker_N.log` per process |
| `verify=False` always | Verified by default, `--no-verify-ssl` to opt out |

## Files

| File | Role |
|---|---|
| `main.py` | Entry point, process pool, event loop, Ctrl+C, progress |
| `crawler.py` | `worker_main` (process) and `DomainCrawler` (per-site crawl) |
| `writers.py` | `RotatingWarcWriter` (workers), `RunLog` (main) |
| `config.py` | Every setting + command-line parsing |
| `utils.py` | URL normalisation, seed loading, logging, CPU/RAM helpers |
