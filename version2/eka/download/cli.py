"""main.py download <pdfs|archive-org>"""
from eka.core.http import HttpClient, add_http_args


def register(sub):
    p = sub.add_parser("download", help="Download PDFs (any URL list) or archive.org items.")
    s = p.add_subparsers(dest="cmd", required=True)

    d = s.add_parser("pdfs", help="Download every URL in txt/csv lists (resumable).")
    d.add_argument("--in", dest="inputs", nargs="+", required=True, help="txt/csv URL lists")
    d.add_argument("--out-dir", default="output/pdfs")
    d.add_argument("--threads", type=int, default=8, help="parallel downloads")
    d.add_argument("--per-host-delay", type=float, default=0.5,
                   help="min seconds between two requests to the same host")
    d.add_argument("--max-mb", type=float, default=200, help="skip files bigger than this")
    d.add_argument("--any-type", action="store_true", help="save whatever comes back (not only PDF)")
    d.add_argument("--no-auto-referer", dest="auto_referer", action="store_false",
                   help="don't send the URL's folder as Referer (some servers need it)")
    d.add_argument("--url-column", default=None, help="CSV column with the URL (default pdf_url)")
    d.add_argument("--referer-column", default=None, help="CSV column with a Referer per URL")
    d.add_argument("--flat", action="store_true", help="no per-host sub-folders")
    d.add_argument("--min-free-ram-mb", type=int, default=1024)
    add_http_args(d, timeout=60, retries=2)
    d.set_defaults(func=_pdfs)

    a = s.add_parser("archive-org", help="Download archive.org items (PDF + OCR text).")
    a.add_argument("--in", dest="inputs", nargs="+", required=True,
                   help="CSV(s) from 'pdf-urls archive-org' or txt identifiers")
    a.add_argument("--out-dir", default="output/archive_org_items")
    a.add_argument("--formats", default="pdf,djvutxt,txt", help="format/extension substrings to keep")
    a.add_argument("--no-require-text", dest="require_text", action="store_false",
                   help="also take items without OCR / text")
    a.add_argument("--threads", type=int, default=4)
    a.add_argument("--max-mb", type=float, default=500)
    add_http_args(a, timeout=120, retries=3)
    a.set_defaults(func=_archive)


def _pdfs(a):
    from eka.download import pdfs
    return pdfs.run(HttpClient.from_args(a), a.inputs, a.out_dir, a.threads, a.per_host_delay,
                    a.max_mb, a.any_type, a.auto_referer, a.url_column, a.referer_column,
                    not a.flat, a.min_free_ram_mb)


def _archive(a):
    from eka.download import archive_org
    fmts = tuple(f.strip() for f in a.formats.split(",") if f.strip())
    return archive_org.run(HttpClient.from_args(a), a.inputs, a.out_dir, fmts, a.require_text,
                           a.threads, a.max_mb)
