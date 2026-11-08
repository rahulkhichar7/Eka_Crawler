# data/

Reference inputs kept from the old folders.

| Path | What |
|---|---|
| `seed_sources/curated_lists/*.csv` | the 29 hand-made "List of Websites - <language / subject>" sheets (August) with topic, language and who added each site |
| `pdf_urls/shodhganga_pdf_links.txt` | 20,075 Shodhganga thesis PDF URLs already collected (input for `download pdfs`) |

## How `../seed.txt` was built

```bash
python main.py seeds build --out seed.txt --in <all files below>
```

Read 2,367,255 lines → **327,656 unique sites** (2,031,244 duplicates, 8,355 invalid lines such as
`kherva-jat-.pincodez.co.in`). No blocklist or keyword filter was applied.

Note: 126,079 of these are sub-domains of `pincodez.co.in` (spam-like pincode pages). Remove
them with `python main.py seeds build --in seed.txt --exclude-keywords pincodez --out seed.txt`.

Sources (old folders):

- `August/`: 19_aug_add, seed, seed_all_aug_16_dedp, seed_unique_19_aug, test, test_dedp,
  `Seeds/*.txt`, `CSVs_17_aug/*.csv` + all_seed.txt
- `July/`: current_urls, deduplicated_seed, deduplicated_student(_2)_urls, math_code(_now), seed,
  seed_student_prev_sum, student_2, students_urls, try_2, unique
- `Oct_Nov/`: 59k_unique_in_domains_from_indicdata_till_oct, All_seed_ex_Sansad, all_seed_16Nov /
  18Nov / 25Nov / 26Oct / 27Nov / incl_64kind, demo_try, feedfile_16nov / 18nov / 25nov / 27nov,
  uniq_in, pdf_domain_16oct
- `resume/`: all_urls, current_urls, try, all_last_urls, already_visited_urls
- `version1/seed.txt`

Not used as seeds: `Oct_Nov/proxy.txt` (proxy credentials), `models.txt`, `model_urls.txt`,
`SodhGanga/shodhganga_pdf_links.txt` (a PDF list, kept here instead).
