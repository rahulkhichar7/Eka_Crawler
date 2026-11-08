# Eka Crawler — version 2

The complete Eka data-collection pipeline in one place:

```
 seeds ──► crawl ──► WARC ──► text ──► dataset (final parquet)
             │         │
             │         └──► pdf-urls from-warc ─┐
             └──► pdf_urls.csv ─────────────────┤
 Shodhganga / NDL / Sansad / archive.org ───────┴──► pdf-urls merge ──► download pdfs
 Common Crawl ─────────────────────────► text ──► dataset
```

Everything runs through **one command**, `python main.py <pipeline> <command> [options]`, and
every command has `--help`. Every long job can be **stopped with Ctrl+C and resumed** by running
the same command again. You decide how many **CPU cores / threads / how much RAM** each job may use.

- How it works inside → [CONCEPTS.md](CONCEPTS.md)
- What changed vs. the old code, and when to use what → [REVIEW.md](REVIEW.md)

---

## 1. Install

Python 3.9+.

```bash
cd version2
python -m venv venv
venv\Scripts\activate            # Windows   (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt               # crawl, seeds, pdf-urls, download, blocklist
pip install -r requirements-optional.txt      # text + dataset (trafilatura, pyarrow)
```

## 2. Folder structure

```
version2/
├── main.py                  the only entry point
├── seed.txt                 327,656 unique sites (every seed list from all old folders, merged)
├── requirements.txt         core packages
├── requirements-optional.txt  trafilatura, pyarrow, socks support
├── README.md  CONCEPTS.md  REVIEW.md
│
├── eka/                     the code (a Python package)
│   ├── core/                shared by every pipeline
│   │   ├── http.py          HttpClient: sessions, retries, size caps, streaming downloads
│   │   ├── proxy.py         pluggable proxies (off by default)
│   │   ├── urls.py          URL normalisation, host cleaning, site keys
│   │   ├── html.py          link extraction, page language, encoding detection
│   │   ├── warc.py          WARC writer (rotating) and streaming reader
│   │   ├── resources.py     --workers / --cpus / priority / free-RAM guard
│   │   └── files.py         logging, URL-list readers, resumable "done" logs
│   ├── seeds/               build | from-warc | check-robots
│   ├── crawl/               the crawler (config, frontier, worker, runner)
│   ├── pdf_urls/            from-warc | shodhganga | ndl-iitkgp | sansad | archive-org | merge
│   ├── download/            pdfs | archive-org
│   ├── text/                warc | commoncrawl   (-> text parquet/jsonl)
│   ├── dataset/             combine              (-> final ID/SOURCE/TIME/REMARK/URI/DATA parquet)
│   └── blocklist/           categories | check | filter
│
├── blocklists/              adult / gambling / malware lists — NOT used unless you ask
│   ├── README.md            what each list is, presets, false-positive notes
│   ├── keywords.json        keyword rules (strict / hard / soft)
│   └── sources/             ut1/, blocklistproject/, other/, dolma/, manual/, allow.txt
│
├── data/                    reference inputs kept from the old folders
│   ├── README.md
│   ├── seed_sources/curated_lists/   the 29 hand-made "List of Websites - <language>" sheets
│   └── pdf_urls/shodhganga_pdf_links.txt   20k Shodhganga PDF URLs already collected
│
├── examples/custom_proxy.py  how to write your own proxy class
└── output/                  created when you run things (git-ignored)
```

## 3. Resource controls (same flags everywhere they apply)

| Flag | Meaning |
|---|---|
| `--workers N` | worker **processes**. Each uses at most ~1 CPU core → "how many cores to give it" |
| `--threads N` | parallel network tasks (per worker for `crawl`) — cheap, mostly waiting on servers |
| `--cpus 0,1` / `2-5` | pin to exactly these cores |
| `--low-priority` (default on) | below-normal OS priority, your PC stays responsive |
| `--min-free-ram-mb 1024` | pause new work while free RAM is below this |
| `--dry-run` (crawl) | print plan + estimated RAM, do nothing |

Rough crawl RAM: ~90 MB per worker + ~20 MB per thread (defaults). `--dry-run` prints the estimate.

---

## 4. Tasks

### A. Seeds — which sites to crawl

`seed.txt` is ready (one `https://host` per line; several URLs of the same site = several start points).

