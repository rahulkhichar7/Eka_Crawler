# Eka Crawler

Web-data collection for the Eka project: crawl Indian / Indic websites into WARC archives,
collect and download PDFs (Shodhganga, NDL IIT KGP, Sansad, archive.org, any site), extract
clean text from WARC and Common Crawl, and build the final parquet dataset.

## Which version?

| Folder | What it is | Use it when |
|---|---|---|
| [`version2/`](version2/) | **The complete pipeline** with one entry point (`python main.py …`): seeds → crawl → PDF URLs → downloads → text → dataset, plus optional blocklists and proxies | **Default. Use this for everything.** |
| [`version1/`](version1/) | A single, minimal, bounded site crawler (`seed.txt` → WARC) | You only want a small, easy-to-read crawler |

Both let you choose how many CPU cores / threads / how much RAM to use, and both can be
stopped with Ctrl+C and resumed by running the same command again.

## Repository layout

```
Eka_Crawler/
├── README.md            this file
├── version2/            the full pipeline (recommended)
│   ├── main.py          one entry point for every pipeline
│   ├── seed.txt         327,656 unique sites merged from all earlier seed lists
│   ├── eka/             code: core/, seeds/, crawl/, pdf_urls/, download/, text/, dataset/, blocklist/
│   ├── blocklists/      adult / gambling / malware lists (off by default)
│   ├── data/            curated website sheets, Shodhganga PDF list
│   ├── examples/        custom proxy classes
│   ├── README.md        folder structure + how to run each task
│   ├── CONCEPTS.md      how and why it works (processes, frontiers, backpressure, resume …)
│   └── REVIEW.md        version1 vs version2, what was merged / dropped, known limits
└── version1/            the minimal crawler
    ├── main.py, crawler.py, config.py, utils.py, writers.py
    ├── README.md        how to use
    └── WORKING.md       how it works
```

## Quick start (version2)

```bash
cd version2
python -m venv venv
venv\Scripts\activate                      # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt            # + requirements-optional.txt for text/dataset

python main.py --help                      # list of pipelines
python main.py crawl --dry-run             # plan + RAM estimate for seed.txt
python main.py crawl --workers 2 --threads 8
```

The main tasks:

```bash
python main.py seeds build --in new_sites.txt --exclude seed.txt --update-master seed.txt --out feed.txt
python main.py crawl --seed-file feed.txt                     # -> output/crawl/warc + pdf_urls.csv
python main.py pdf-urls shodhganga                            # or ndl-iitkgp / sansad / archive-org / from-warc
python main.py pdf-urls merge --in output/crawl/pdf_urls.csv output/pdf_urls --out to_download.txt
python main.py download pdfs --in to_download.txt --threads 8
python main.py text warc --warc output/crawl/warc             # or: text commoncrawl --snapshot CC-MAIN-2025-18
python main.py dataset combine --in output/text/crawl --source EC
```

Full instructions: [`version2/README.md`](version2/README.md).

## Notes

- `seed.txt` is not filtered. 126k of its entries are sub-domains of `pincodez.co.in`
  (spam-like pages); drop them with
  `python main.py seeds build --in seed.txt --exclude-keywords pincodez --out seed.txt`.
- Blocklists and proxies are **off** unless you pass `--blocklist …` / `--proxy …`.
- Keep proxy lists and credentials **out of the repository** (point to them with `--proxy file:…`).
- Everything a run produces goes to `output/` (git-ignored).
