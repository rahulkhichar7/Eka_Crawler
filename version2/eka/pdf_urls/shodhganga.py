"""Shodhganga (INFLIBNET thesis repository) -> PDF URLs.

Walks the browse-by-title listing (100 theses per page), opens every thesis
handle page and collects its /bitstream/...pdf links.

Resumable: the next browse offset is stored in <out>.state after each page.
Polite: --delay seconds between requests, --threads item pages in parallel.
"""
import concurrent.futures as cf
import json
import os
import time
from urllib.parse import urljoin

from eka.core.html import parse

BASE = "https://shodhganga.inflibnet.ac.in"
BROWSE = BASE + "/browse?type=title&sort_by=1&order=ASC&rpp=100&etal=0&offset={}"


def _handles(doc):
    out = []
    for href in doc.xpath("//a/@href") if doc is not None else []:
        if "/handle/" in href and "browse" not in href and "search" not in href:
            u = urljoin(BASE, href)
            if u not in out:
                out.append(u)
    return out


def _pdfs(doc, page_url):
    out = []
    for href in doc.xpath("//a/@href") if doc is not None else []:
        if "/bitstream/" in href and href.lower().split("?")[0].endswith(".pdf"):
            u = urljoin(page_url, href)
            if u not in out:
                out.append(u)
    return out


def run(client, out_path, start_offset=None, end_offset=None, delay=1.0, threads=2, stop=None):
    state_path = out_path + ".state"
    offset = 0
    if start_offset is not None:
        offset = start_offset
    elif os.path.exists(state_path):
        with open(state_path) as f:
            offset = json.load(f)["next_offset"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    print(f"Shodhganga: starting at offset {offset} -> {out_path}")
    total = 0

    def get_doc(url):
        time.sleep(delay)
        with client.get(url) as r:
            if r.status_code != 200:
                return None
            return parse(r.content)

    def item(url):
        try:
            return url, _pdfs(get_doc(url), url), None
        except Exception as e:
            return url, [], type(e).__name__

    with open(out_path, "a", encoding="utf-8") as out, cf.ThreadPoolExecutor(threads) as ex:
        while end_offset is None or offset < end_offset:
            if stop is not None and stop.is_set():
                break
            try:
                doc = get_doc(BROWSE.format(offset))
            except Exception as e:
                print(f"  browse offset {offset} failed: {e}; retrying in 30s", flush=True)
                time.sleep(30)
                continue
            handles = _handles(doc)
            if not handles:
                print(f"  no theses at offset {offset}: finished (or blocked).")
                break
            n = 0
            for url, pdfs, err in ex.map(item, handles):
                for p in pdfs:
                    out.write(p + "\n")
                n += len(pdfs)
                if err:
                    print(f"    ! {url}: {err}")
            out.flush()
            total += n
            offset += 100
            with open(state_path, "w") as f:
                json.dump({"next_offset": offset}, f)
            print(f"  offset {offset - 100}: {len(handles)} theses, {n} PDFs (total {total})", flush=True)
    return 0
