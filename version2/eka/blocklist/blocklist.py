"""Domain / URL blocklists (adult, gambling, malware, ...). OFF by default.

The lists live in version2/blocklists/sources/ (see blocklists/README.md):

    ut1/<category>/domains|urls      UT1 Toulouse lists (used by RefinedWeb)
    other/<category>/domains|urls    any extra list in the same layout (e.g. india_court_orders)
    blocklistproject/<category>.txt  The Block List Project, one domain per line
    dolma/subreddit_blocklist.txt    Dolma's NSFW subreddit names (matched as domain labels)
    manual/*.txt                     hand-made lists (one domain per line)
    keywords.json                    keyword rules: strict (substring), hard (whole token), soft

Usage:
    bl = Blocklist.load("blocklists", categories="unsafe")    # or "adult,gambling" or "all"
    bl.match("https://www.example-casino.com/x")              # -> "ut1:gambling" or None
"""
import json
import logging
import os
import re

log = logging.getLogger("eka.blocklist")

DEFAULT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "blocklists")

# Never block these UT1 lists: they are allow-lists, not block-lists.
ALLOW_LISTS = {"liste_blanche", "exceptions_liste_bu"}

PRESETS = {
    # recommended: few false positives (checked against the 327k-site seed list)
    "unsafe": {"adult", "mixed_adult", "gambling", "arjel", "malware", "phishing", "cryptojacking",
               "ddos", "stalkerware", "ransomware", "scam", "manual"},
    # wider net - also catches matrimony sites, archive.org (torrent list), news (drugs list) ...
    "broad": {"adult", "mixed_adult", "lingerie", "dating", "gambling", "arjel", "malware", "phishing",
              "cryptojacking", "crypto", "ddos", "hacking", "warez", "piracy", "torrent", "drugs", "drogue",
              "scam", "ransomware", "agressif", "stalkerware", "dangerous_material", "manual"},
    "adult": {"adult", "mixed_adult", "lingerie"},
    "gambling": {"gambling", "arjel"},
    "security": {"malware", "phishing", "cryptojacking", "crypto", "ddos", "hacking", "ransomware", "scam",
                 "stalkerware"},
    # 'dolma_nsfw' (subreddit names matched as the first host label) is never in a preset:
    # it blocks things like apply.<university>.ac.in. Ask for it by name if you want it.
}

_TOKEN = re.compile(r"[a-z0-9]+")


def normalize(url):
    u = str(url).strip().lower()
    for p in ("http://", "https://"):
        if u.startswith(p):
            u = u[len(p):]
            break
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    return u.rstrip("/")


