"""URL and host helpers shared by every pipeline."""
import os
import re
from urllib.parse import unquote, urlsplit, urlunsplit

# File types that are never HTML. The crawler never requests these.
SKIP_EXTENSIONS = frozenset("""
    .pdf .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .rtf .csv .txt .xml .json .epub
    .zip .rar .7z .gz .tgz .tar .bz2 .xz .iso .dmg .exe .msi .apk .bin .deb .rpm
    .jpg .jpeg .png .gif .bmp .webp .svg .ico .tif .tiff .psd .heic
    .mp3 .wav .ogg .flac .aac .m4a .wma .mp4 .avi .mov .mkv .wmv .flv .webm .m4v .3gp
    .css .js .woff .woff2 .ttf .eot .otf .swf .dwg .kml .kmz .shp
""".split())

# a real public hostname: labels of a-z0-9-, at least one dot, alphabetic TLD (or xn--)
_HOST_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")


def normalize_url(url, keep_query=False):
    """Canonical absolute http(s) URL, or None if unusable.

    lowercases scheme/host, drops default port and #fragment, empty path -> "/",
    drops ?query unless keep_query.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except (ValueError, AttributeError):
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = parts.path or "/"
    query = parts.query if keep_query else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def site_key(url_or_host):
    """host[:port] without leading 'www.' - example.com and www.example.com are one site."""
    host = urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host
    host = host.rsplit("@", 1)[-1].lower()
    return host[4:] if host.startswith("www.") else host


def url_key(url):
    """Identity of a URL for de-duplication: ignores http/https and 'www.'."""
    rest = url.split("://", 1)[-1]
    return rest[4:] if rest.startswith("www.") else rest


def path_extension(url):
    try:
        return os.path.splitext(urlsplit(url).path.lower())[1]
    except ValueError:
        return ""


def has_skipped_extension(url):
    return path_extension(url) in SKIP_EXTENSIONS


def is_pdf_url(url):
    return path_extension(url) == ".pdf"


def clean_host(raw):
    """Extract a valid hostname from a messy seed line, or None.

    Handles '%20' and spaces ('http://   www.eai.in'), missing scheme,
    'user@host' (emails), ports, trailing paths, IDN hosts.
    """
    if not raw:
        return None
    s = unquote(str(raw)).strip().strip("\"'<>[](),;")
    s = re.sub(r"\s+", "", s).replace("\\", "/")
    s = re.sub(r"^(?:(?:https?|hxxps?|ftps?|mailto):/*)+", "", s, flags=re.I)  # 'http://https://x'
    s = s.lstrip("([{<'\"`.,;:/")                                  # '(www.site.in', '//site.in'
    s = re.sub(r"\.{2,}", ".", s)                                  # 'irctc..co.in'
    if not s:
        return None
    s = "http://" + s
    try:
        host = urlsplit(s).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.strip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    return host if _HOST_RE.fullmatch(host) else None


def scheme_of(raw):
    s = unquote(str(raw)).strip().lower()
    if s.startswith("https://"):
        return "https"
    if s.startswith("http://"):
        return "http"
    return None
