# AGENTS.md — myfind internals

Read this before changing anything. It records what was *measured* on this box
against this corpus, because most of the design here is a consequence of a
number rather than a preference.

## Code map

```
myfind.toml                the mapping: fields, roles, links -- DATA, not code
myfind/
  config.py                TOML + .env + environment overrides -> dataclasses
  basex.py                 sync BaseX REST client; document-name fetch
  ria.py                   reading values/links out of a stored moduleItem
  solr.py                  Solr client; schema GENERATED from myfind.toml
  indexer.py               BaseX -> Solr, batched, resumable via receipt
  app.py                   Flask routes
templates/                 base, home, search, search_all, record
static/style.css
tools/
  index.py                 the indexing CLI
  field_inventory.xq       per-field coverage over the real store
  inventory.xml            its output, for the mapping decisions
  smoke_solr.py            the 20-record end-to-end proof
tests/                     33 tests; none need a live service
```

The request path, end to end:

```
browser -> Flask -> Solr (search, facets, paging)          <- only this touches 181k records
                  -> BaseX (record page, linked records)   <- O(1) document-name lookups
```

`basex.py` is deliberately synchronous:
Flask is WSGI, so requests are already served one-per-thread and an async client
would only add an event loop to babysit. The async twin in `the sibling OAI-PMH provider project`
exists for a Starlette app and is not a better shape here.

## The store (BaseX)

### One document per record — so fetch by name, never by scan

Each record is its **own document**, named `<Module>-<id>.xml`:

```
sync_Object/Object-447469.xml        <- one moduleItem
sync_Person/Person-218316.xml
sync_Multimedia/Multimedia-268711.xml
```

`db:list('sync_Object')` returns exactly those names. So a record page is
`doc('sync_Object/Object-447469.xml')` — an O(1) name lookup. Searching for the
record instead (`collection('sync_Object')//moduleItem[@id='447469']`) is a full
scan of 47,855 documents. Both measured, below.

**This is the single most important fact for performance in this project.** Every
record page and every linked-record page uses the document name. Only the indexer
reads whole collections.

### No indexes, and why that is fine

`db:info('sync_Object')` reports all four index kinds disabled:

```
textindex=false  attrindex=false  tokenindex=false  ftindex=false
```

No `CREATE INDEX`/`CREATE TEXTINDEX`/`CREATE INDEX fulltext` has ever been run on
these databases. That is acceptable **because of the architecture**: BaseX is
never asked a question that needs an index. It is asked for a document by name
(O(1) already), and everything else — ranked search, facet counts, "which objects
reference this person" — is answered by Solr from a derived index.

Adding a BaseX index would buy exactly one thing: *reverse* traversal inside
BaseX (person → objects), which currently needs a full pass. Solr answers that
better and is already being built. So: **not needed, and not requested.**

The costs of *not* having one show up only if search is left in BaseX, which is
why Phase 2 exists.

### Measured costs

All with the local `bin/basex` client (includes ~1 s JVM startup), warm database,
`sync_Object` alone = 47,853 documents / 2,190 MB / 114,738,761 nodes:

| Operation | Time | Notes |
|---|---|---|
| substring scan, no index | **13.8 s** | `value[contains(lower-case(string(.)), "landschaft")]` → 1,630 hits |
| full-text scan, no index | **11.9 s** | `value[. contains text "Landschaft"]` → 851 hits |
| record fetch by **scanning** for `@id` | **10.3 s** | `collection(...)[.//moduleItem/@id="447469"]` |
| record fetch by **document name** | **1.5 s** | `doc('sync_Object/Object-447469.xml')` |
| link-map aggregation, all 3 modules | **39.9 s** | one pass, `group by` over every `moduleReference` |

Two lessons are baked into the code because of this:

- **Two scan-sized operations are 7–9× the cost of a name lookup.** A result list
  that fetches N records by scanning is N×10.3 s. Fetch by name, or don't fetch.
- **An aggregation over a whole module is inherently a full pass** — 40 s for the
  link map, and facets are the same shape of work. Facets belong in Solr.

A first attempt at the link map nested `collection($d)//moduleReference[@name=$n]`
inside a `for $n in distinct-values(...)`, which is O(records × names) and did not
finish in 30 s. The fix is a single FLWOR with `group by`, which is what
`tools/field_inventory.xq` now does (and what its predecessor got wrong).