```bash
# rebuild / extend: merge any txt or csv files/folders (CSV: every cell starting with http)
python main.py seeds build --in new_list.txt data/seed_sources/curated_lists --out seed.txt

# only NEW sites (not in the master list), and append them to the master  (old Oct-Nov flow)
python main.py seeds build --in new_urls.txt --exclude seed.txt --update-master seed.txt \
       --skip-wiki-google --out feed_new.txt

# keep only .in sites, drop blocklisted ones
python main.py seeds build --in big_list.txt --tld .in --blocklist unsafe --out seed_in.txt

# discover new domains linked from pages you already crawled
python main.py seeds from-warc --warc output/crawl/warc --tld .in --exclude seed.txt --out new_domains.txt

# which seeds allow crawling at all (robots.txt)?
python main.py seeds check-robots --in seed.txt --out-dir output/robots --threads 32
```

### B. Crawl sites → WARC (+ PDF links)

```bash
python main.py crawl --dry-run                     # check the plan first
python main.py crawl                               # defaults: 2 workers x 8 threads, 2000 pages/site
python main.py crawl --workers 4 --threads 10 --max-pages-per-domain 500 --cpus 0-3
python main.py crawl --seed-file my_sites.txt --output-dir output/crawl_my_sites
python main.py crawl --frontier disk               # huge sites: queue lives in SQLite, RAM stays flat
python main.py crawl --no-save-html                # PDF-link crawler: no WARC, only pdf_urls.csv
python main.py crawl --no-verify-ssl               # many gov.in sites have broken certificates
python main.py crawl --help                        # all options
```

Ctrl+C once = save every site's progress and stop. Same command again = resume.
Outputs in `output/crawl/`: `warc/*.warc.gz`, `pdf_urls.csv` (pdf_url, parent_url, parent_lang, site),
`visited.tsv` (url, status, bytes, lang), `failed.tsv`, `done.txt`, `logs/`, `state/`.

### C. Collect PDF URLs

```bash
python main.py pdf-urls from-warc  --warc output/crawl/warc            # PDF links inside crawled pages
python main.py pdf-urls shodhganga --threads 2 --delay 1               # all thesis PDFs (resumable)
python main.py pdf-urls ndl-iitkgp --in ndl_pages.txt                  # NDL viewer pages -> PDF URLs
python main.py pdf-urls sansad --start 1 --end 1302000                 # eparlib API -> JSON + PDF URLs
python main.py pdf-urls archive-org --languages hindi,tamil            # archive.org items per language
```

Merge everything into one list and drop what you already downloaded:

```bash
python main.py pdf-urls merge --in output/crawl/pdf_urls.csv output/pdf_urls data/pdf_urls \
       --exclude output/pdfs/manifest.csv --out output/to_download.txt

# Lok Sabha Q&A sheet (english_pdf / hindi_pdf columns)
python main.py pdf-urls merge --in lok_sabha_questions.csv --column english_pdf hindi_pdf --out lok_sabha.txt
```

### D. Download PDFs

```bash
python main.py download pdfs --in output/to_download.txt --out-dir output/pdfs --threads 8
python main.py download pdfs --in output/pdf_urls/ndl_iitkgp.csv --url-column pdf_url --referer-column referer
```

Files go to `output/pdfs/<host>/<path>__<hash>.pdf`; `manifest.csv` records every URL. Re-running
skips finished and permanently-failed URLs and retries temporary failures.
Only real PDFs are kept (Content-Type PDF or body starting with `%PDF`); login pages saved as
".pdf" are rejected.

### E. archive.org books

```bash
python main.py pdf-urls archive-org --out-dir output/pdf_urls/archive_org     # 1) item lists per language
python main.py download archive-org --in output/pdf_urls/archive_org/hindi_texts.csv \
       --out-dir output/archive_org_items                                     # 2) PDF + OCR text per item
```

### F. Text extraction

```bash
python main.py text warc --warc output/crawl/warc --out-dir output/text/crawl --workers 4
python main.py text commoncrawl --snapshot CC-MAIN-2025-18 --kind wet --max-files 100 --languages hin,ben,tam
python main.py text commoncrawl --snapshot CC-MAIN-2025-18 --kind warc --max-files 20 --workers 4
```

One output file per input file (`URIS, DATA, LANG, WARC_DATE`), parquet if pyarrow is installed
else jsonl. Common Crawl raw files are deleted after processing unless `--keep-raw`.

### G. Final dataset

```bash
python main.py dataset combine --in output/text/crawl --source EC --out-dir output/dataset
python main.py dataset combine --in output/text/commoncrawl --source CC --time file --dedupe-uri
```

Writes `ID_<first>_<last>.parquet` files (rotated at `--max-gb`, default 100) with columns
`ID, SOURCE, TIME, REMARK, URI, DATA`. IDs continue across runs (`state.json`); use
`--start-id` to start a numbering range (the old pipeline used 10_000_000_000 for CC and
13_920_846_253 for EC).

