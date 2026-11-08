"""National Digital Library (ndl.iitkgp.ac.in) document pages -> real PDF URLs.

An NDL document page shows the PDF inside an iframe viewer; the viewer page
contains   PDFViewerApplication.open({"url":"/path/to.pdf"}).
We follow page -> iframe (with Referer) -> regex -> absolute PDF URL.

Input: a txt/csv list of NDL document page URLs.
Output CSV: page_url, pdf_url, referer   (download with:  main.py download pdfs --referer-column referer)
"""
import concurrent.futures as cf
import csv
import os
import re
import time
from urllib.parse import urljoin

from eka.core.files import DoneLog, iter_url_lines
from eka.core.html import parse

BASE = "http://ndl.iitkgp.ac.in"
PDF_RE = re.compile(r'PDFViewerApplication\.open\(\{"url":"(.*?)"\}\)')


def resolve(client, page_url):
    with client.get(page_url) as r:
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        doc = parse(r.content)
    frames = doc.xpath('//*[@id="content-holder"]/@src') if doc is not None else []
    if not frames:
        frames = doc.xpath("//iframe/@src") if doc is not None else []
    if not frames:
        return None, "no_viewer_iframe"
    viewer = urljoin(page_url, frames[0])
    with client.get(viewer, headers={"Referer": page_url}) as r:
        m = PDF_RE.search(r.text)
    if not m:
        return None, "no_pdf_in_viewer"
    return urljoin(BASE, m.group(1).replace("\\/", "/")), None


def run(client, inputs, out_csv, threads=4, delay=0.5):
    pages = list(dict.fromkeys(iter_url_lines(inputs)))
    done = DoneLog(out_csv + ".done")
    todo = [p for p in pages if p not in done]
    print(f"NDL: {len(pages)} pages, {len(pages) - len(todo)} done, {len(todo)} to resolve")
    new = not os.path.exists(out_csv)
    with open(out_csv, "a", encoding="utf-8", newline="") as f, cf.ThreadPoolExecutor(threads) as ex:
        w = csv.writer(f)
        if new:
            w.writerow(["page_url", "pdf_url", "referer"])

        def job(p):
            time.sleep(delay)
            try:
                return (p,) + resolve(client, p)
            except Exception as e:
                return p, None, type(e).__name__

        for i, (page, pdf, err) in enumerate(ex.map(job, todo), 1):
            if pdf:
                w.writerow([page, pdf, page])
            done.add(page, err or "ok")
            if i % 50 == 0:
                f.flush()
                print(f"  {i}/{len(todo)}", flush=True)
    done.close()
    print(f"Done -> {out_csv}")
    return 0
