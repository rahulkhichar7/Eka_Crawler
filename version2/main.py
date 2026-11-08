"""Eka Crawler v2 - one entry point for every pipeline.

    python main.py seeds      build | from-warc | check-robots
    python main.py crawl      [options]                      (sites -> WARC + PDF links)
    python main.py pdf-urls   from-warc | shodhganga | ndl-iitkgp | sansad | archive-org | merge
    python main.py download   pdfs | archive-org
    python main.py text       warc | commoncrawl
    python main.py dataset    combine
    python main.py blocklist  categories | check | filter

Add --help after any command for its options, e.g.  python main.py crawl --help
"""
import argparse
import multiprocessing as mp
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def build_parser():
    from eka.blocklist import cli as blocklist_cli
    from eka.crawl import cli as crawl_cli
    from eka.dataset import cli as dataset_cli
    from eka.download import cli as download_cli
    from eka.pdf_urls import cli as pdf_urls_cli
    from eka.seeds import cli as seeds_cli
    from eka.text import cli as text_cli

    parser = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="pipeline", required=True, metavar="PIPELINE")
    for mod in (seeds_cli, crawl_cli, pdf_urls_cli, download_cli, text_cli, dataset_cli, blocklist_cli):
        mod.register(sub)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