## The link map

Cross-module links are `moduleReference[@name=...]/moduleReferenceItem[@moduleItemId=...]`,
and `@targetModule` names the module the id belongs to. **So a link resolves to a
document name**: `moduleItemId="218316"` in an `ObjPerAssociationRef` →
`sync_Person/Person-218316.xml`.

Measured over the whole store (occurrence counts):

| From | Reference | Target | Count | Target present? |
|---|---|---|---|---|
| Object | `ObjPerAssociationRef` | Person | 47,853 | ✅ |
| Object | `ObjMultimediaRef` | Multimedia | 43,983 | ✅ |
| Object | `ObjAcquisitionSourcePerRef` | Person | 19,353 | ✅ |
| Object | `ObjObjectARef` | Object | 10,889 | ✅ |
| Object | `ObjObjectBRef` | Object | 9,577 | ✅ |
| Object | `ObjOwnerRef` | Address | 47,853 | ❌ |
| Object | `TypeDimRef` | DefDimension | 58,782 | ❌ |
| Object | `InvNumberSchemeRef` | InventoryNumber | 44,604 | ❌ |
| Object | `MovementRef` | Movement | 40,370 | ❌ |
| Object | `ObjObjectGroupsRef` | ObjectGroup | 31,071 | ❌ |
| Object | `ObjOwnership001Ref` | Ownership | 29,829 | ❌ |
| Object | `ObjLiteratureRef` | Literature | 21,427 | ❌ |
| Object | `ObjOwnershipRef` | Ownership | 13,623 | ❌ |
| Object | `ObjCollectionActivityRef` | CollectionActivity | 9,001 | ❌ |
| Object | `ObjRegistrarRef` | Registrar | 8,489 | ❌ |
| Object | `ObjConservationRef` | Conservation | 4,738 | ❌ |
| Object | `ExhibitionRef` | Exhibition | 611 | ❌ |
| Object | `ObjMovementRef` | Movement | 390 | ❌ |
| Object | `AddressRef` | Address | 395 | ❌ |
| Object | `LiteratureRef` | Literature | 81 | ❌ |
| Object | `ObjAccessoryRef` | Accessory | 26 | ❌ |
| Object | `ObjTaskRef` | Task | 16 | ❌ |
| Object | `ObjLoanRef` | Address | 3 | ❌ |
| Person | `PerObjectRef` | Object | 5,453 | ✅ |
| Person | `PerObjectgroupRef` | ObjectGroup | 4,019 | ❌ |
| Person | `PerLiteratureRef` | Literature | 2,996 | ❌ |
| Person | `PerVenderRef` | Object | 902 | ✅ |
| Person | `PerOwnershipMNRef` | Ownership | 756 | ❌ |
| Person | `PerPersonBRef` | Person | 374 | ✅ |
| Person | `PerPersonARef` | Person | 310 | ✅ |
| Person | `AddressRef` | Address | 78 | ❌ |
| Person | `PerPhotographerRef` | Multimedia | 55 | ✅ |
| Person | `PerMultimediaRef` | Multimedia | 10 | ✅ |
| Person | `HolderRef` | Address | 5 | ❌ |
| Person | `PerTaskRef` | Task | 1 | ❌ |
| Multimedia | `MulObjectRef` | Object | 127,551 | ✅ |
| Multimedia | `MulPhotographerPerRef` | Person | 97,309 | ✅ |
| Multimedia | `MulMultimediaGroup001Ref` | MultimediaGroup | 27,579 | ❌ |
| Multimedia | `MulConservationRef` | Conservation | 744 | ❌ |
| Multimedia | `MulMultimediaGroupRef` | MultimediaGroup | 4 | ❌ |
| Multimedia | `MulLiteratureRef` | Literature | 3 | ❌ |
| Multimedia | `MulAccessoryRef` | Accessory | 1 | ❌ |
| Multimedia | `MulExhibitionRef` | Exhibition | 1 | ❌ |

### 14 referenced modules are absent

Only `Object`, `Person` and `Multimedia` were ever ingested. Every link to
Address, Ownership, Literature, ObjectGroup, Movement, Exhibition,
InventoryNumber, DefDimension, CollectionActivity, Registrar, Task, Accessory,
MultimediaGroup and Conservation points at a document that does not exist — and
one of them, `ObjOwnerRef` → Address, is on **every single object** (47,853), and
carries the holding institution.

