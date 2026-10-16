# myfind compared with VuFind and Blacklight

Written against the projects as they stand on 2026-10-16, from their own
repositories rather than from summaries: `vufind-org/vufind` (default branch
`dev`) and `projectblacklight/blacklight` (default branch `main`).

**Verdict in one line:** myfind implements the same architecture and the core
discovery loop, at roughly 1% of the scale — it is a proof of concept that shares
their shape, not a replacement for either.

---

## At a glance

| | myfind | VuFind | Blacklight |
|---|---|---|---|
| Language | Python ≥ 3.11 | PHP ≥ 8.2 | Ruby ≥ 3.3 |
| Framework | Flask + Jinja2 | Laminas MVC 3.8, Symfony Console | Rails engine |
| Search index | Solr 9.11 | Solr | Solr |
| License | **MIT** | **GPL-2.0-only** | **Apache-2.0** |
| First release | 2026 | 2013 (repo), earlier in production | 2009 |
| Stars / forks | 0 / 0 | 306 / 403 | 798 / 255 |
| Last commit | today | 2026-10-15 | 2026-10-12 |
| Source size | ~2,000 lines + 2,000 lines of tests | ~6,100 paths | Rails engine |
| Ingest pipeline | **included** | included | **not included** |

Blacklight's own summary is the most instructive: *"a discovery interface for any
Solr index."* It is a **toolkit** — you bring the data and the model. VuFind is an
**application** — you configure it and it works. myfind is closer to Blacklight in
philosophy and closer to VuFind in scope-of-a-single-deployment.

---

## The architecture is the same

All three rest on one idea, and it is worth stating plainly because it is what
makes them comparable at all:

> The **system of record** holds the authoritative records. A **Solr index** holds
> a flattened, denormalised copy for search. The **UI** answers a query from the
> index and fetches a record from the store for display.

myfind's execution of it:

```
browser ─> Flask ─┬─> Solr   search, facets, sort over 181,203 documents
                  └─> BaseX  record pages and link resolution, by document name
```

Blacklight and VuFind put Solr in the same position; they differ in what they
expect on the other side. Blacklight expects *another* store you have already
indexed from (usually Fedora/Hydra, or anything). VuFind expects an **ILS** and
MARC records.

The concrete consequence for myfind is that its "system of record" is BaseX —
native XML, one document per record, named `Object-447469.xml` — which makes a
record page an O(1) name lookup (measured 1.5 s cold, far less warm) instead of a
search. Neither VuFind nor Blacklight has that particular luxury; both are
generally fetching a record from an ILS over a protocol, or from a repository
over HTTP, and both cache or denormalise what they can.

---

## Feature comparison

Evidence below is file counts from each project's own tree, not marketing lists.

### Search and browse

| Capability | myfind | VuFind | Blacklight |
|---|---|---|---|
| Keyword search over one Solr core | ✅ | ✅ | ✅ |
| Facets with counts | ✅ 6 (Objects) | ✅ | ✅ |
| **Facet counts exclude own filter** | ✅ (verified) | ✅ | ✅ |
| Sorting | ✅ 3 keys | ✅ | ✅ |
| Fielded search (`title:…`) | ✅ via Solr syntax | ✅ | ✅ |
| **Advanced search form** | ❌ | ✅ | ✅ |
| Date / range facets | ❌ | ✅ | ✅ (RangeLimit) |
| Hierarchical facets | ❌ | ✅ (47 files) | ✅ |
| Highlighting of hit terms | ❌ | ✅ | ✅ |
| Spelling / did-you-mean | ❌ | ✅ (6 files) | ✅ (`_did_you_mean`) |
| Search scopes ("catalog only") | ❌ | ✅ (Channels, 14 files) | ✅ |
| Alphabetic browse | ❌ | ✅ (3 files) | ❌ |
| Recommendations / more-like-this | ❌ | ✅ (Recommend subsystem) | ✅ |
| Result grouping / merging | ❌ | ✅ (WorkKeys, MergeRecursiveTrait) | ✅ (`_group`) |
| Pagination, per-page size | ✅ | ✅ | ✅ |

### Records and links

| Capability | myfind | VuFind | Blacklight |
|---|---|---|---|
| Record page | ✅ 3 entities | ✅ | ✅ |
| Linked records (record → record) | ✅ (BaseX name lookup) | ✅ | ✅ |
| Dangling link handled gracefully | ✅ ("not in this index") | — | — |
| Authority control | ❌ | ✅ (7 files) | ❌ (BYO) |
| Hierarchical collections | ❌ | ✅ | ✅ |
| Format drivers (MARC, EAD, DC…) | ❌ (one shape: RIA XML) | ✅ **36 record drivers** | ❌ (BYO model) |
| ILS integration (holdings, availability) | ❌ | ✅ **48 ILS drivers** | ❌ |
| Covers / TOC / abstracts | ❌ | ✅ (88 files) | ✅ |

### Export, APIs, machine access

| Capability | myfind | VuFind | Blacklight |
|---|---|---|---|
| JSON API | ✅ `/api/<entity>` | ✅ (VuFindApi) | ✅ (jbuilder) |
| Citation export (RIS, BibTeX, EndNote) | ❌ | ✅ (8 files) | ✅ (`_citation`) |
| RSS / Atom feeds | ❌ | ✅ | ✅ (`_document.rss/atom`) |
| OpenSearch description | ❌ | ✅ | ✅ |
| **OAI-PMH server** | ❌ | ✅ (7 files) | ❌ |
| Sitemap for crawlers | ❌ | ✅ (36 files) | ❌ |
| SRU / Z39.50 | ❌ | ✅ | ❌ |

