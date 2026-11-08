"""HTML parsing helpers (lxml)."""
import re
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lxml_html

_SKIP_PREFIXES = ("#", "javascript:", "mailto:", "tel:", "data:", "whatsapp:", "sms:")


_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*['\"]?\s*([a-zA-Z0-9_-]+)", re.I)


def charset_from_content_type(ctype):
    m = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", ctype or "", re.I)
    return m.group(1).lower() if m else None


def sniff_encoding(body, declared=None):
    """HTTP charset > <meta charset> > valid UTF-8 > let lxml guess.
    (lxml alone treats undeclared bytes as Latin-1, which garbles every Indic page.)"""
    if declared:
        return declared
    head = body[:4096]
    m = _META_CHARSET.search(head)
    if m:
        return m.group(1).decode("ascii", "ignore").lower()
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return None


def parse(body, encoding=None):
    try:
        if isinstance(body, bytes):
            enc = sniff_encoding(body, encoding)
            if enc:
                try:
                    return lxml_html.fromstring(body, parser=lxml_html.HTMLParser(encoding=enc))
                except LookupError:  # unknown charset name
                    pass
        return lxml_html.fromstring(body)
    except (etree.ParserError, ValueError, TypeError):
        return None


def page_lang(doc):
    if doc is None:
        return ""
    v = doc.xpath("string(/html/@lang)") or doc.xpath("string(//html/@lang)")
    return (v or "").strip()[:20]


def extract_links(doc, base_url, tags=("a",)):
    """Absolute URLs from href/src of the given tags. Respects <base href>."""
    if doc is None:
        return []
    base = doc.xpath("//base/@href")
    if base:
        try:
            base_url = urljoin(base_url, base[0].strip())
        except ValueError:
            pass
    out = []
    for tag in tags:
        for el in doc.iter(tag):
            ref = el.get("href") or el.get("src")
            if not ref:
                continue
            ref = ref.strip()
            if not ref or ref.lower().startswith(_SKIP_PREFIXES):
                continue
            try:
                out.append(urljoin(base_url, ref))
            except ValueError:
                continue
    return out


def visible_text(doc):
    """Fallback text extractor when trafilatura is not installed."""
    if doc is None:
        return ""
    for bad in doc.xpath("//head|//script|//style|//noscript|//template|//svg|//nav|//footer|//header"):
        bad.drop_tree()
    text = doc.text_content()
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return "\n".join(l for l in lines if l)