### H. Blocklists (optional — off by default)

We did not filter while crawling. The lists are kept, cleaned and ready if you want them:

```bash
python main.py blocklist categories                              # what is there
python main.py blocklist check https://some-site.in --blocklist unsafe
python main.py blocklist filter --in seed.txt --out seed_clean.txt --removed removed.tsv --blocklist unsafe
python main.py crawl --blocklist unsafe                          # skip blocked sites and links while crawling
python main.py crawl --blocklist adult,gambling --blocklist-keywords strict
```

Presets: `unsafe` (recommended: adult, gambling, malware/phishing/scam), `broad` (more lists,
more false positives), `adult`, `gambling`, `security`, `all`. See `blocklists/README.md`.

### I. Proxies (optional — off by default)

```bash
--proxy none                                  # default: direct
--proxy http://user:pass@1.2.3.4:8080         # one proxy
--proxy file:proxies.txt                      # rotate a list, failing proxies are benched for a while
--proxy "file:proxies.txt?strategy=round_robin&reload=600&max_failures=3&cooldown=300"
--proxy examples/custom_proxy.py:ApiProxyPool?api_url=https://provider/list    # your own class
```

Works on every command that makes requests. Write your own provider by subclassing
`eka.core.proxy.ProxyProvider` and implementing `get(url) -> proxy URL or None`
(see `examples/custom_proxy.py`: API-refreshed pool, sticky-per-host, proxy-only-for-some-hosts).

---

## 5. Old script → new command

| Old | New |
|---|---|
| `version1/`, `September/` + `Oct_Nov/Final_Crawler.py`, `final_2.py`, `July/*.py`, `resume/resume.py` | `main.py crawl` |
| `Oct_Nov/pdf_crawler.py`, `Pdf_Data_Downloading/extract_pdfurls_from_domain.py` (IIT KGP) | `main.py crawl --no-save-html` |
| `pipeline.ipynb` "DiskCache crawler" | `main.py crawl --frontier disk` |
| `pipeline.ipynb` proxy cells, `Oct_Nov/SodhGanga.ipynb` proxy cell | `--proxy ...` on any command |
| `August/*process.ipynb`, `Final_Pipeline/urls_to_new_domains.py`, pipeline cell 2 | `main.py seeds build` |
| pipeline cell 7 "Domain Extractor from Warc" | `main.py seeds from-warc` |
| `July/filter_faster_ones.ipynb` robots checks | `main.py seeds check-robots` |
| `Pdf_Data_Downloading/url_extractor.ipynb` | `main.py pdf-urls from-warc` |
| `SodhGanga/url_extract.ipynb`, pipeline cell 15 | `main.py pdf-urls shodhganga` |
| `Pdf_Data_Downloading/IITKGP/test.py` | `main.py pdf-urls ndl-iitkgp` |
| `September/Sansad_crawl.ipynb` | `main.py pdf-urls sansad` |
| `Archieve_org/download_metadata.py`, `download.ipynb` (metadata) | `main.py pdf-urls archive-org` |
| pipeline cell 4 `PDFUrlCleaner` | `main.py pdf-urls merge` |
| `Oct_Nov/pdf_crawler.ipynb`, `SodhGanga.ipynb` downloaders (Pool 3/500) | `main.py download pdfs --threads N` |
| `Archieve_org/downlaod_by_metadata..py`, `download.ipynb` (download) | `main.py download archive-org` |
| `CC_ParamAnanta/`, `Common Crawl Pipeline/*.py` | `main.py text commoncrawl` / `text warc` |
| `Oct_Nov/parquet_combine_eka.py`, `parquet_pipeline.ipynb`, `August/parquet_combining.ipynb` | `main.py dataset combine` |
| `Blocklisted URLs/` | `blocklists/` + `main.py blocklist ...` / `--blocklist` |

## 6. Troubleshooting

- **PC slow** → lower `--workers` first, then `--threads`; or `--cpus 0,1`.
- **Many `ssl_error`** in `failed.tsv` → `--no-verify-ssl`.
- **Many timeouts** → raise `--timeout` / `--request-timeout`, or lower `--threads`.
- **Site marked done with 0 pages** → unreachable; see `failed.tsv`. Delete its line in `done.txt` to retry.
- **Start over** → `--no-resume`, or a new `--output-dir`.
- **`text` says lxml-fallback** → `pip install trafilatura` for much cleaner text.
- **`dataset combine` fails to import pyarrow** → `pip install pyarrow`.
