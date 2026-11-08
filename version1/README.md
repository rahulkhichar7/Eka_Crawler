# Eka Crawler

A multi-process web crawler that saves HTML pages to WARC files. It crawls
every site in `seed.txt`, stays inside each site, obeys robots.txt, and can be
stopped and resumed at any time.

**You decide how much of your PC it uses**: how many CPU cores, how many sites
at once, how much RAM must stay free. Start small and go up.

How it works inside: see [WORKING.md](WORKING.md).

---

## 1. Install

Python 3.9 or newer.

```bash
cd version1
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
```

## 2. Seed file

`seed.txt` has one site per line. The `https://` is optional, and `#` starts a comment.
Duplicate sites (also `www.` vs no `www.`) are removed automatically.

```
https://dpiit.gov.in
darpg.gov.in
# this line is ignored
```

## 3. Check the plan first (no crawling)

```bash
python main.py --dry-run
```

```
Seeds: 693 unique sites (42 duplicates, 0 invalid skipped); 0 already done; 693 to crawl.
Resources: 2 worker processes x 8 threads = 16 sites at once | CPU cores: any (PC has 8) | est. peak RAM ~664 MB
Limits: depth 5, 2000 pages/site, 1.0s delay, max page 5.0 MB, output -> output
```

## 4. Run

```bash
python main.py
```

Every 10 seconds it prints a progress line:

```
[00:12:40] domains 41/693 done, 16 active | pages 18210 (24.0/s, 610 MB) | failed 312 | crawler RAM 410 MB | free RAM 9.8 GB
```

**Stop:** press `Ctrl+C` once. Every site in progress saves its queue, and the
crawler exits within `--shutdown-grace` seconds (default 30). A second `Ctrl+C`
force-quits, but then sites in progress lose their saved queue.

**Resume:** run the same command again. Finished sites are skipped and
interrupted sites continue where they stopped. Use `--no-resume` to start over.

---

## Choosing resources

| You want | Use |
|---|---|
| Light, PC stays usable | `--workers 1 --threads 4` |
| Default | `--workers 2 --threads 8` |
| Heavier crawl on an 8-core PC | `--workers 4 --threads 10` |
| Use only cores 2 and 3 | `--workers 2 --cpus 2,3` |
| Never let free RAM drop under 2 GB | `--min-free-ram-mb 2048` |

- **`--workers`** = processes. Each one uses **at most about 1 CPU core**, so this is
  effectively "how many cores to give it". Keep it below your core count.
- **`--threads`** = sites crawled at once *per worker*. Crawling mostly waits on
  the network, so threads cost little CPU. Sites in flight = workers × threads.
- **`--cpus`** pins all crawler processes to specific cores (`0,1`, `2-5`, …).
- **`--low-priority`** (on by default) runs below normal priority, so your other
  apps win when they need the CPU. Turn it off with `--no-low-priority`.
- **`--min-free-ram-mb`** pauses fetching while free system RAM is below this.

Rough RAM: about **70 MB per worker + about 20 MB per thread** with the default limits.
`--dry-run` prints the estimate for your settings.

## All options

Run `python main.py --help` for the full list with defaults. The defaults live in
`config.py`; edit them there to change them permanently.

| Option | Default | Meaning |
|---|---|---|
| `--seed-file` | `seed.txt` | Sites to crawl |
| `--output-dir` | `output` | Where everything is written |
| `--workers` | 2 | Worker processes (≈ CPU cores used) |
| `--threads` | 8 | Sites at once per worker |
| `--cpus` | any | Pin to cores, e.g. `0,1` or `2-5` |
| `--low-priority / --no-low-priority` | on | Below-normal OS priority |
| `--min-free-ram-mb` | 1024 | Pause when free RAM is below this (0 = off) |
| `--max-depth` | 5 | Link hops from the start page |
| `--max-pages-per-domain` | 2000 | HTML pages saved per site (0 = no limit) |
| `--max-frontier-per-domain` | 20000 | Max queued URLs per site (caps RAM) |
| `--max-page-mb` | 5 | Bigger responses are skipped |
| `--request-timeout` | 15 | Seconds per connect / read |
| `--retries` | 1 | Extra tries on timeout, 429, 5xx |
| `--crawl-delay` | 1.0 | Seconds between requests to the same site |
| `--respect-robots / --no-respect-robots` | on | Obey robots.txt |
| `--keep-query / --no-keep-query` | off | Keep `?query` in URLs |
| `--verify-ssl / --no-verify-ssl` | on | Check HTTPS certificates |
| `--user-agent` | `EkaCurator/1.0` | User-Agent header |
| `--warc-max-mb` | 500 | Rotate WARC file at this size (compressed) |
| `--resume / --no-resume` | on | Continue the previous run |
| `--progress-every` | 10 | Seconds between progress lines |
| `--shutdown-grace` | 30 | Seconds to wait after Ctrl+C |
| `--log-level` | INFO | `DEBUG` logs every URL |
| `--dry-run` | off | Print plan and exit |

Many Indian government sites have broken HTTPS certificates. If `failed.tsv`
shows a lot of `ssl_error`, rerun with `--no-verify-ssl`.

## Output

```
output/
├── warc/          <run>-w00-00000.warc.gz …  one series per worker, rotated at --warc-max-mb
├── visited.tsv    url <tab> status <tab> bytes      every saved page
├── failed.tsv     url <tab> reason                  timeouts, HTTP errors, SSL errors
├── done.txt       sites fully finished (skipped on resume)
├── state/         queue of each interrupted site (deleted when the site finishes)
└── logs/          main.log, worker_N.log (rotated at 20 MB)
```

Read the WARCs with `warcio`:

```python
from warcio.archiveiterator import ArchiveIterator
with open("output/warc/....warc.gz", "rb") as f:
    for rec in ArchiveIterator(f):
        if rec.rec_type == "response":
            print(rec.rec_headers.get_header("WARC-Target-URI"), len(rec.content_stream().read()))
```

## Troubleshooting

- **PC still feels slow:** lower `--workers` first, then `--threads`. Or pin to a few
  cores with `--cpus`.
- **Lots of `ReadTimeout`:** raise `--request-timeout`, or lower `--threads` if your
  connection is saturated.
- **A site was "done" with 0 pages:** it was unreachable. Check `failed.tsv`. To retry it,
  delete its line from `output/done.txt`.
- **Want a fresh crawl:** `--no-resume`, or use a new `--output-dir`.
