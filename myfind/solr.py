"""Solr clients -- one synchronous, one asynchronous -- and the schema they build.

The schema is **derived from `myfind.toml`**, not maintained beside it. A field
listed in the mapping gets its Solr fields created automatically, so the two
cannot drift -- which is the failure mode where a facet is configured in the app
and silently returns nothing because the Solr field was never created.

Field types, and why:

- `<field>`          `text_general`, multiValued, stored  -- search + display
- `<field>_facet`    `string`, multiValued, docValues      -- exact facet terms
- `text`             `text_general`, multiValued           -- the catch-all box

A facet needs `string`, not `text_general`: analysed text would split a facet
term on whitespace and tokenise it, so "KdZ 5653" would become two meaningless
facet values. Display and search want the analysed field. Hence both.

As in `basex.py`, the request building and the schema derivation are module-level
functions shared by both transports, so a sync path and an async path cannot
drift apart.

Which to use: `SolrClient` for the indexer and CLI tools, `AsyncSolrClient` for
the app.
"""

from __future__ import annotations

import httpx

from .config import Config

# Solr's query syntax treats these as operators. A facet value is data -- a
# title can contain a colon, a maker's name can contain a hyphen -- so they are
# backslash-escaped rather than passed through to be parsed as syntax.
_SOLR_SPECIAL = set('\\+-!(){}[]^"~*?:/&|')


class SolrError(Exception):
    """Solr reported a failure, or could not be reached."""


def _escape(value: str) -> str:
    return "".join("\\" + ch if ch in _SOLR_SPECIAL else ch for ch in str(value))


def _unreachable(exc: Exception, url: str) -> SolrError:
    """A Solr that is DOWN must look like a Solr that failed, not an unhandled
    transport exception: the app answers 503 for the first and a bare 500 for the
    second, and that distinction is the whole point of the error type."""
    return SolrError(f"cannot reach Solr at {url}: {exc}")


def _http_error(resp: httpx.Response) -> SolrError:
    """Keep Solr's own message. It is what made the facet 400 diagnosable, and
    losing it turns a one-line fix into a guessing game."""
    return SolrError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")


def _is_already_present(exc: SolrError) -> bool:
    """`add-field` on an existing field is the idempotent case, not a failure."""
    return "already exists" in str(exc) or "Field 'id' is not defined" in str(exc)


def _update_ok(resp: dict) -> bool:
    return resp.get("responseHeader", {}).get("status") in (0, None)


# ---------------------------------------------------------------------------
# Request building -- shared
# ---------------------------------------------------------------------------

def build_search_params(
    q: str = "*:*",
    rows: int = 20,
    start: int = 0,
    facets: list[str] | None = None,
    facet_limit: int = 30,
    sort: str | None = None,
    fields: str = "*",
    filters: list[tuple[str, str]] | None = None,
    module: str | None = None,
) -> dict[str, object]:
    """The `select` parameters for a faceted query.

    `facets` are field names WITHOUT the `_facet` suffix. `filters` are
    (field, value) pairs the user has selected.

    **Facet counts must exclude the facet's own filter.** If they do not, then
    selecting "Zeichnung" collapses the object_type facet to a single value and
    the user can never select a second type, so a facet becomes a one-way door.
    Solr's mechanism is a tag/exclusion pair: the filter is tagged, and the facet
    that must ignore it names that tag.

        fq={!tag=object_type}object_type_facet:"Zeichnung"
        facet.field={!ex=object_type}object_type_facet

    Counts then reflect every OTHER active filter, which is what a user reads a
    facet count to mean.
    """
    params: dict[str, object] = {
        "q": q,
        "rows": rows,
        "start": start,
        "fl": fields,
        "wt": "json",
    }
    if sort:
        params["sort"] = sort

    fqs: list[str] = []
    if module:
        fqs.append(f'module:"{_escape(module)}"')
    selected: set[str] = set()
    for field, value in filters or []:
        selected.add(field)
        fqs.append(f'{{!tag={field}}}{field}_facet:"{_escape(value)}"')
    if fqs:
        params["fq"] = fqs

    if facets:
        params["facet"] = "true"
        params["facet.field"] = [
            f"{{!ex={f}}}{f}_facet" if f in selected else f"{f}_facet" for f in facets
        ]
        params["facet.limit"] = facet_limit
        params["facet.mincount"] = 1
        params["facet.sort"] = "count"
    return params


