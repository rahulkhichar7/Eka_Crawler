"""main.py blocklist <categories|check|filter>"""
import argparse
import sys

from eka.blocklist.blocklist import PRESETS, Blocklist, add_blocklist_args, list_categories
from eka.core.files import iter_url_lines


def register(sub):
    p = sub.add_parser("blocklist", help="Inspect / apply the adult-gambling-malware blocklists.",
                       description="Blocklists are NOT applied anywhere unless you pass --blocklist.")
    s = p.add_subparsers(dest="cmd", required=True)

    c = s.add_parser("categories", help="List every category and how many entries it has.")
    c.add_argument("--blocklist-dir", default=None)
    c.set_defaults(func=cmd_categories)

    k = s.add_parser("check", help="Check URLs against the lists.")
    k.add_argument("urls", nargs="+")
    add_blocklist_args(k)
    k.set_defaults(func=cmd_check)

    f = s.add_parser("filter", help="Split a URL/seed list into kept and blocked.")
    f.add_argument("--in", dest="inputs", nargs="+", required=True, help="txt/csv files or folders")
    f.add_argument("--out", required=True, help="kept URLs")
    f.add_argument("--removed", default=None, help="blocked URLs with reason (tsv)")
    add_blocklist_args(f)
    f.set_defaults(func=cmd_filter)


def _load(args):
    return Blocklist.load(args.blocklist_dir, args.blocklist or "unsafe", args.blocklist_keywords)


def cmd_categories(args):
    print(f"{'source':18} {'category':26} {'domains':>9} {'urls':>8}  note")
    for src, cat, d, u, note in list_categories(args.blocklist_dir):
        print(f"{src:18} {cat:26} {d:9d} {u:8d}  {note}")
    print("\nPresets:")
    for name, cats in PRESETS.items():
        print(f"  {name:9} = {', '.join(sorted(cats))}")
    return 0


def cmd_check(args):
    bl = _load(args)
    for u in args.urls:
        hit = bl.match(u)
        print(f"{'BLOCKED' if hit else 'ok':8} {u}  {hit or ''}")
    return 0


def cmd_filter(args):
    bl = _load(args)
    kept = blocked = 0
    rem = open(args.removed, "w", encoding="utf-8") if args.removed else None
    with open(args.out, "w", encoding="utf-8") as out:
        for u in iter_url_lines(args.inputs):
            hit = bl.match(u)
            if hit:
                blocked += 1
                if rem:
                    rem.write(f"{u}\t{hit}\n")
            else:
                kept += 1
                out.write(u + "\n")
    if rem:
        rem.close()
    print(f"kept {kept}, blocked {blocked} -> {args.out}")
    return 0