class Blocklist:
    def __init__(self):
        self.domains = {}    # domain -> source:category
        self.urls = {}       # normalized url -> source:category
        self.labels = {}     # first host label -> source:category
        self.keywords = {}   # category -> {"strict": set, "hard": set, "soft": set}
        self.keyword_level = None
        self.allow = set()

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, root=None, categories="unsafe", keyword_level=None):
        """categories: preset name, comma list, or 'all'. keyword_level: None|strict|hard|soft."""
        root = os.path.abspath(root or DEFAULT_ROOT)
        src = os.path.join(root, "sources")
        wanted = cls.expand_categories(categories)
        bl = cls()

        for kind in ("ut1", "other"):  # folder-per-category lists: <cat>/domains and <cat>/urls
            base = os.path.join(src, kind)
            if not os.path.isdir(base):
                continue
            for cat in sorted(os.listdir(base)):
                folder = os.path.join(base, cat)
                if not os.path.isdir(folder):
                    continue
                if cat in ALLOW_LISTS:
                    bl.allow.update(_read_lines(os.path.join(folder, "domains")))
                    continue
                if wanted is not None and cat not in wanted:
                    continue
                for d in _read_lines(os.path.join(folder, "domains")):
                    bl.domains.setdefault(normalize(d), f"{kind}:{cat}")
                for u in _read_lines(os.path.join(folder, "urls")):
                    bl.urls.setdefault(normalize(u), f"{kind}:{cat}")

        bpp = os.path.join(src, "blocklistproject")
        if os.path.isdir(bpp):
            for name in sorted(os.listdir(bpp)):
                cat = os.path.splitext(name)[0]
                if wanted is not None and cat not in wanted:
                    continue
                for d in _read_lines(os.path.join(bpp, name)):
                    bl.domains.setdefault(normalize(d), f"blocklistproject:{cat}")

        manual = os.path.join(src, "manual")
        if os.path.isdir(manual) and (wanted is None or "manual" in wanted):
            for name in sorted(os.listdir(manual)):
                for d in _read_lines(os.path.join(manual, name)):
                    bl.domains.setdefault(normalize(d), f"manual:{os.path.splitext(name)[0]}")

        dolma = os.path.join(src, "dolma", "subreddit_blocklist.txt")
        if os.path.exists(dolma) and (wanted is None or "dolma_nsfw" in wanted):
            for lab in _read_lines(dolma):
                lab = lab.strip().lower()
                if len(lab) >= 4:  # very short labels cause false positives
                    bl.labels.setdefault(lab, "dolma:nsfw_subreddit")

        if keyword_level:
            kw_path = os.path.join(root, "keywords.json")
            with open(kw_path, encoding="utf-8") as f:
                rules = json.load(f)
            for cat, levels in rules.items():
                if wanted is None or cat in wanted:
                    bl.keywords[cat] = {k: set(v) for k, v in levels.items()}
            bl.keyword_level = keyword_level

        bl.allow.update(normalize(d) for d in _read_lines(os.path.join(src, "allow.txt")))
        bl.allow = {normalize(d) for d in bl.allow}
        log.info(f"blocklist loaded: {len(bl.domains)} domains, {len(bl.urls)} urls, "
                 f"{len(bl.labels)} labels, keywords={keyword_level}")
        return bl

    @staticmethod
    def expand_categories(spec):
        if spec in (None, "", "all"):
            return None
        out = set()
        for part in str(spec).split(","):
            part = part.strip()
            if part:
                out |= PRESETS.get(part, {part})
        return out

    # ------------------------------------------------------------ matching
    def match(self, url):
        """Return 'source:category' if url is blocked, else None."""
        n = normalize(url)
        host = n.split("/", 1)[0].split(":", 1)[0]
        parts = host.split(".")
        if self.allow and any(".".join(parts[i:]) in self.allow for i in range(len(parts))):
            return None
        for i in range(len(parts) - 1):  # a.b.example.com -> b.example.com -> example.com
            hit = self.domains.get(".".join(parts[i:]))
            if hit:
                return hit
        if self.urls:
            hit = self.urls.get(n) or self.urls.get(n.split("?", 1)[0])
            if hit:
                return hit
        if self.labels and parts:
            hit = self.labels.get(parts[0])
            if hit:
                return hit
        if self.keyword_level:
            return self._keyword_match(n)
        return None

    def _keyword_match(self, n):
        levels = {"strict": ("strict",), "hard": ("strict", "hard"),
                  "soft": ("strict", "hard", "soft")}[self.keyword_level]
        tokens = set(_TOKEN.findall(n))
        for cat, rules in self.keywords.items():
            if "strict" in levels and any(w in n for w in rules.get("strict", ())):
                return f"keyword:{cat}"
            if "hard" in levels and tokens & rules.get("hard", set()):
                return f"keyword:{cat}"
            if "soft" in levels and any(w in n for w in rules.get("soft", ())):
                return f"keyword:{cat}"
        return None

    def __bool__(self):
        return bool(self.domains or self.urls or self.labels or self.keywords)


def list_categories(root=None):
    root = os.path.abspath(root or DEFAULT_ROOT)
    src = os.path.join(root, "sources")
    rows = []
    for kind in ("ut1", "other"):
        base = os.path.join(src, kind)
        if not os.path.isdir(base):
            continue
        for cat in sorted(os.listdir(base)):
            if not os.path.isdir(os.path.join(base, cat)):
                continue
            d = len(_read_lines(os.path.join(base, cat, "domains")))
            u = len(_read_lines(os.path.join(base, cat, "urls")))
            rows.append((kind, cat, d, u, "ALLOW-LIST" if cat in ALLOW_LISTS else ""))
    bpp = os.path.join(src, "blocklistproject")
    if os.path.isdir(bpp):
        for name in sorted(os.listdir(bpp)):
            rows.append(("blocklistproject", os.path.splitext(name)[0],
                         len(_read_lines(os.path.join(bpp, name))), 0, ""))
    dolma = os.path.join(src, "dolma", "subreddit_blocklist.txt")
    if os.path.exists(dolma):
        rows.append(("dolma", "dolma_nsfw", len(_read_lines(dolma)), 0, "labels"))
    manual = os.path.join(src, "manual")
    if os.path.isdir(manual):
        for name in sorted(os.listdir(manual)):
            rows.append(("manual", "manual", len(_read_lines(os.path.join(manual, name))), 0, name))
    return rows


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def add_blocklist_args(parser):
    g = parser.add_argument_group("Blocklist (off by default)")
    g.add_argument("--blocklist", default="",
                   help="Skip sites/links in these categories: 'unsafe', 'adult', 'gambling', "
                        "'security', 'all', or a comma list (see: main.py blocklist categories).")
    g.add_argument("--blocklist-keywords", default=None, choices=["strict", "hard", "soft"],
                   help="Also block by keyword rules (strict = fewest false positives).")
    g.add_argument("--blocklist-dir", default=None, help="Folder with sources/ and keywords.json.")
    return g


def blocklist_from_args(args):
    if not getattr(args, "blocklist", "") and not getattr(args, "blocklist_keywords", None):
        return None
    return Blocklist.load(args.blocklist_dir, args.blocklist or "unsafe", args.blocklist_keywords)