def build_schema_fields(config: Config) -> list[dict]:
    """Every Solr field the mapping needs, merged by name.

    Merging matters: several fields are legitimately shared across modules --
    `title` (Object and Person), `org_unit` (all three), `object_ids` (Person and
    Multimedia) -- so without it the same name is added once per module that maps
    it. Identical definitions collapse harmlessly; **conflicting** ones are an
    error, and must be, because otherwise whichever module happens to be iterated
    first silently decides the type and a facet that one module declares is
    quietly absent because another declared the field without it.
    """
    wanted: list[dict] = [
        {"name": "id", "type": "string", "multiValued": False, "stored": True,
         "required": True, "uniqueKey": True},
        {"name": "module", "type": "string", "stored": True},
        {"name": "record_id", "type": "string", "stored": True},
        {"name": "text", "type": "text_general", "multiValued": True, "stored": False},
        {"name": "last_modified", "type": "pdate", "stored": True},
    ]
    facet_fields: list[str] = []
    for module in config.modules:
        for spec in module.fields:
            if spec.field == "last_modified":
                # Declared explicitly above as `pdate`; adding it again as
                # text_general would either fail or, worse, win the race and
                # leave a date sorted as a string.
                continue
            if spec.is_sort_key():
                # A sort key exists to be ordered on. Solr cannot sort a
                # multi-valued field, and a `text_general` one is analysed and
                # tokenised, so "KdZ 005653" would sort on its parts.
                wanted.append(
                    {"name": spec.field, "type": "string", "multiValued": False,
                     "stored": True, "docValues": True}
                )
            else:
                wanted.append(
                    {"name": spec.field, "type": "text_general",
                     "multiValued": True, "stored": True}
                )
            if spec.has("facet") and spec.field not in facet_fields:
                facet_fields.append(spec.field)
                wanted.append(
                    {"name": f"{spec.field}_facet", "type": "string",
                     "multiValued": True, "stored": False, "docValues": True}
                )
        for link in module.links:
            wanted.append(
                {"name": link.field, "type": "string", "multiValued": True, "stored": True}
            )

    by_name: dict[str, dict] = {}
    for spec in wanted:
        name = spec["name"]
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = spec
        elif previous != spec:
            raise ValueError(
                f"field {name!r} is declared with two different definitions: "
                f"{previous} vs {spec}. Two modules mapping one field must agree "
                f"on its type and options."
            )
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Synchronous client -- indexer and CLI tools
# ---------------------------------------------------------------------------

class SolrClient:
    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SolrClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport --------------------------------------------------------

    def _post(self, path: str, payload: object, ctype: str = "application/json") -> dict:
        try:
            resp = self._client.post(
                f"{self.url}/{path.lstrip('/')}",
                json=payload if ctype == "application/json" else None,
                content=None if ctype == "application/json" else payload,
                headers={"Content-Type": ctype},
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json() if resp.content else {}

    def _get(self, path: str, **params: object) -> dict:
        try:
            resp = self._client.get(f"{self.url}/{path.lstrip('/')}", params=params)
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json()

    # -- operations -------------------------------------------------------

    def alive(self) -> bool:
        try:
            return "response" in self._get("select", q="*:*", rows=0)
        except Exception:
            return False

    def count(self) -> int:
        return int(self._get("select", q="*:*", rows=0)["response"]["numFound"])

    def delete_all(self) -> None:
        self._post("update?commit=true", {"delete": {"query": "*:*"}})

    def index(self, docs: list[dict], commit: bool = False) -> int:
        """Index documents. Returns how many were sent."""
        if not docs:
            return 0
        path = "update?commit=true" if commit else "update"
        resp = self._post(path, docs)
        if not _update_ok(resp):
            raise SolrError(f"update failed: {resp}")
        return len(docs)

    def commit(self) -> None:
        self._post("update?commit=true", {})

    def search(self, **kwargs) -> dict:
        return self._get("select", **build_search_params(**kwargs))

    def ensure_schema(self, config: Config) -> dict:
        """Create every field the mapping needs. Idempotent."""
        wanted = build_schema_fields(config)
        created, existing = 0, 0
        for spec in wanted:
            try:
                self._post("schema", {"add-field": spec})
                created += 1
            except SolrError as exc:
                if _is_already_present(exc):
                    existing += 1
                else:
                    raise
        return {"created": created, "existing": existing, "fields": wanted}


# ---------------------------------------------------------------------------
# Asynchronous client -- the app
# ---------------------------------------------------------------------------

class AsyncSolrClient:
    """The same surface, awaited.

    Takes an injected `httpx.AsyncClient` so the app holds **one** connection pool
    for its lifetime. Under WSGI a client per request was the honest shape,
    because each request had its own thread; under ASGI a pool per request would
    throw away connection reuse and forfeit much of what the event loop is for.
    """

    def __init__(
        self, url: str, timeout: float = 120.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "AsyncSolrClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # -- transport --------------------------------------------------------

    async def _post(self, path: str, payload: object, ctype: str = "application/json") -> dict:
        try:
            resp = await self.client.post(
                f"{self.url}/{path.lstrip('/')}",
                json=payload if ctype == "application/json" else None,
                content=None if ctype == "application/json" else payload,
                headers={"Content-Type": ctype},
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json() if resp.content else {}

    async def _get(self, path: str, **params: object) -> dict:
        try:
            resp = await self.client.get(f"{self.url}/{path.lstrip('/')}", params=params)
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json()

    # -- operations -------------------------------------------------------

    async def alive(self) -> bool:
        try:
            return "response" in await self._get("select", q="*:*", rows=0)
        except Exception:
            return False

    async def count(self) -> int:
        return int((await self._get("select", q="*:*", rows=0))["response"]["numFound"])

    async def delete_all(self) -> None:
        await self._post("update?commit=true", {"delete": {"query": "*:*"}})

    async def index(self, docs: list[dict], commit: bool = False) -> int:
        if not docs:
            return 0
        path = "update?commit=true" if commit else "update"
        resp = await self._post(path, docs)
        if not _update_ok(resp):
            raise SolrError(f"update failed: {resp}")
        return len(docs)

    async def commit(self) -> None:
        await self._post("update?commit=true", {})

    async def search(self, **kwargs) -> dict:
        return await self._get("select", **build_search_params(**kwargs))

    async def ensure_schema(self, config: Config) -> dict:
        wanted = build_schema_fields(config)
        created, existing = 0, 0
        for spec in wanted:
            try:
                await self._post("schema", {"add-field": spec})
                created += 1
            except SolrError as exc:
                if _is_already_present(exc):
                    existing += 1
                else:
                    raise
        return {"created": created, "existing": existing, "fields": wanted}
