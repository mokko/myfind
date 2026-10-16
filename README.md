# myfind

A small collection discovery portal: **Flask + Jinja** in front, **BaseX** as the
record store, **Solr** as the search index. It is a deliberately reduced
re-implementation of the two systems it is modelled on — VuFind (PHP) and
Blacklight (Ruby/Rails) — for one museum collection whose records already live
as XML in BaseX.

Name check: `myfind` is free on PyPI (404) and npm (404). A GitHub *organisation*
called `myfind` already exists, and there are ~250 public repos named `myfind`,
so publish under your own account rather than the bare name.

## What this is, and what it is not

It is a *discovery* portal: one search box over the whole collection, facets to
narrow a result set, a record page per object, and links that walk from an object
to the people who made it and the images of it.

It is not a replacement for BaseX. BaseX stays the system of record — the
authoritative XML, one document per record, and the thing your OAI-PMH provider
already serves from. myfind only *reads* it, plus a derived Solr index for the
parts BaseX is bad at (ranked text search, facet counts over 180k records).

## Status: early scaffolding

Honest state, so a later session does not over-read this repo:

| Piece | State |
|---|---|
| `tools/field_inventory.xq` | run over all three modules; output in `tools/inventory.xml` |
| Field mapping (`myfind.toml`) | settled for Objects / Persons / Assets, from real coverage |
| `myfind/basex.py` | REST client; document-name fetch, link resolution in one query |
| `myfind/ria.py` | RIA values, links, UTC normalization |
| `myfind/solr.py` | Solr client + **schema generated from the mapping** |
| `myfind/indexer.py` | BaseX → Solr, batched, resumable via receipt |
| `myfind/app.py` + templates | **written** — per-entity search, results, record pages, JSON API |
| **Solr** | **installed and running** — 9.11.0 on port 8983, core `myfind`, schema built |
| **Pipeline proven** | **20 real records** indexed and queried end to end |
| Tests | 33 passing (`pytest`), no live services needed |
| Full index (181k records) | **not yet run** — blocked on the two commands below |

### Not yet done, and why

Everything downstream of the index needs two things only you can provide:

1. **The BaseX password** as `<repo>/.env` (see *Requirements*). Without it
   the app cannot read a single record and the indexer cannot read the store.
2. **A writable directory on the NVMe**, so the index does not land on the eMMC.

Until the index is built, the search pages render an empty result set (correctly —
they say so) and **record pages already work standalone**, because they read
BaseX rather than Solr.

## Solr

Installed at `<SOLR_HOME>`, running on **8983** (BaseX has 8080 and
1984). Started with:

```bash
cd <SOLR_HOME>
export SOLR_JAVA_MEM="-Xms1g -Xmx2g"
./bin/solr start -p 8983
./bin/solr create -c myfind -n myfind        # real core, once the schema exists
```

Two notes worth keeping:

- **Put the heap in `bin/solr.in.sh`, not just the environment.** The 2 GB heap
  above came from an exported variable, so a plain restart silently drops back to
  the 512 MB default. Measured RSS at 2 GB max heap: ~1.3 GB.
- **The index should live on the NVMe.** `/mnt/nvme` has 862 GB free; the eMMC
  root has 31 GB. Needs one `sudo` line to create a writable directory — see
  *The box it runs on*.

## The box it runs on

Measured, not assumed:

- **Radxa ROCK 5B+**, kernel `6.1.115-vendor-rk35xx`, `aarch64`
- 8 cores: 4× Cortex-A76 (up to 1800 MHz) + 4× Cortex-A55
- **16 GB RAM**; swap is 7.8 GB **zram** (compressed RAM) + an 8 GB NVMe swapfile
- **Two disks, and the fast one is nearly empty:**
  - root on **eMMC** `/dev/mmcblk0p1` — 113 GB, **31 GB free**, 185 MB/s
  - **1 TB Samsung 970 EVO Plus NVMe** at `/mnt/nvme` — **862 GB free, 1% used**
- OpenJDK **21.0.12** (runtime; no JDK/`javac`)
- Python **3.14.7**, `uv` 0.12.19, Docker 29.9.0, git 2.43.0
- BaseX **12.4** at `~/basex/basex`, HTTP server running since Oct 5 — REST on
  **8080**, client/server on **1984**, `TIMEOUT = 3600` in `.basex`

Solr is pure Java, so **aarch64 is not an obstacle** — there is no native
component to build. RAM and index I/O are the real budget, and the NVMe removes
the I/O half of it. `/mnt/nvme` is root-owned and not writable by the user; one
`sudo` line fixes that:

```bash
sudo mkdir -p /mnt/nvme/myfind && sudo chown "$USER:$USER" /mnt/nvme/myfind
```

Known soft limits: `ulimit -n` is 4096 (Solr suggests 65000). A warning, not a
blocker at this size.

## Requirements

- BaseX 12 with the module databases loaded (already the case)
- Java 21 for Solr (present)
- Python ≥ 3.11 with `flask` and `httpx`

