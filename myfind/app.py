"""The Starlette app: per-entity search, results and record pages.

The split of labour is the whole design, and it is the same split VuFind and
Blacklight make:

- **Search, facets, ranking, paging -> Solr.** Facet counts mean reading a field
  across 181k records; measured in BaseX that is a ~40 s full pass, which is why
  an index exists at all.
- **The record page -> BaseX.** The XML is the system of record, and a record is
  one document with a known name, so it is an O(1) lookup rather than a search.
- **Linked records -> BaseX, by document name.** A link carries the target's id,
  so `Person-218316.xml` is a lookup. The target may legitimately be absent: 14
  modules are referenced and not ingested, including the owner on every object.
  A dangling link renders its label and says so; it never 404s.

Why ASGI rather than WSGI, and the one trap in it:

Under WSGI every request blocked on its backend call, and concurrency came only
from running several worker *processes*. Under ASGI one process holds many
requests in flight -- but a single event loop means **CPU work blocks everyone**.
`ET.fromstring` on a record with large CLOBs is CPU work, so the parse is handed
to a worker thread rather than done on the loop. Migrating the framework without
that step would have made concurrency worse, not better.

What async actually buys here, measured on the real store: six record fetches
take 46 ms gathered against 207 ms sequential. It also makes three independent
things possible that the sync version could only do in series -- the three entity
counts on the home page, the two backend probes in /healthz, and resolving every
link group on a record page.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import anyio
import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, NoMatchFound, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import ria
from .basex import AsyncBaseXClient, BaseXError
from .config import Config, Module
from .config import load as load_config
from .solr import AsyncSolrClient, SolrError

# Query parameters that are not facets.
RESERVED = {"q", "page", "sort", "per_page"}


@dataclass(frozen=True)
class DisplayField:
    """One row on a record page.

    A dataclass rather than a dict, deliberately: a dict key named `values` is
    unreachable from Jinja as `f.values`, because Jinja tries attribute lookup
    first and finds `dict.values` -- the bound method -- so `f.values | join`
    fails with "'builtin_function_or_method' object is not iterable".
    """

    field: str
    label: str
    values: list[str]


@dataclass(frozen=True)
class ResolvedLink:
    """One linked record on a record page.

    `present` is False when the target module was never ingested -- the common
    case here, since 14 referenced modules are absent.
    """

    id: str
    label: str
    present: bool


@dataclass(frozen=True)
class LinkGroup:
    """All the links a record has through one reference.

    Named `links`, not `items`: a dict key called `items` is reachable from Jinja
    only as `group.items`, which finds `dict.items` -- the bound method -- and
    fails to iterate. Same trap as `DisplayField.values`.

    `target` is None when the reference names a module this installation does not
    serve -- `ObjOwnerRef -> Address` is on every object and there is no Address
    module. Then `target_name` is None and every link renders as a label marked
    "not in this index".
    """

    label: str
    target: Module | None
    target_label: str
    target_name: str | None
    links: list[ResolvedLink]


# ---------------------------------------------------------------------------
# Pure work, deliberately outside the request loop
# ---------------------------------------------------------------------------

def parse_record(
    raw_xml: str, module: Module, offset: str
) -> tuple[list[DisplayField], list[tuple[object, list[dict]]], str] | None:
    """Parse a record and extract everything the page needs. **CPU only.**

    Module-level and free of I/O so it can be handed to a worker thread. Returns
    None when the document carries no moduleItem, which is a 404 rather than an
    error.
    """
    try:
        node = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise BaseXError(f"stored document was not XML: {exc}") from exc

    item = ria.item_of(node)
    if item is None:
        return None

    fields: list[DisplayField] = []
    for spec in module.display_fields():
        values = ria.extract_values(item, spec)
        if not values:
            continue
        if spec.field == "last_modified":
            values = [
                ria.normalize_timestamp(v, offset) or v for v in values
            ]
        fields.append(
            DisplayField(field=spec.field, label=label_for(spec.field), values=values)
        )

    links = [(link, ria.extract_links(item, link)) for link in module.links]

    # Shown behind a disclosure so a cataloguer can see what the mapping read.
    # Truncated: some records serialise to tens of kilobytes of CLOBs.
    item_xml = ET.tostring(item, encoding="unicode")
    if len(item_xml) > 40000:
        item_xml = item_xml[:40000] + "\n… truncated …"

    return fields, links, item_xml


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------

def create_app(config_path: str | os.PathLike[str] | None = None) -> Starlette:
    cfg_path = config_path or os.environ.get("MYFIND_CONFIG", "myfind.toml")
    config: Config = load_config(cfg_path)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        """Two connection pools for the process, not one per request.

        Two rather than one because BaseX needs its credentials on every request
        and Solr has none -- a single shared client could carry only one.
        """
        app.state.basex_http = httpx.AsyncClient(
            auth=(config.basex.user, config.basex.password),
            timeout=config.basex.timeout,
            headers={"Content-Type": "application/query+xml; charset=utf-8"},
        )
        app.state.solr_http = httpx.AsyncClient(timeout=120.0)
        try:
            yield
        finally:
            await app.state.basex_http.aclose()
            await app.state.solr_http.aclose()

    templates = Jinja2Templates(
        directory=str(_template_dir()),
        context_processors=[_globals_for(config)],
    )

    # -- helpers ----------------------------------------------------------

    def bx_client(request: Request) -> AsyncBaseXClient:
        # getattr with a default: the pool only exists once lifespan has run, and
        # a client is perfectly able to create its own (which tests rely on).
        return AsyncBaseXClient(
            url=config.basex.url,
            user=config.basex.user,
            password=config.basex.password,
            timeout=config.basex.timeout,
            client=getattr(request.app.state, "basex_http", None),
        )

    def solr_client(request: Request) -> AsyncSolrClient:
        return AsyncSolrClient(
            url=config.solr.url,
            client=getattr(request.app.state, "solr_http", None),
        )

    def path_for(request: Request, name: str, **params: object) -> str:
        """A **path** for an endpoint, not a full URL.

        Starlette's `url_for` returns an absolute URL built from the request's
        Host, which embeds the host in every link and is wrong the moment the app
        sits behind a proxy that rewrites Host. Flask returned a path, and the
        templates and tests are written for that, so this keeps it.
        """
        return request.url_for(name, **params).path

    def selected_filters(request: Request, module: Module) -> list[tuple[str, str]]:
        """Facet selections from the query string.

        **Only keys that are an actual facet of this entity are accepted.** An
        allow-list rather than a denylist of reserved names: any other query
        parameter (`limit`, `utm_source`, a typo) would otherwise become a Solr
        filter and be answered with `undefined field <x>_facet` and a 400. A field
        may repeat, which is an OR within that facet.
        """
        allowed = {spec.field for spec in module.facet_fields()}
        out: list[tuple[str, str]] = []
        for key in request.query_params.keys():
            if key in RESERVED or key not in allowed:
                continue
            for value in request.query_params.getlist(key):
                if value:
                    out.append((key, value))
        return out

    def current_filters(request: Request, module: Module) -> list[tuple[str, str]]:
        """The facet selections currently in the URL, for rebuilding it.

        Same allow-list, so a stray parameter is not carried into every link.
        """
        allowed = {spec.field for spec in module.facet_fields()}
        return [
            (k, v)
            for k in request.query_params.keys()
            if k not in RESERVED and k in allowed
            for v in request.query_params.getlist(k)
            if v
        ]

    def url_for_filters(request: Request, module: Module, pairs: list[tuple[str, str]]) -> str:
        """A search URL from an explicit filter list, keeping q and sort.

        Both survive a facet click, because losing your query when you narrow the
        facets is the thing that makes a portal feel broken.
        """
        keep: list[tuple[str, str]] = []
        q = request.query_params.get("q", "")
        if q:
            keep.append(("q", q))
        keep.extend(pairs)
        sort = request.query_params.get("sort")
        if sort:
            keep.append(("sort", sort))
        base = path_for(request, "entity_search", module_name=module.name)
        query = "&".join(f"{k}={_quote(v)}" for k, v in keep)
        return f"{base}?{query}" if query else base

    def build_query(q: str, module: Module) -> str:
        """The user's terms -> a Solr query.

        An empty box is everything. Otherwise the terms go to the catch-all
        `text` field, and are also matched against the object number so
        "KdZ 5653" finds its record even when the title does not contain it.
        """
        q = (q or "").strip()
        if not q:
            return "*:*"
        safe = "".join("\\" + ch if ch in "+-!(){}[]^~*?:/&|" else ch for ch in q)
        return f"text:({safe}) OR object_number:({safe})"

    def render(request: Request, name: str, **context: object):
        return templates.TemplateResponse(request, name, context)

    def entity_or_404(module_name: str) -> Module:
        module = config.find(module_name)
        if module is None:
            raise HTTPException(status_code=404)
        return module

    # -- routes -----------------------------------------------------------
    # Order matters: Starlette matches in registration order and ranks nothing,
    # so "/api/{module_name}" must precede the generic "/{module_name}/{record_id}"
    # or "/api/Object" would be read as a record.

    async def home(request: Request):
        solr = solr_client(request)
        # Three counts, three independent queries -- gathered, not looped. The
        # sync version paid for them one after another.
        results = await asyncio.gather(
            *[solr.search(q="*:*", rows=0, module=m.name) for m in config.modules]
        )
        counts = {
            m.name: r["response"]["numFound"] for m, r in zip(config.modules, results)
        }
        return render(
            request, "home.html", config=config, counts=counts,
            q=request.query_params.get("q", ""),
        )

    async def global_search(request: Request):
        """The single search box, across all three entities."""
        q = request.query_params.get("q", "").strip()
        page = _int_param(request, "page", 1, minimum=1)
        per_page = config.site.page_size * 2
        solr = solr_client(request)
        res = await solr.search(
            q=build_query(q, config.modules[0]) if q else "*:*",
            rows=per_page,
            start=(page - 1) * per_page,
        )
        return render(
            request, "search_all.html", config=config, q=q,
            response=res["response"], page=page,
        )

    async def entity_search(request: Request):
        module = entity_or_404(request.path_params["module_name"])
        q = request.query_params.get("q", "").strip()
        page = _int_param(request, "page", 1, minimum=1)
        per_page = config.site.page_size
        filters = selected_filters(request, module)

        solr = solr_client(request)
        res = await solr.search(
            q=build_query(q, module),
            rows=per_page,
            start=(page - 1) * per_page,
            facets=[f.field for f in module.facet_fields()],
            filters=filters,
            module=module.name,
            sort=_sort_param(request.query_params.get("sort"), module),
        )

        num = res["response"]["numFound"]
        return render(
            request,
            "search.html",
            config=config,
            module=module,
            q=q,
            response=res["response"],
            facets=res.get("facet_counts", {}).get("facet_fields", {}),
            selected=filters,
            page=page,
            pages=max(1, math.ceil(num / per_page)) if num else 1,
            facet_add_url=lambda f, v: url_for_filters(
                request, module, current_filters(request, module) + [(f, v)]
            ),
            facet_remove_url=lambda f, v: url_for_filters(
                request, module,
                [p for p in current_filters(request, module) if p != (f, v)],
            ),
            facet_clear_url=lambda f: url_for_filters(
                request, module,
                [p for p in current_filters(request, module) if p[0] != f],
            ),
            is_selected=_make_is_selected(filters),
        )

    async def record(request: Request):
        module = entity_or_404(request.path_params["module_name"])
        record_id = request.path_params["record_id"]
        bx = bx_client(request)

        raw = await bx.fetch_record_text(module.database, f"{module.name}-{record_id}.xml")
        if raw is None:
            raise HTTPException(status_code=404)

        # The parse and every extraction is CPU, so it goes to a thread: on the
        # event loop it would stall every other request in flight.
        parsed = await anyio.to_thread.run_sync(
            parse_record, raw, module, config.basex.timezone_offset
        )
        if parsed is None:
            raise HTTPException(status_code=404)
        fields, links, item_xml = parsed

        # Linked records. The target may be a module this installation does not
        # serve -- ObjOwnerRef points at Address and there is no Address module --
        # so the lookup is tolerant and the group renders as labels marked "not
        # in this index".
        resolvable = []
        for link, targets in links:
            if not targets:
                continue
            target_module = config.find(link.target)
            ids = [t["id"] for t in targets]
            if target_module:
                resolvable.append(
                    bx.existing_documents(target_module.database, target_module.name, ids)
                )
            else:
                resolvable.append(_noop())

        # Every group's existence resolved CONCURRENTLY. The sync version could
        # only ask one after another.
        present_sets = list(await asyncio.gather(*resolvable)) if resolvable else []

        link_groups: list[LinkGroup] = []
        i = 0
        for link, targets in links:
            if not targets:
                continue
            target_module = config.find(link.target)
            present = present_sets[i] if target_module else set()
            i += 1
            link_groups.append(
                LinkGroup(
                    label=label_for(link.field),
                    target=target_module,
                    target_label=(
                        target_module.display_label() if target_module else link.target
                    ),
                    target_name=target_module.name if target_module else None,
                    links=[
                        ResolvedLink(
                            id=t["id"],
                            label=t["label"] or t["id"],
                            present=t["id"] in present,
                        )
                        for t in targets
                    ],
                )
            )

        return render(
            request,
            "record.html",
            config=config,
            module=module,
            record_id=record_id,
            item_xml=item_xml,
            fields=fields,
            link_groups=link_groups,
        )

    async def api_search(request: Request):
        module = config.find(request.path_params["module_name"])
        if module is None:
            return JSONResponse({"error": "unknown entity"}, status_code=404)
        q = request.query_params.get("q", "").strip()
        limit = min(200, _int_param(request, "limit", 20, minimum=1))
        solr = solr_client(request)
        res = await solr.search(
            q=build_query(q, module),
            rows=limit,
            filters=selected_filters(request, module),
            module=module.name,
        )
        return JSONResponse(
            {
                "entity": module.name,
                "label": module.display_label(),
                "query": q,
                "numFound": res["response"]["numFound"],
                "docs": res["response"]["docs"],
            }
        )

    async def healthz(request: Request):
        solr = solr_client(request)
        bx = bx_client(request)

        async def solr_state():
            if not await solr.alive():
                return False, None
            return True, await solr.count()

        # Both backends probed at once; neither waits on the other.
        (solr_ok, docs), bx_ok = await asyncio.gather(solr_state(), bx.ping())
        return JSONResponse({"solr": solr_ok, "solr_documents": docs, "basex": bx_ok})

    # A failure in either backend is reported as a service problem, not a 500.
    # A 500 says "this application is broken"; the honest message is "search is
    # unavailable", and it is the difference between a user retrying and a user
    # filing a bug. The underlying error is still shown, because a portal whose
    # search silently returns nothing is worse than one that says why.
    async def solr_error(request: Request, exc: Exception):
        return templates.TemplateResponse(
            request,
            "backend_error.html",
            {"config": config, "backend": "Solr", "detail": str(exc), "url": config.solr.url},
            status_code=503,
        )

    async def basex_error(request: Request, exc: Exception):
        return templates.TemplateResponse(
            request,
            "backend_error.html",
            {"config": config, "backend": "BaseX", "detail": str(exc), "url": config.basex.url},
            status_code=503,
        )

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            # The static mount goes FIRST. Starlette matches in registration order
            # and ranks nothing, so "/static/style.css" would otherwise be read as
            # a record with entity "static" and id "style.css" -- a 404 that no
            # test caught, because no test asks for a static file.
            Mount("/static", app=StaticFiles(directory=str(_static_dir())), name="static"),
            Route("/healthz", healthz, name="healthz"),
            Route("/search", global_search, name="global_search"),
            # Likewise before the generic record route, or "/api/Object" reads as
            # a record with entity "api".
            Route("/api/{module_name}", api_search, name="api_search"),
            Route("/", home, name="home"),
            Route("/{module_name}/", entity_search, name="entity_search"),
            Route("/{module_name}/{record_id}", record, name="record"),
        ],
        exception_handlers={
            SolrError: solr_error,
            BaseXError: basex_error,
        },
    )
    app.state.myfind = config
    return app


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def path_url_for(request: Request, name: str, **params: object) -> str:
    """A **path** for an endpoint, plus any extra parameters as a query string.

    Two Flask behaviours are restored here, both of which Starlette lacks:

    - **A path, not a full URL.** Starlette's `url_for` returns an absolute URL
      built from the request's Host, which embeds the host in every link and is
      wrong the moment the app sits behind a proxy that rewrites Host.
    - **Extra parameters become a query string.** Starlette accepts *only* path
      parameters and raises `NoMatchFound` for anything else, where Flask appended
      unknown kwargs as a query string. Templates written for Flask pass `q=...`,
      so without this `url_for('global_search', q=...)` is a hard error.
    """
    try:
        return request.url_for(name, **params).path
    except NoMatchFound:
        pass

    route = next(
        (r for r in request.app.routes if getattr(r, "name", None) == name), None
    )
    declared = set(re.findall(r"{(\w+)}", getattr(route, "path", "") or ""))
    if not declared and "path" in params:
        declared = {"path"}  # a Mount, which declares no convertors

    path_params = {k: v for k, v in params.items() if k in declared}
    extra = [(k, v) for k, v in params.items() if k not in declared]
    url = request.url_for(name, **path_params).path
    return f"{url}?{urlencode(extra)}" if extra else url


def _globals_for(config: Config):
    """The context processor: what every template gets.

    `url_for` is overridden here on purpose. Jinja resolves a context variable
    before an environment global, so returning a path-returning `url_for` shadows
    Starlette's absolute-URL one without touching a single template call site.
    """

    def inject(request: Request) -> dict:
        return {
            "site": config.site,
            "modules": config.modules,
            "label_for": label_for,
            "url_for": lambda name, **params: path_url_for(request, name, **params),
        }

    return inject


async def _noop() -> set[str]:
    """A group whose target module is not served has nothing to look up."""
    return set()


def _int_param(request: Request, name: str, default: int, minimum: int = 0) -> int:
    """A query parameter read defensively.

    `?page=abc` or `?page=` used to be a 500 on `int()`. A bad page number is a
    bad request, not a crash.
    """
    raw = request.query_params.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _template_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _static_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "static"


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _make_is_selected(filters: list[tuple[str, str]]):
    selected = set(filters)

    def is_selected(field: str, value: str) -> bool:
        return (field, value) in selected

    return is_selected


def label_for(field: str) -> str:
    """A readable label from a field name, so config needs no extra key.

    Special cases exist only where the mechanical reading is wrong or useless
    ("_ids" is a count of links, not a label).
    """
    special = {
        "object_number": "Object number",
        "object_number_sort": "Object number (sort)",
        "object_type": "Object type",
        "date_display": "Date",
        "material_technique": "Material / technique",
        "collection_group": "Collection group",
        "org_unit": "Organisational unit",
        "last_modified": "Last modified",
        "asset_type": "Asset type",
        "file": "File",
        "file_size": "File size",
        "has_thumbnail": "Has thumbnail",
        "maker": "Maker",
        # Both forms are shown on purpose: "Watteau, Antoine Jean" (inverted, what
        # a facet sorts on) and "Jean Antoine Watteau (1684-1721)" (natural, with
        # dates). Distinct labels, or the record page shows "Maker" twice.
        "maker_display": "Maker (full form)",
        "maker_ids": "Makers",
        "asset_ids": "Assets",
        "owner_ids": "Holding institution",
        "object_ids": "Objects",
        "photographer_ids": "Photographers",
        "authorised_form": "Authorised form",
        "person_type": "Person type",
    }
    return special.get(field, field.replace("_", " ").capitalize())


def _sort_param(sort: str | None, module: Module) -> str | None:
    """Translate a UI sort key into a Solr sort clause.

    Only fields the mapping marked sortable are accepted, so an arbitrary sort
    cannot be injected through the query string.
    """
    sortable = {f.field for f in module.sort_fields()}
    default = "score desc, last_modified desc"
    if not sort:
        return default
    key = sort.lstrip("-")
    if key not in sortable:
        return default
    direction = "asc" if sort.startswith("-") else "desc"
    # Sort on the field itself, never on a `_facet` companion: facet fields are
    # multi-valued strings, and Solr cannot sort a multi-valued field.
    return f"{key} {direction}"


def main() -> None:
    """Development entry point. Production uses the ASGI server directly:

        uvicorn myfind.app:create_app --factory --host 127.0.0.1 --port 8010
    """
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        raise SystemExit(
            "uvicorn is not installed. Install the prod extra: uv pip install -e '.[prod]'"
        )
    uvicorn.run(
        create_app(),
        host=os.environ.get("MYFIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYFIND_PORT", 5000)),
    )


if __name__ == "__main__":
    main()
