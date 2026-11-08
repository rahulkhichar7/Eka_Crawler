# Review — version1 vs version2, and when to use what

## In one paragraph each

**version1** is a single, focused tool: a bounded, resumable multi-process **site crawler**
(`seed.txt` → WARC files). It is the fixed version of the original crawler that crashed RAM
and CPU (one process per seed, exponential queues, full downloads in RAM, unbounded queues).
Small, easy to read, no other features.

**version2** is the **whole Eka data pipeline** in one package with one entry point
(`python main.py …`): seed management, the crawler (version1's design plus PDF-link
collection, page language, disk frontier, proxy and blocklist support), PDF-URL collectors
for Shodhganga / NDL IIT KGP / Sansad / archive.org / WARC files, a resumable PDF downloader,
archive.org downloader, WARC & Common Crawl text extraction, the final parquet dataset builder,
and the cleaned blocklists. Everything shares one HTTP client, one resource-control system
and the same stop/resume behaviour.

## When to use what

| You want to… | Use |
|---|---|
| Just crawl a list of sites into WARC, nothing else | version1 `python main.py` (or version2 `crawl`) |
| Crawl and also get the PDF links (with page language) | version2 `crawl` |
| Find PDFs on sites without storing HTML | version2 `crawl --no-save-html` |
| Crawl very large sites (millions of URLs) | version2 `crawl --frontier disk` |
| Build / refresh / de-duplicate seed lists, find new domains | version2 `seeds` |
| Shodhganga / NDL / Sansad / archive.org | version2 `pdf-urls …` then `download …` |
| Download a big PDF URL list safely | version2 `download pdfs` |
| Text from your WARCs or Common Crawl | version2 `text …` |
| Build the final ID/SOURCE/TIME/REMARK/URI/DATA parquet | version2 `dataset combine` |
| Remove adult / gambling / malware sites | version2 `blocklist` or `--blocklist unsafe` |
| Use proxies | version2 `--proxy …` |