### Users and sessions

| Capability | myfind | VuFind | Blacklight |
|---|---|---|---|
| Bookmarks / saved items | ❌ | ✅ (Favorites, 15 files) | ✅ (`bookmark.rb`) |
| Search history | ❌ | ✅ | ✅ |
| Saved searches + email alerts | ❌ | ✅ (6 files) | ✅ (`record_mailer`) |
| User accounts / authentication | ❌ | ✅ (MyResearch) | ✅ (BYO) |
| Tags, comments, reviews | ❌ | ✅ (41 files) | ❌ |
| Interlibrary loan, document delivery | ❌ | ✅ | ❌ |
| QR codes, SMS citations | ❌ | ✅ | ❌ |

### Operations

| Capability | myfind | VuFind | Blacklight |
|---|---|---|---|
| i18n / translations | ❌ | ✅ | ✅ |
| Themes | ❌ (one stylesheet) | ✅ (VuFindTheme) | ✅ (BYO) |
| Concurrency model | sync, 4 processes | PHP-FPM | Puma/Passenger |
| Health endpoint | ✅ `/healthz` | ❌ | ❌ |
| Backend outage → 503, not empty results | ✅ (tested) | — | — |
| Automated tests | 144, 93% coverage | PHPUnit suite | RSpec suite |
| Index rebuild, measured | **11.5 min / 181k docs** | — | — |

Legend: ✅ shipped · ❌ absent · — not applicable or not verified.
"BYO" = bring your own; the engine provides the hook, not the feature.

---

## Where myfind genuinely differs, rather than merely lacks

Four differences are deliberate, and worth separating from the gaps:

**1. It owns the ingest.** Blacklight ships no indexer — you write one (usually
Traject for MARC). myfind has one, mapping RIA XML → Solr from `myfind.toml`, with
a resumable receipt and a measured 11.5-minute full rebuild. For a single known
source, that is less machinery than adopting Traject and a Rails ingestion
pipeline.

**2. The mapping is data, and the Solr schema is generated from it.**
`myfind.toml` declares fields and their roles (`search`/`facet`/`display`/`sort`);
the schema, the indexer and the record page all derive from that one file. VuFind
configures through `.ini` files across many concerns; Blacklight configures through
`blacklight_config` in Ruby. Both are more powerful and both have more surface.

**3. The record page is rendered from the store, live.** Not from the index, and
not cached. That is why a missing link renders as *"not in this index"* rather than
404ing, and why a stale index can never produce a wrong record page — only a
missing search hit.

**4. Failure is explicit.** A backend that is down returns 503 naming the backend
and quoting its error; the tests assert that an outage never renders as "no
results found". Neither VuFind nor Blacklight documents this as a behaviour.

---

## What it would take to be a real discovery layer

Ordered by how much each would change what the thing *is*, not by effort:

1. **Range and hierarchical facets**, and term highlighting. These are what make
   a discovery interface feel like one; the machinery is Solr-side and mostly
   config.
2. **Record export** — RIS/BibTeX/citation — and **RSS/Atom**. Cheap, high value
   for a research audience, and the current JSON API covers only half the need.
3. **Saved searches and alerts.** Needs persistence and a scheduler; the first
   thing here that requires state.
4. **Auth + bookmarks.** Needs a user model and a login path — and once it exists,
   the whole data-governance question about internal fields becomes a design
   constraint rather than a footnote.
5. **A second metadata dialect.** Right now the code assumes RIA XML shapes
   throughout `ria.py`. VuFind's 36 record drivers and 48 ILS drivers exist
   precisely because a real deployment never has one source. This is the change
   that would force an abstraction layer.
6. **i18n and theming.** Not hard, entirely absent.
7. **ILS/repository integration** for live holdings and availability. Only
   relevant if the portal must show what is currently on the shelf.

Items 1–2 are days. Item 5 is the architectural one.

---

## Scale and maturity, honestly

| | myfind | VuFind | Blacklight |
|---|---|---|---|
| Deployments | 1 (local) | many, worldwide | many, incl. Stanford, UVA |
| Contributors | 1 | 403 forks' worth | 255 forks' worth |
| Years of production hardening | 0 | 13+ | 17 |
| Documentation | README + AGENTS.md | full manual, wiki | guides + YARD |
| Known scope | 3 entities, 1 dialect | MARC + many | anything in Solr |

The distance is not in the code — myfind's core loop is real and measured — but
in everything accumulated around it: drivers, translations, edge cases, and the
institutional knowledge that comes from years of other people's catalogues.

---

## On licensing

Worth stating because it affects reuse:

- **VuFind is GPL-2.0-only.** Adopting it into anything proprietary is a
  licensing decision, not just a technical one.
- **Blacklight is Apache-2.0** — permissive, with the copyright held by UVA,
  Stanford, Johns Hopkins and Data Curation Experts.
- **myfind is MIT** — the most permissive of the three, and the least encumbered.

GitHub reports Blacklight's license as `NOASSERTION`; the file itself is Apache
2.0, with a multi-institution copyright header that defeats automatic detection.

---

## Conclusion

If the goal is to run a library or museum discovery portal today, **use VuFind or
Blacklight** — the drivers alone are years of work, and neither is going to be
out-featured by a proof of concept.

If the goal is to understand how such a system works, to own the ingest from a
specific XML source, or to keep the whole thing small enough to read and change in
an afternoon — myfind does the same job on the same architecture, over 181,203
real records, with its costs measured rather than assumed. The useful thing it
contributes is not features but the numbers: an 11.5-minute rebuild, a 33 MB index,
a ~140 MB working set, and a documented list of the traps that a portal of this
shape hits.
