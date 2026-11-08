# Blocklists (optional)

Lists of adult, gambling, malware, phishing … websites. **Nothing in the pipeline uses them
unless you pass `--blocklist …`** — we did not filter while crawling. Many entries are
old/outdated, and some lists contain false positives (see presets below).

## What is here

| Folder | Source | Format |
|---|---|---|
| `sources/ut1/<category>/` | UT1 Toulouse blacklists (the lists RefinedWeb used) — 68 categories | `domains`, `urls`, `usage` (+ unused regex `expressions`) |
| `sources/blocklistproject/<category>.txt` | The Block List Project (blocklist.site) — 17 categories | one domain per line (converted from `0.0.0.0 domain`) |
| `sources/other/india_court_orders/urls` | URLs blocked in India by court/government orders (thejeshgn lists: MSM world cup, Kabali, medianama, kafila) | exact URLs |
| `sources/dolma/subreddit_blocklist.txt` | Dolma's NSFW subreddit names | matched against the first host label |
| `sources/manual/gambling_adult.txt` | the hand-made gambling/adult list from `blocklisted_urls.ipynb` | one domain per line |
| `sources/allow.txt` | sites that are **never** blocked (`gov.in`, `nic.in`, `ac.in`, `edu.in`, archive.org, gutenberg, wikipedia…) | domain = also all sub-domains |
| `keywords.json` | keyword rules per category: `strict` (specific substrings), `hard` (whole tokens), `soft` (loose) | JSON |

UT1's `adult` category has only its `urls` file in this copy (the big `domains` file was
never downloaded); adult sites are still covered by `manual`, `mixed_adult` and keywords.
To refresh UT1: download `blacklists.tar.gz` from dsi.ut-capitole.fr and replace `sources/ut1/`.

## Presets

| Preset | Categories | Notes |
|---|---|---|
| `unsafe` (recommended) | adult, mixed_adult, gambling, arjel, malware, phishing, cryptojacking, ddos, stalkerware, ransomware, scam, manual | on the 327k `seed.txt`: 68 sites blocked, all gambling / crypto-mining |
| `broad` | unsafe + lingerie, dating, crypto, hacking, warez, piracy, torrent, drugs, drogue, agressif, dangerous_material | more false positives: matrimony sites (dating), archive.org/gutenberg (torrent), some news (drugs) — allow-list protects the important ones |
| `adult`, `gambling`, `security` | subsets | |
| `all` | every list, including harmless UT1 categories (press, shopping, sports, bank, blog …) | rarely what you want |
| any name | e.g. `--blocklist dating,india_court_orders,dolma_nsfw` | `dolma_nsfw` is never in a preset: it blocks e.g. `apply.<university>.ac.in` |

## Use

```bash
python main.py blocklist categories
python main.py blocklist check https://x.com https://y.in --blocklist unsafe
python main.py blocklist filter --in seed.txt --out seed_clean.txt --removed removed.tsv --blocklist unsafe
python main.py seeds build --in lists/ --out seed.txt --blocklist unsafe
python main.py crawl --blocklist unsafe --blocklist-keywords strict
python main.py text warc --blocklist adult
```

To add your own list: create `sources/other/<my_category>/domains` (and/or `urls`), then
`--blocklist my_category`. To never block a site: add it to `sources/allow.txt`.