**Recommendation:** use version2 for everything. version1 stays as the minimal reference
crawler (and is what version2's crawler was built from).

## Typical end-to-end runs

**Indic web crawl**
```
seeds build (new sites) → crawl → text warc → dataset combine --source EC
                         ↘ seeds from-warc --tld .in (new domains for the next round)
                         ↘ pdf_urls.csv → pdf-urls merge → download pdfs
```

**PDF collection**
```
pdf-urls shodhganga / ndl-iitkgp / sansad / archive-org / from-warc
→ pdf-urls merge --exclude output/pdfs/manifest.csv → download pdfs
```

**Common Crawl**
```
text commoncrawl --snapshot CC-MAIN-… → dataset combine --source CC --time file
```

## What was kept, merged, or dropped from the old folders

| Old | Status |
|---|---|
| version1, Final_Crawler (Sep/Oct), final_2, July crawlers, resume.py, pdf_crawler.py, DiskCache crawler, IIT KGP domain crawler | **merged** into one crawler (`crawl`) with flags |
| seed notebooks, DomainExtractor, WARC domain extractor, robots checks | **merged** into `seeds` |
| url_extractor (PDF links from WARC), Shodhganga, NDL, Sansad, archive.org metadata, PDFUrlCleaner | **kept** as `pdf-urls` sources |
| Pool(3)/Pool(500) PDF downloaders, Lok Sabha downloader, "any txt" downloader | **merged** into `download pdfs` (threads, per-host delay, resume, real-PDF check) |
| archive.org download notebook | `download archive-org` |
| CC_ParamAnanta, Common Crawl Pipeline scripts | **merged** into `text commoncrawl` / `text warc` |
| parquet combiners (EC + CC versions) | **merged** into `dataset combine` |
| Blocklisted URLs | **cleaned** into `blocklists/` (one copy per list, plain format, allow-list, presets) |
| all seed .txt/.csv files (40+, 2.37 M lines) | **merged** into `seed.txt` (327,656 sites) |
| curated "List of Websites" sheets, Shodhganga PDF list | **kept** in `data/` |
| proxy-fetch stubs, hard-coded proxy files | **dropped** — replaced by the `--proxy` plug-in |
| benchmark / timing scripts & JSON, visualisation notebooks, test notebooks, `hi.html`, model lists | **dropped** (experiments, not pipeline) |
| derived blocklist CSVs, 4 duplicate Dolma copies, adguard/dnsmasq/alt duplicates | **dropped** (rebuilt from sources) |

The old folders were **not deleted**. version2 is self-contained, so you can delete or
archive them when you are happy.

## Bugs in the old code that version2 fixes

- One process per seed (RAM/CPU crash), global `Manager().dict()` visited set (CPU + RAM leak).
- Links re-queued from every page (exponential queues); `list.pop(0)`.
- Full responses (incl. PDFs/videos) loaded into RAM to check their type; no size cap.
- Unbounded queues of page bodies into a single WARC writer.
- `WARC_SIZE_LIMIT = 500*1024` (500 KB) in version1; WARC headers claiming gzip on decoded bodies.
- Pages without `<meta charset>` decoded as Latin-1 (garbled Indic text).
- `resume.py`: `manager.list().popleft()` → every domain failed silently.
- `July/actual.py`: wrong indentation → only the last link of each page was queued.
- `scraper_srajan.py`: `rp_dict = []` indexed by string, `link['href']` on a string → crash.
- PDF downloaders: 500 processes; file name "add `_` until unique" broke resume; HTML login
  pages saved as `.pdf`; no temp file → half files looked complete.
- Combiners loading whole parquet files into RAM; marking inputs done before their output was safe.
- Ctrl+C: hundreds of tracebacks, ~24 min of joins, hangs on exit.

## Known limitations / what was not tested here

Tested end-to-end on Linux against local test servers (infinite link sites, broken
https, large/slow pages, fake PDFs, 404s, a local forward proxy, and mock Shodhganga / NDL /
Sansad / archive.org servers): crawl (memory + disk frontier, Ctrl+C and resume, https→http
fallback, robots, PDF links, proxy, blocklist), seeds, pdf-urls (all sources + merge),
download pdfs (resume, real-PDF check), text warc (jsonl), blocklists on the real lists.
Seed building and blocklist filtering ran on your PC against the real files.

Not verified here:

- `warcio`, `protego`, `trafilatura`, `pyarrow` were not installable in the test machine;
  stand-ins were used for warcio/protego. **`dataset combine` and parquet output were not run**
  (they need pyarrow) — run them once on a small folder first.
- Real external sites were not contacted: Shodhganga, NDL, eparlib and archive.org page
  structures are taken from the old code and may have changed.
- Windows-specific Ctrl+C behaviour was designed for but not exercised on Windows.
- `seed.txt` is **not** filtered (you did not filter while crawling). 98% of it is `.in`.
  **126,079 of the 327,656 sites (38%) are sub-domains of `pincodez.co.in`** (one thin
  pincode page per post office, picked up by the `.in` domain discovery). They are almost
  certainly not worth crawling; to drop them:
  `python main.py seeds build --in seed.txt --exclude-keywords pincodez --out seed.txt`
  (→ ~201k sites). Other big families are legitimate (`*.pci.ac.in` 2k, `*.kvs.ac.in` 1.3k,
  `*.nic.in` 5.3k, `*.gov.in` 18.9k). `--blocklist unsafe` would remove another 68.
- Seeds use the hostname only (port and path are dropped by `seeds build`; `crawl` itself
  keeps full start URLs if you give them).

## Security note

`Oct_Nov/proxy.txt` contains proxy **usernames and passwords** in plain text and sits inside a
git repository. It was not copied into version2. Consider deleting it and rotating those
credentials; keep proxy lists outside the repo (or in a git-ignored file) and point to them
with `--proxy file:…`.