**So a link resolver must not 404 on those.** It renders the label the record
already carries (the `formattedValue` inside the `moduleReferenceItem` is
present even when the target is not — "Kupferstichkabinett, Staatliche Museen zu
Berlin" comes from there) and marks the target as not in this index. Treating a
dangling link as an error would break the owner line on every record page.

Note also the two directions disagree: `ObjMultimediaRef` on the Object side
counts 43,983 while `MulObjectRef` on the Multimedia side counts 127,551. The
asset → object direction is populated for every asset; the object → asset
direction only on some. Do not assume a link is symmetric when building a
"related records" panel — derive each direction from its own module.

## Field mapping

Verified against the real records and against the coverage counts recorded in
`<oai-provider>/oai.toml` (which were counted over a 1,000-object sample, not
assumed).

### Object

| Role | Element |
|---|---|
| title | `virtualField[@name='ObjObjectTitleVrt']/value` — 1000/1000 |
| object number | `dataField[@name='ObjObjectNumberTxt']/value` — 1000/1000 |
| object number (sort) | `dataField[@name='ObjObjectNumberSortedTxt']/value` |
| type | `dataField[@name='ObjTechnicalTermClb']/value` — 999/1000 |
| date (display) | `virtualField[@name='ObjDateVrt']/value` — 863/1000, free text ("um 1715/16") |
| material/technique | `virtualField[@name='ObjMaterialTechniqueVrt']/value` — 998/1000 |
| makers | `moduleReference[@name='ObjPerAssociationRef']` → Person, with `RoleVoc` / `AttributionVoc` |
| holder | `moduleReference[@name='ObjOwnerRef']` → Address (absent) |
| location | `vocabularyReference[@name='ObjNormalLocationVoc']` |
| collection group | `vocabularyReference[@name='ObjOrgGroupVoc']` |
| images | `moduleReference[@name='ObjMultimediaRef']` → Multimedia |
| last modified | `systemField[@name='__lastModified']/value` |

The `ObjDateVrt` value is **free text, not a date** ("um 1715/16"). Anything that
sorts or ranges by date needs a real parsed date alongside it; do not feed the
display string to a range facet.

Names self-decode as `<Module><Meaning><TypeSuffix>` — `Txt` text, `Clb` a CLOB,
`Vrt` a virtual/computed field, `Lnu` a sort number, `Grp` a group. See
`<oai-provider>/ZETCOM-FIELDS.md`.

### `present` is not `nonempty` — check before building a display line

The inventory counts an element as *present*. For `virtualField` and
`systemField` that is not the same as carrying a value: the export emits the
element for every record and leaves `<value/>` empty when there is nothing.

Measured on `sync_Object` (47,853 objects):

| Field | present | non-empty | empty |
|---|---|---|---|
| `ObjObjectTitleVrt` | 47,853 | 47,242 | **611** |
| `ObjDateVrt` | 47,853 | 39,882 | **7,971** |
| `ObjMaterialTechniqueVrt` | 47,853 | 47,441 | 412 |
| `ObjObjectNumberVrt` | 47,853 | 47,836 | 17 |
| `ObjTechnicalTermClb` (dataField) | 47,783 | 47,783 | 0 |

`dataField` carries no empty element, so for it present == non-empty. For
`virtualField` they diverge, sometimes a lot: the date is genuinely absent on
17% of objects and the title on 611.

Consequences: `normalize-space(.) ne ''` is the test, not element presence; a
result list must have a fallback for an object with no title (object number is
the honest fallback); and "facets are empty because the data is empty" must be
distinguished from "the mapping is wrong" by counting non-empty, not present.

### Person

| Role | Element | Coverage |
|---|---|---|
| name | `dataField[@name='PerNameTxt']` | 5,799 / 5,799 |
| authorised form ("Nennform") | `dataField[@name='PerNennformTxt']` | 5,798 |
| type | `vocabularyReference[@name='PerTypeVoc']` | 5,797 |
| objects by this person | `moduleReference[@name='PerObjectRef']` → Object | 5,453 (94%) |
| as seller/vendor | `moduleReference[@name='PerVenderRef']` → Object | 902 |
| multimedia | `moduleReference[@name='PerPhotographerRef']` → Multimedia | 55 |
| sources / biography note | `dataField[@name='PerSourceClb']` | — |
| last modified | `systemField[@name='__lastModified']` | 5,799 |

### Multimedia

| Role | Element | Coverage |
|---|---|---|
| owning object | `moduleReference[@name='MulObjectRef']` → Object | 127,551 / 127,551 |
| file (original) | `dataField[@name='MulOriginalFileTxt']` | 122,831 (96.3%) |
| file size | `dataField[@name='MulSizeTxt']` | 122,464 (96.0%) |
| type | `vocabularyReference[@name='MulTypeVoc']` | 127,280 (99.8%) |
| has thumbnail | `dataField[@name='ThumbnailBoo']` | 124,910 (97.9%) |
| photographer | `moduleReference[@name='MulPhotographerPerRef']` → Person | 97,309 |
| last modified | `systemField[@name='__lastModified']` | 127,551 |

Every asset links to its object, and 96% name an original file — so the asset
side is the reliable one for building "images of this object".

## Decisions in the app worth not re-litigating

- **The schema is generated from `myfind.toml`** (`SolrClient.ensure_schema`). A
  field is declared once, in the mapping, and its Solr fields follow. The
  alternative -- a hand-maintained `managed-schema` beside the config -- drifts,
  and the symptom is a facet that is configured in the app and returns nothing
  because its Solr field was never created.
- **Every facetable field exists twice:** `<field>` as `text_general` (search and
  display) and `<field>_facet` as `string` (exact facet terms). A facet must be
  `string`: analysed text splits `KdZ 5653` into two meaningless facet values.
- **Facet counts exclude the facet's own filter**, via Solr's tag/exclusion pair:
  `fq={!tag=object_type}object_type_facet:"Zeichnung"` together with
  `facet.field={!ex=object_type}object_type_facet`. Without it, selecting one
  value collapses that facet to a single term and a second value can never be
  chosen -- a one-way door, and no error is raised anywhere.
- **An empty field is omitted, not written empty.** `extract_values` returns `[]`
  for a field the record leaves blank, and the indexer skips it, so an empty
  element cannot become an empty facet term or a blank display line.
- **`q` and `sort` survive a facet click.** Losing the query when you narrow is
  what makes a portal feel broken.
- **Dangling links render, they do not 404.** `existing_documents()` resolves every
  target of a record in **one** query (`doc-available()` over a name list) rather
  than one round trip per link.
- **Only mapping-declared sort keys are accepted** from the query string, so a
  sort cannot be injected.
- **A field whose only role is `sort` is created as single-valued `string` with
  docValues.** Solr **cannot sort a multi-valued field at all**, and a
  `text_general` one is analysed and tokenised, so `KdZ 005653` would sort on its
  parts. Getting this wrong is a 400 on every sorted request, and it is invisible
  until someone clicks the sort control.
- **Changing a field's type requires recreating the core.** Solr refuses to write
  a document when the field's index options differ from the ones already in the
  index: `cannot change field "x" from index options=DOCS_AND_FREQS_AND_POSITIONS
  to inconsistent index options=DOCS`. Deleting the field from the schema is not
  enough — the options live in the index segments. Delete the core, recreate it,
  reindex. At 11.5 minutes for the full corpus that is the cheap answer.
- **A Solr field shared by two modules is declared once.** `title`, `org_unit`
  and `object_ids` are each mapped in more than one module, so a naive
  "one add-field per module field" posts the same name repeatedly. Identical
  definitions collapse harmlessly; *conflicting* ones **raise**, because
  otherwise whichever module happens to be iterated first silently decides the
  type -- and a facet is quietly missing, or a sort 400s, with nothing to point
  at. Same hazard as declaring one field from two code paths in one module.
- **A backend error renders as 503, never a bare 500.** A 500 tells the user the
  application is broken; 503 with the backend's own message tells them search is
  down and the record data is fine. A portal that returns an empty result set on
  a backend failure is the worst option: it looks like "no matches".

## Indexing plan (BaseX → Solr)

- **Enumerate first, then chunk.** Do **not** pull a whole collection in one REST
  response: `sync_Object` alone serialises to 2.19 GB and the server would hold
  it in memory. Take names from `db:list()`, then fetch N documents per query
  (batches of a few hundred) with their names bound as external variables.
  Resumable and memory-bounded, and it needs no BaseX index.
- **Write a receipt** after each batch (document names + a hash of the mapping
  config) so an interrupted index run resumes instead of restarting. The sibling
  project learned this the hard way — see `<oai-provider>/AGENTS.md`.
- **The mapping lives in `myfind.toml`, not in code.** A field is a path plus the
  roles it plays (search / facet / display / sort), so changing what is faceted
  is a config edit and a reindex, not a code change.
- **One Solr core, three record types**, discriminated by a `type` field, because
  a discovery portal searches across all three at once and facets over them
  together.
- **Solr holds no payload it cannot rebuild.** The index is derived; the record
  page renders from BaseX. That keeps the XML authoritative and means a schema
  change is a reindex, never a migration.

### External variables must be declared in the prolog

A query that binds variables fails `[XPST0008] Undeclared variable: $db` unless
the variable is declared. Bindings alone do not declare anything:

```xquery
declare variable $db external;
declare variable $limit as xs:integer external;   -- typed: makes `1 to $limit` work
for $n in db:list($db)[1 to $limit] return doc($db || '/' || $n)
```

Declaring the type matters: an untyped external variable arrives as a **string**,
so `1 to $limit` on `"20"` fails `[XPTY0004]` with a message that never mentions
the binding. `tools/smoke_solr.py` emits the `xs:integer` declaration
automatically when the Python value is an `int`, and `myfind/basex.py` should do
the same before it grows a second caller.

## Environment

- Storage is **not** one disk: root is a 113 GB eMMC (`/dev/mmcblk0p1`, 31 GB
  free, 185 MB/s) but there is a **1 TB Samsung 970 EVO Plus NVMe** at
  `/mnt/nvme` with **862 GB free and 1% used**. Index I/O belongs there, not on
  the eMMC. `/mnt/nvme` is currently `root:root` and not writable by the user —
  creating a working directory there is a one-line `sudo` handover, not
  something to work around.
- Swap is 8 GB **file on the NVMe** plus 7.8 GB **zram** (compressed RAM, not
  disk). Budget RAM accordingly: zram swap competes with the working set.
- Solr's `open file limit` warning is real: `ulimit -n` is **4096** against
  Solr's recommendation of 65000. Raising it needs a login-shell setting; it is
  a warning, not a blocker at this corpus size.
- BaseX 12.4, HTTP server `pid` on REST **8080**, client/server **1984**,
  `TIMEOUT = 3600` already raised in `<BASEX_HOME>/.basex` (the 30 s default
  would kill a long ingest — see the sibling project).
- `<BASEX_HOME>/.basex` sets `DBPATH = <BASEX_HOME>/data`.
- Credentials come from a `.env` beside the config (`<PREFIX>_BASEX_PASSWORD`),
  never from a tracked file. The tracked TOML carries a dev placeholder only so a
  fresh checkout runs.
- The REST endpoint is a full read/write XQuery interface. Keep it bound to
  localhost and put only the Flask app on a reachable port.

## Testing conventions

Follow `the sibling OAI-PMH provider project`:

- Tests needing a live BaseX are marked `integration` and **skip themselves**
  when it is down, so a bare `pytest` runs anywhere and `pytest -m "not integration"`
  is the fast loop.
- Prove query work against **real records from the real store**, not fixtures.
  The sibling project's costliest bug was a fixture whose field names did not
  exist in the real export, which hid a missing title mapping indefinitely.
- Assert **counts** (`count()`, `namespace-uri()`), not serialised text.

## Open questions

1. `db:list-details()` reports 47,855 / 5,799 / 127,677 resources;
   `count(collection(...))` returns 47,853 / 5,799 / 127,551. The gap is small and
   unexplained. Settle it before the indexer trusts any count.
2. Whether the Multimedia → Object direction should be preferred over
   Object → Multimedia when building the related-items panel (the counts differ
   by 3×; see above).
3. Solr heap for a 5.2 GB corpus on a 16 GB box shared with BaseX, the desktop
   stack and a browser: **measured, not guessed** — 512 MB, 1 GB and 2 GB were
   each replayed against the same 240 faceted queries, and the live working set
   is only ~70-140 MB whatever the ceiling is. Set to 2 GB as deliberate
   headroom, since a ceiling is a limit and not a reservation (`-Xms1g` commits
   one gigabyte; the rest is taken on demand). The measurements are in the
   README; revisit only if the corpus grows by an order of magnitude.
