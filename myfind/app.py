"""The Flask app: per-entity search, results and record pages.

The split of labour is the whole design, and it is the same split VuFind and
Blacklight make:

- **Search, facets, ranking, paging -> Solr.** Facet counts mean reading a field
  across 181k records; measured in BaseX that is a ~40 s full pass, which is why
  an index exists at all.
- **The record page -> BaseX.** The XML is the system of record, and a record is
  one document with a known name, so it is an O(1) lookup (measured 1.5 s cold
  local client, well under that warm over REST) rather than a search.
- **Linked records -> BaseX, by document name.** A link carries the target's id,
  so `Person-218316.xml` is a lookup. The target may legitimately be absent: 14
  modules are referenced and not ingested, including the owner on every object.
  A dangling link renders its label and says so; it never 404s.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, url_for

from . import ria
from .basex import BaseXClient, BaseXError
from .config import Config, Module
from .config import load as load_config
from .indexer import BATCH_QUERY  # noqa: F401  (kept importable for tooling)
from .solr import SolrClient, SolrError

# Query parameters that are not facets.
RESERVED = {"q", "page", "sort", "per_page"}


@dataclass(frozen=True)
class DisplayField:
    """One row on a record page.

    A dataclass rather than a dict, deliberately: a dict key named `values`
    is unreachable from Jinja as `f.values`, because Jinja tries attribute
    lookup first and finds `dict.values` -- the bound method -- so
    `f.values | join(', ')` fails with "'builtin_function_or_method' object is
    not iterable". The dataclass has no such method, so the name is unambiguous.
    """

    field: str
    label: str
    values: list[str]


@dataclass(frozen=True)
class ResolvedLink:
    """One linked record on a record page.

    `present` is False when the target module was never ingested -- which is the
    common case here: 14 referenced modules are absent, including the owner on
    every object.
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

    `target` may be None when the reference names a module this installation does
    not serve -- `ObjOwnerRef -> Address` is on every object and there is no
    Address module. Then `target_name` is None (nothing to link to) and every
    link in the group renders as a label marked "not in this index".
    """

    label: str
    target: Module | None
    target_label: str
    target_name: str | None
    links: list[ResolvedLink]


def create_app(config_path: str | os.PathLike[str] | None = None) -> Flask:
    cfg_path = config_path or os.environ.get("MYFIND_CONFIG", "myfind.toml")
    config: Config = load_config(cfg_path)

    app = Flask(
        __name__,
        template_folder=str(_template_dir()),
        static_folder=str(_static_dir()),
        static_url_path="/static",
    )
    app.config["MYFIND"] = config

    # -- helpers ----------------------------------------------------------

    def bx_client() -> BaseXClient:
        return BaseXClient(
            url=config.basex.url,
            user=config.basex.user,
            password=config.basex.password,
            timeout=config.basex.timeout,
        )

    def solr_client() -> SolrClient:
        return SolrClient(config.solr.url)

    def selected_filters(args, module: Module) -> list[tuple[str, str]]:
        """Facet selections from the query string.

        **Only keys that are an actual facet of this entity are accepted.** An
        allow-list rather than a denylist of reserved names: any other query
        parameter (`limit`, `utm_source`, a typo) would otherwise be turned into
        a Solr filter and answered with `undefined field <x>_facet` and a 400.
        A field may repeat, which is an OR within that facet.
        """
        allowed = {spec.field for spec in module.facet_fields()}
        out: list[tuple[str, str]] = []
        for key in args.keys():
            if key in RESERVED or key not in allowed:
                continue
            for value in args.getlist(key):
                if value:
                    out.append((key, value))
        return out

    def _url_for_filters(module: Module, args, pairs: list[tuple[str, str]]) -> str:
        """Build a search URL from an explicit filter list, keeping q and sort.

        Both `q` and `sort` survive a facet click, because losing your query when
        you narrow the facets is the thing that makes a portal feel broken.
        """
        keep: list[tuple[str, str]] = []
        q = args.get("q", "")
        if q:
            keep.append(("q", q))
        keep.extend(pairs)
        sort = args.get("sort")
        if sort:
            keep.append(("sort", sort))
        query = "&".join(f"{k}={_quote(v)}" for k, v in keep)
        base = url_for("entity_search", module_name=module.name)
        return f"{base}?{query}" if query else base

    def _current_filters(args, module: Module) -> list[tuple[str, str]]:
        """The facet selections currently in the URL, for rebuilding it.

        Same allow-list as `selected_filters`, so a stray parameter is not
        carried along into every facet link.
        """
        allowed = {spec.field for spec in module.facet_fields()}
        return [
            (k, v)
            for k in args.keys()
            if k not in RESERVED and k in allowed
            for v in args.getlist(k)
            if v
        ]

    def facet_add_url(module: Module, args, field: str, value: str) -> str:
        """Add one facet value to the current selection."""
        pairs = _current_filters(args, module) + [(field, value)]
        return _url_for_filters(module, args, pairs)

    def facet_remove_url(module: Module, args, field: str, value: str) -> str:
        """Remove exactly one selected value, leaving the rest of the facet.

        Distinct from clearing the field: with a multi-valued facet the user
        expects to drop one chip and keep the others.
        """
        pairs = [p for p in _current_filters(args, module) if p != (field, value)]
        return _url_for_filters(module, args, pairs)

    def facet_clear_url(module: Module, args, field: str) -> str:
        """Remove every selection for one facet field."""
        pairs = [p for p in _current_filters(args, module) if p[0] != field]
        return _url_for_filters(module, args, pairs)

    def build_query(q: str, module: Module) -> str:
        """The user's terms -> a Solr query.

        An empty box is everything. Otherwise the terms go to the catch-all
        `text` field with the standard `edismax`-ish behaviour applied by Solr's
        default parser, and a term that looks like an object number is also
        matched against the object number field so "KdZ 5653" finds its record
        even when the title does not contain it.
        """
        q = (q or "").strip()
        if not q:
            return "*:*"
        # Escape only the syntax characters that would break the query; leave
        # the terms otherwise alone so a phrase in quotes still works.
        safe = "".join("\\" + ch if ch in "+-!(){}[]^~*?:/&|" else ch for ch in q)
        return f"text:({safe}) OR object_number:({safe})"

    # -- routes -----------------------------------------------------------

    @app.route("/")
    def home():
        with solr_client() as solr:
            counts = {}
            for module in config.modules:
                res = solr.search(q="*:*", rows=0, module=module.name)
                counts[module.name] = res["response"]["numFound"]
        return render_template(
            "home.html", config=config, counts=counts, q=request.args.get("q", "")
        )

    @app.route("/search")
    def global_search():
        """The single search box, across all three entities."""
        q = request.args.get("q", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        with solr_client() as solr:
            res = solr.search(
                q=build_query(q, config.modules[0]) if q else "*:*",
                rows=config.site.page_size * 2,
                start=(page - 1) * config.site.page_size * 2,
            )
        return render_template(
            "search_all.html",
            config=config,
            q=q,
            response=res["response"],
            page=page,
        )

    @app.route("/<module_name>/")
    def entity_search(module_name: str):
        try:
            module = config.module(module_name)
        except KeyError:
            abort(404)

        q = request.args.get("q", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        per_page = config.site.page_size
        filters = selected_filters(request.args, module)

        with solr_client() as solr:
            res = solr.search(
                q=build_query(q, module),
                rows=per_page,
                start=(page - 1) * per_page,
                facets=[f.field for f in module.facet_fields()],
                filters=filters,
                module=module.name,
                sort=_sort_param(request.args.get("sort"), module),
            )

        num = res["response"]["numFound"]
        return render_template(
            "search.html",
            config=config,
            module=module,
            q=q,
            response=res["response"],
            facets=res.get("facet_counts", {}).get("facet_fields", {}),
            selected=filters,
            page=page,
            pages=max(1, math.ceil(num / per_page)) if num else 1,
            facet_add_url=lambda f, v: facet_add_url(module, request.args, f, v),
            facet_remove_url=lambda f, v: facet_remove_url(module, request.args, f, v),
            facet_clear_url=lambda f: facet_clear_url(module, request.args, f),
            is_selected=_make_is_selected(filters),
        )

    @app.route("/<module_name>/<record_id>")
    def record(module_name: str, record_id: str):
        try:
            module = config.module(module_name)
        except KeyError:
            abort(404)

        document = f"{module.name}-{record_id}.xml"
        with bx_client() as bx:
            node = bx.fetch_record(module.database, document)
            if node is None:
                abort(404)
            item = ria.item_of(node)
            if item is None:
                abort(404)

            # Every display field, in config order.
            fields = []
            for spec in module.display_fields():
                values = ria.extract_values(item, spec)
                if not values:
                    continue
                if spec.field == "last_modified":
                    values = [ria.normalize_timestamp(v, config.basex.timezone_offset) or v
                              for v in values]
                fields.append(
                    DisplayField(
                        field=spec.field, label=label_for(spec.field), values=values
                    )
                )

            # Linked records. The target may be a module this installation does
            # not serve -- ObjOwnerRef points at Address and there is no Address
            # module -- so the lookup is tolerant and the group renders as
            # labels marked "not in this index".
            link_groups = []
            for link in module.links:
                targets = ria.extract_links(item, link)
                if not targets:
                    continue
                target_module = config.find(link.target)
                present = (
                    bx.existing_documents(
                        target_module.database,
                        target_module.name,
                        [t["id"] for t in targets],
                    )
                    if target_module
                    else set()
                )
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

        # Shown behind a disclosure so a cataloguer can see what the mapping read.
        # Truncated: some records serialise to tens of kilobytes of CLOBs.
        from xml.etree import ElementTree as ET

        raw_xml = ET.tostring(item, encoding="unicode")
        if len(raw_xml) > 40000:
            raw_xml = raw_xml[:40000] + "\n… truncated …"

        return render_template(
            "record.html",
            config=config,
            module=module,
            record_id=record_id,
            item=item,
            item_xml=raw_xml,
            fields=fields,
            link_groups=link_groups,
        )

    @app.route("/api/<module_name>")
    def api_search(module_name: str):
        try:
            module = config.module(module_name)
        except KeyError:
            return jsonify({"error": "unknown entity"}), 404
        q = request.args.get("q", "").strip()
        limit = min(200, int(request.args.get("limit", 20) or 20))
        with solr_client() as solr:
            res = solr.search(
                q=build_query(q, module),
                rows=limit,
                filters=selected_filters(request.args, module),
                module=module.name,
            )
        return jsonify(
            {
                "entity": module.name,
                "label": module.display_label(),
                "query": q,
                "numFound": res["response"]["numFound"],
                "docs": res["response"]["docs"],
            }
        )

    # A failure in either backend is reported as a service problem, not a 500.
    # A 500 says "this application is broken"; the honest message is "search is
    # unavailable", and it is the difference between a user retrying and a user
    # filing a bug. The underlying error is still shown, because a portal whose
    # search silently returns nothing is worse than one that says why.
    @app.errorhandler(SolrError)
    def _solr_error(exc: SolrError):
        return (
            render_template(
                "backend_error.html",
                config=config,
                backend="Solr",
                detail=str(exc),
                url=config.solr.url,
            ),
            503,
        )

    @app.errorhandler(BaseXError)
    def _basex_error(exc: BaseXError):
        return (
            render_template(
                "backend_error.html",
                config=config,
                backend="BaseX",
                detail=str(exc),
                url=config.basex.url,
            ),
            503,
        )

    @app.route("/healthz")
    def healthz():
        with solr_client() as solr:
            solr_ok = solr.alive()
            docs = solr.count() if solr_ok else None
        with bx_client() as bx:
            bx_ok = bx.ping()
        return jsonify({"solr": solr_ok, "solr_documents": docs, "basex": bx_ok})

    @app.context_processor
    def inject():
        # `label_for` is exposed so a template renders "Object number" rather
        # than the raw field name: the mapping's key is an identifier, not copy.
        return {
            "site": config.site,
            "modules": config.modules,
            "label_for": label_for,
        }

    return app


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
    app = create_app()
    host = os.environ.get("MYFIND_HOST", "127.0.0.1")
    port = int(os.environ.get("MYFIND_PORT", 5000))
    app.run(host=host, port=port, debug=bool(os.environ.get("MYFIND_DEBUG")))


if __name__ == "__main__":
    main()
