#!/usr/bin/env python3
"""Index the BaseX store into Solr.

    python3 tools/index.py                    # all modules, resumable
    python3 tools/index.py --limit 500        # a cheap end-to-end check
    python3 tools/index.py --reset            # clear the Solr index first
    python3 tools/index.py --module Object    # just one module
    python3 tools/index.py --schema-only      # create the Solr fields and stop

Safe to re-run: documents are keyed by `Module-id`, so a resumed run replaces
rather than duplicates, and the receipt decides what may be skipped.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myfind import config as config_mod  # noqa: E402
from myfind.basex import BaseXClient  # noqa: E402
from myfind.indexer import index_module, mapping_hash  # noqa: E402
from myfind.solr import SolrClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="myfind.toml")
    ap.add_argument("--limit", type=int, default=None, help="documents per module")
    ap.add_argument("--module", action="append", default=None)
    ap.add_argument("--reset", action="store_true", help="delete all indexed docs first")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--schema-only", action="store_true")
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    print(f"config: {cfg.path}")
    print(f"basex : {cfg.basex.url} (user {cfg.basex.user})")
    print(f"solr  : {cfg.solr.url}")

    with SolrClient(cfg.solr.url) as solr:
        if not solr.alive():
            print(f"ERROR: Solr is not answering at {cfg.solr.url}", file=sys.stderr)
            return 2

        schema = solr.ensure_schema(cfg)
        print(f"schema: {schema['created']} fields created, {schema['existing']} already present")

        if args.schema_only:
            return 0

        if args.reset:
            before = solr.count()
            solr.delete_all()
            print(f"reset : removed {before} documents")
            # A reset invalidates the receipt: the batches it describes are gone.
            receipt = Path(cfg.index.receipt)
            if not receipt.is_absolute():
                receipt = cfg.path.parent / receipt
            if receipt.exists():
                receipt.unlink()
                print(f"reset : removed receipt {receipt}")

        with BaseXClient(
            url=cfg.basex.url,
            user=cfg.basex.user,
            password=cfg.basex.password,
            timeout=cfg.basex.timeout,
        ) as bx:
            if not bx.ping():
                print(f"ERROR: BaseX is not answering at {cfg.basex.url}", file=sys.stderr)
                return 2

            wanted = args.module or cfg.module_names()
            print(f"mapping hash: {mapping_hash(cfg)}")
            started = time.time()
            summaries = []
            for name in wanted:
                module = cfg.module(name)
                t0 = time.time()
                summary = index_module(
                    cfg,
                    module,
                    bx,
                    solr,
                    limit=args.limit,
                    resume=not args.no_resume,
                )
                summary["seconds"] = round(time.time() - t0, 1)
                summaries.append(summary)
                print(
                    f"[{name}] done in {summary['seconds']}s: "
                    f"{summary['documents_indexed']} indexed "
                    f"of {summary['source_documents']} source documents",
                    flush=True,
                )

            total = solr.count()
            elapsed = round(time.time() - started, 1)
            print(f"\nindex now holds {total} documents ({elapsed}s total)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
