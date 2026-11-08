"""Sansad (Parliament of India) digital library -> JSON pages + PDF URLs.

eparlib.sansad.in has a REST API that returns records in ranges:
    /restv3/fetch/all?start=S&rows=N&order=date_desc&locale=en
We save each range as JSON (skipped if it already exists = resumable) and pull
every string that looks like a PDF link out of it into pdf_urls.csv.

For the Lok Sabha Q&A sheet (CSV with english_pdf / hindi_pdf columns) no
special code is needed:  main.py pdf-urls merge --in sheet.csv --column english_pdf hindi_pdf
"""
import csv
import json
import os
import time

API = "https://eparlib.sansad.in/restv3/fetch/all?start={start}&rows={rows}&order=date_desc&locale=en"


def _pdf_strings(obj, out):
    if isinstance(obj, dict):
        for v in obj.values():
            _pdf_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _pdf_strings(v, out)
    elif isinstance(obj, str):
        s = obj.strip()
        if ".pdf" in s.lower() and (s.startswith(("http://", "https://", "/"))):
            out.append(s if s.startswith("http") else "https://eparlib.sansad.in" + s)


def run(client, out_dir, start, end, rows=1000, delay=1.0):
    os.makedirs(out_dir, exist_ok=True)
    pdf_csv = os.path.join(out_dir, "pdf_urls.csv")
    new = not os.path.exists(pdf_csv)
    errors = open(os.path.join(out_dir, "errors.txt"), "a", encoding="utf-8")
    with open(pdf_csv, "a", encoding="utf-8", newline="") as pf:
        w = csv.writer(pf)
        if new:
            w.writerow(["pdf_url", "source_json"])
        for s in range(start, end, rows):
            name = f"{s}__{s + rows - 1}.json"
            path = os.path.join(out_dir, name)
            if os.path.exists(path):
                continue
            t = time.time()
            try:
                with client.get(API.format(start=s, rows=rows), headers={"Accept": "application/json"}) as r:
                    r.raise_for_status()
                    data = r.json()
            except Exception as e:
                errors.write(f"{s}-{s + rows - 1}\t{e}\n")
                errors.flush()
                print(f"  {name} failed: {e}", flush=True)
                time.sleep(delay)
                continue
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            pdfs = []
            _pdf_strings(data, pdfs)
            for p in dict.fromkeys(pdfs):
                w.writerow([p, name])
            pf.flush()
            print(f"  {name}: {len(pdfs)} PDF links ({time.time() - t:.1f}s)", flush=True)
            time.sleep(delay)
    errors.close()
    return 0
