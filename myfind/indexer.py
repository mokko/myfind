"""BaseX -> Solr indexing.

Three decisions worth knowing, all of them learned the expensive way:

1. **Enumerate names, then fetch in batches.** A whole collection is never
   requested in one round trip: `sync_Object` alone serialises to 2.19 GB and the
   REST server would hold every byte of it in memory before answering. Names come
   from `db:list()`, documents come in batches of `index.batch`.

2. **The receipt is fail-safe, because a receipt that lies skips chunks into a
   hole.** It names the module and the mapping hash it describes, is ignored for
   any other mapping, and is written atomically after each batch. A batch that
   was never indexed must never be recorded as done.

3. **Re-ingesting a batch is idempotent** -- documents are keyed by
   `Module-id`, so Solr replaces rather than duplicates. That is what makes a
   resumed run safe to just re-run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from . import ria
from .basex import BaseXClient
from .config import Config, Module
from .solr import SolrClient

# Fetch a batch of documents by name. `subsequence` on db:list keeps the order
# stable across runs, which is what makes a batch offset meaningful.
BATCH_QUERY = """\
declare variable $db external;
declare variable $start as xs:integer external;
declare variable $count as xs:integer external;
for $n in subsequence(db:list($db), $start, $count)
return doc($db || '/' || $n)
"""


def mapping_hash(config: Config) -> str:
    """A hash of everything that changes the produced Solr documents.

    If the mapping changes, a receipt from the old mapping must not be trusted:
    it would skip exactly the batches whose content needs rebuilding.
    """
    payload = []
    for m in config.modules:
        payload.append(
            {
                "name": m.name,
                "database": m.database,
                # Only the roles that change what is WRITTEN to Solr. `display`
                # affects the template, not the document, so adding it to a field
                # must not invalidate the index and force a full rebuild -- that
                # is a false invalidation, and it teaches people to ignore the
                # receipt.
                "fields": [
                    [f.field, f.kind, f.name, sorted(r for r in f.roles if r != "display")]
                    for f in m.fields
                ],
                "links": [[l.field, l.reference, l.target] for l in m.links],
            }
        )
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Receipt:
    path: Path
    data: dict

    @classmethod
    def load(cls, path: Path) -> "Receipt":
        try:
            return cls(path=path, data=json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # An unreadable receipt means "nothing has been done", which is the
            # only safe reading: the alternative is assuming work that may not
            # have happened.
            return cls(path=path, data={})

    def batches_done(self, module: str, mhash: str) -> int:
        entry = self.data.get("modules", {}).get(module)
        if not entry or entry.get("mapping_hash") != mhash:
            return 0
        return int(entry.get("batches_done", 0))

    def record(
        self, module: str, mhash: str, batches_done: int, docs_indexed: int, total: int
    ) -> None:
        self.data.setdefault("modules", {})[module] = {
            "mapping_hash": mhash,
            "batches_done": batches_done,
            "docs_indexed": docs_indexed,
            "source_documents": total,
        }
        # Atomic: written beside itself and moved into place, because a
        # half-written receipt claims a batch that was never ingested.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def map_record(record: ET.Element, module: Module, offset: str) -> dict | None:
    """One stored RIA record -> one Solr document, or None if it is empty."""
    item = ria.item_of(record)
    if item is None:
        return None
    rid = ria.record_id(record)
    if not rid:
        return None

    doc: dict[str, object] = {
        "id": f"{module.name}-{rid}",
        "module": module.name,
        "record_id": rid,
    }
    searchable: list[str] = []

    for spec in module.fields:
        values = ria.extract_values(item, spec)
        if not values:
            continue
        if spec.is_sort_key():
            # Single-valued in Solr, so write a scalar rather than a list: a
            # one-element array happens to work, and stops working the moment a
            # record carries two values.
            doc[spec.field] = values[0]
        else:
            doc[spec.field] = values
        if spec.has("facet"):
            doc[f"{spec.field}_facet"] = values
        if spec.has("search"):
            searchable.extend(values)
        if spec.field == "last_modified":
            normalized = ria.normalize_timestamp(values[0], offset)
            if normalized:
                doc["last_modified"] = normalized

    for link in module.links:
        targets = ria.extract_links(item, link)
        if targets:
            doc[link.field] = [t["id"] for t in targets]

    # The catch-all box searches one deduplicated blob rather than a dismax over
    # every field: simpler to reason about, and the per-field search is still
    # available for fielded queries.
    if searchable:
        seen: dict[str, None] = {}
        for value in searchable:
            seen.setdefault(value, None)
        doc["text"] = list(seen)

    return doc


def document_names(client: BaseXClient, database: str) -> list[str]:
    return [
        n.strip()
        for n in client.query(f"db:list('{database}')").split("\n")
        if n.strip()
    ]


def index_module(
    config: Config,
    module: Module,
    bx: BaseXClient,
    solr: SolrClient,
    limit: int | None = None,
    resume: bool = True,
    verbose: bool = True,
) -> dict:
    """Index one module. Returns a summary dict."""
    mhash = mapping_hash(config)
    receipt = Receipt.load(
        Path(config.index.receipt)
        if Path(config.index.receipt).is_absolute()
        else (config.path.parent / config.index.receipt)
    )

    names = document_names(bx, module.database)
    total = len(names)
    if limit:
        total = min(total, limit)
    batch_size = max(1, config.index.batch)

    start_batch = receipt.batches_done(module.name, mhash) if resume else 0
    if verbose:
        print(
            f"[{module.name}] {total} documents, batch={batch_size}, "
            f"starting at batch {start_batch}"
        )

    docs_indexed = 0
    batches_done = start_batch
    n_batches = (total + batch_size - 1) // batch_size

    for b in range(start_batch, n_batches):
        offset = b * batch_size
        count = min(batch_size, total - offset)
        if count <= 0:
            break
        xml = bx.query(BATCH_QUERY, db=module.database, start=offset + 1, count=count)
        if not xml.strip():
            break
        try:
            wrapper = ET.fromstring(f"<batch>{xml}</batch>")
        except ET.ParseError as exc:
            raise RuntimeError(f"batch {b} was not XML: {exc}") from exc

        docs = [
            d
            for d in (map_record(rec, module, config.basex.timezone_offset) for rec in wrapper)
            if d
        ]
        if docs:
            solr.index(docs)
            docs_indexed += len(docs)

        batches_done = b + 1
        receipt.record(module.name, mhash, batches_done, docs_indexed, total)
        if verbose and (b % 10 == 0 or b + 1 == n_batches):
            print(
                f"[{module.name}] batch {batches_done}/{n_batches} "
                f"({docs_indexed} docs indexed)",
                flush=True,
            )

    solr.commit()
    return {
        "module": module.name,
        "source_documents": total,
        "documents_indexed": docs_indexed,
        "batches": batches_done,
        "mapping_hash": mhash,
    }