## Quick start

```bash
cd <repo>

# 1. Dependencies
uv venv && uv pip install -e ".[dev]"

# 2. Credentials. Copy the template and fill it in (`.env` is gitignored).
cp .env.example .env && chmod 600 .env
#    If the value already exists somewhere on this machine, copy it across
#    without ever printing it:
#    python3 tools/link_env.py --source <existing>.env

# 3. Build the index — start small, then the real thing (resumable)
.venv/bin/python tools/index.py --limit 500
.venv/bin/python tools/index.py

# 4. Run it
.venv/bin/python -m myfind.app                 # http://127.0.0.1:5000
```

BaseX must be up (REST on 8080 by default):

```bash
curl -s -u <user>:<pw> "http://localhost:8080/rest?query=1%20to%203"   # 1 2 3
```

Credentials are **not** in this repo. The `.env` beside the config holds
`MYFIND_BASEX_PASSWORD` and is read at startup; the tracked TOML carries a dev
placeholder only so a fresh checkout runs.

## What it serves

Three entities, each with a search page, a results list with facets, and a record
page:

| Route | What |
|---|---|
| `/` | landing page, record counts per entity |
| `/search?q=` | one box across all three entities |
| `/Object/?q=&sort=` | search Objects (facets: number, type, maker, location, collection group, unit) |
| `/Object/447469` | one Object: all display fields, linked makers and assets |
| `/Person/?q=` | search Persons (facets: type, unit) |
| `/Person/218316` | one Person, with the objects made by them |
| `/Multimedia/?q=` | search Assets (facets: type, unit) |
| `/Multimedia/268711` | one Asset, with its owning object |
| `/api/Object?q=&limit=` | JSON results for one entity |
| `/healthz` | are BaseX and Solr answering |

**Linked-record pages are the point.** An Object links to its makers and its
assets; a Person links back to the objects they made; an Asset links to its
object. Links resolve by document name (`Person-218316.xml`), and a link whose
target module was never ingested renders its label and is marked
"not in this index" — it never 404s. That matters because **14 referenced modules
are absent, including the owner on every single object.**

## Test

```bash
.venv/bin/python -m pytest -q          # 33 tests, no live services needed
```

## The data it serves

Three MuseumPlus RIA modules, one document per record:

| Database | Documents | Size | Record file |
|---|---|---|---|
| `sync_Object` | ~47,853 | 2,190 MB | `Object-<id>.xml` |
| `sync_Multimedia` | ~127,551 | 2,660 MB | `Multimedia-<id>.xml` |
| `sync_Person` | ~5,799 | 359 MB | `Person-<id>.xml` |

~181k records, ~5.2 GB of native XML. The full module/link structure is in
`AGENTS.md`.

## Roadmap

**Phase 1 — Flask + Jinja + direct BaseX (no Solr).**
Record pages, linked-record pages, field display. These are all O(1) document
lookups, so they are fast *without* any index. This is the part BaseX is good at
and it validates the field mapping against real records.

**Phase 2 — Solr.**
Search, facets, ranking, highlighting. Facet counts mean reading every value of
a field across 180k records; BaseX needs a full pass for that (~40 s measured),
which is why a real index exists. A BaseX → Solr indexer maps each record to a
Solr document; Solr becomes the only thing that answers a query, and every hit
is then fetched from BaseX by document name.

**Not planned:** changing the BaseX store. In particular the store currently has
**no indexes at all** (text, attribute, token, full-text all disabled) and that
is fine for this architecture — see `AGENTS.md` → *No indexes, and why*.

## Open questions

1. **Should BaseX get an attribute/text index?** Recommendation: **no**, on the
   current design — Solr absorbs search and BaseX is only ever hit by document
   name. The one thing an index would buy is *reverse* traversal inside BaseX
   ("which objects link to person X"), and Solr answers that better. Revisit only
   if a future feature wants cross-record queries answered from BaseX.
2. **Document-count discrepancy.** `db:list-details()` reports 47,855 / 5,799 /
   127,677 resources while `count(collection(...))` returns 47,853 / 5,799 /
   127,551. Small, unexplained — settle it before trusting counts for the indexer.
3. **14 referenced modules are not in the store** (Address, Ownership,
   Literature, ObjectGroup, Movement, Exhibition, InventoryNumber, DefDimension,
   CollectionActivity, Registrar, Task, Accessory, MultimediaGroup, Conservation).
   Linked-record pages must render "not in this index" rather than 404.
4. **Solr version.** 9.11.0 is downloaded; 10.0.0 exists. 9.11 is the safer
   pick against Java 21 and a much larger body of documentation.

## License

MIT — see `LICENSE`.

## Where to read more

- `AGENTS.md` — internals: code map, the link map, measured costs, the mapping,
  and the indexing plan.
- `the sibling OAI-PMH provider project` — the sibling project; same store, same REST client shape,
  and the place the BaseX conventions in this repo came from.
