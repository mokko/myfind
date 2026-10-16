"""A thin Solr client, and the schema generated from the mapping.

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
"""

from __future__ import annotations

import httpx

from .config import Config


class SolrError(Exception):
    """Solr reported a failure."""


# Solr's query syntax treats these as operators. A facet value is data -- a
# title can contain a colon, a maker's name can contain a hyphen -- so they are
# backslash-escaped rather than passed through to be parsed as syntax.
_SOLR_SPECIAL = set('\\+-!(){}[]^"~*?:/&|')


def _escape(value: str) -> str:
    return "".join("\\" + ch if ch in _SOLR_SPECIAL else ch for ch in str(value))


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
        resp = self._client.post(
            f"{self.url}/{path.lstrip('/')}",
            json=payload if ctype == "application/json" else None,
            content=None if ctype == "application/json" else payload,
            headers={"Content-Type": ctype},
        )
        if resp.status_code >= 400:
            raise SolrError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")
        return resp.json() if resp.content else {}

    def _get(self, path: str, **params: object) -> dict:
        resp = self._client.get(f"{self.url}/{path.lstrip('/')}", params=params)
        if resp.status_code >= 400:
            raise SolrError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")
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
        """Index documents. Returns how many Solr reported as added."""
        if not docs:
            return 0
        path = "update?commit=true" if commit else "update"
        resp = self._post(path, docs)
        header = resp.get("responseHeader", {})
        if header.get("status") not in (0, None):
            raise SolrError(f"update failed: {resp}")
        return len(docs)

    def commit(self) -> None:
        self._post("update?commit=true", {})

    def search(
        self,
        q: str = "*:*",
        rows: int = 20,
        start: int = 0,
        facets: list[str] | None = None,
        facet_limit: int = 30,
        sort: str | None = None,
        fields: str = "*",
        filters: list[tuple[str, str]] | None = None,
        module: str | None = None,
    ) -> dict:
        """Run a faceted query.

        `facets` are field names WITHOUT the `_facet` suffix. `filters` are
        (field, value) pairs the user has selected.

        **Facet counts must exclude the facet's own filter.** If they do not, then
        selecting "Zeichnung" collapses the object_type facet to a single value
        and the user can never select a second type, so a facet becomes a
        one-way door. Solr's mechanism is a tag/exclusion pair: the filter is
        tagged, and the facet that must ignore it names that tag.

            fq={!tag=object_type}object_type_facet:"Zeichnung"
            facet.field={!ex=object_type}object_type_facet

        Counts then reflect every OTHER active filter, which is what a user
        reads a facet count to mean.
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
                f"{{!ex={f}}}{f}_facet" if f in selected else f"{f}_facet"
                for f in facets
            ]
            params["facet.limit"] = facet_limit
            params["facet.mincount"] = 1
            params["facet.sort"] = "count"
        return self._get("select", **params)

    # -- schema -----------------------------------------------------------

    def ensure_schema(self, config: Config) -> dict:
        """Create every field the mapping needs. Idempotent."""
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
                wanted.append(
                    {
                        "name": spec.field,
                        "type": "text_general",
                        "multiValued": True,
                        "stored": True,
                    }
                )
                if spec.has("facet") and spec.field not in facet_fields:
                    facet_fields.append(spec.field)
                    wanted.append(
                        {
                            "name": f"{spec.field}_facet",
                            "type": "string",
                            "multiValued": True,
                            "stored": False,
                            "docValues": True,
                        }
                    )
            for link in module.links:
                wanted.append(
                    {
                        "name": link.field,
                        "type": "string",
                        "multiValued": True,
                        "stored": True,
                    }
                )

        # add-field errors if the field already exists; that is the idempotent
        # case, so each is applied individually and an "already exists" reply is
        # not an error.
        created, existing = 0, 0
        for spec in wanted:
            try:
                self._post("schema", {"add-field": spec})
                created += 1
            except SolrError as exc:
                if "already exists" in str(exc) or "Field 'id' is not defined" in str(exc):
                    existing += 1
                else:
                    raise
        return {"created": created, "existing": existing, "fields": wanted}
