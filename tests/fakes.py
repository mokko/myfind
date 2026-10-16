"""In-memory stand-ins for the two backends -- asynchronous, like the app.

The route layer was the least-tested code in the project and the place every
rendering bug actually lived: two record pages returned 500 because a dict key
named `values` shadows `dict.values` in Jinja, and every asset page said
"Untitled record" because the heading chain was hardcoded. None of that needed a
real Solr or BaseX to catch -- it needed the page to be *rendered*.

These fakes let `TestClient` render every route with no services running, so the
suite stays fast and runnable anywhere. They are deliberately dumb.

**They are async because the app is.** Starlette's `TestClient` presents a
synchronous interface to the tests -- it drives the ASGI app through a portal --
so the assertions stay short and imperative, but the fakes must be awaitable
because that is what the routes call.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from myfind.basex import BaseXError
from myfind.solr import SolrError


class FakeSolr:
    """Serves canned results and facets, and records what it was asked."""

    def __init__(self, url: str, **kwargs) -> None:
        self.url = url
        # Every search() call, so a test can assert on the REQUEST the app built
        # -- facet tagging, the sort clause, the filter allow-list. Those are the
        # failures that raise no error.
        self.calls: list[dict] = []
        self.docs: list[dict] = []
        self.counts: dict[str, int] = {}
        self.facets: dict[str, dict[str, list]] = {}
        self._alive = True

    async def __aenter__(self) -> "FakeSolr":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def close(self) -> None:
        return None

    # -- the surface app.py uses ------------------------------------------
    async def alive(self) -> bool:
        return self._alive

    async def count(self) -> int:
        return sum(self.counts.values()) or len(self.docs)

    async def index(self, docs: list[dict], commit: bool = False) -> int:
        self.docs.extend(docs)
        return len(docs)

    async def commit(self) -> None:
        return None

    async def delete_all(self) -> None:
        self.docs.clear()

    async def ensure_schema(self, config) -> dict:
        return {"created": 0, "existing": 0, "fields": []}

    async def search(self, **params) -> dict:
        self.calls.append(params)
        module = params.get("module")
        docs = [d for d in self.docs if not module or d.get("module") == module]
        total = self.counts.get(module, len(docs)) if module else await self.count()

        facet_fields = {}
        for name in params.get("facets") or []:
            facet_fields[f"{name}_facet"] = list(self.facets.get(module, {}).get(name, []))

        rows = int(params.get("rows", 20))
        start = int(params.get("start", 0))
        return {
            "response": {
                "numFound": total,
                "start": start,
                "docs": docs[start : start + rows],
            },
            "facet_counts": {"facet_fields": facet_fields},
        }

    # -- convenience -------------------------------------------------------
    def last_call(self) -> dict:
        return self.calls[-1]


class FakeSolrDown(FakeSolr):
    """A Solr that is not listening -- the case that used to be an unhandled 500.

    Only the transport is missing; `alive()` still answers, because /healthz has
    to be able to report the outage rather than fail with it.
    """

    async def count(self) -> int:
        raise SolrError("cannot reach Solr: connection refused")

    async def search(self, **params) -> dict:
        raise SolrError("cannot reach Solr at http://127.0.0.1:9/solr/x: refused")

    async def ensure_schema(self, config) -> dict:
        raise SolrError("cannot reach Solr: connection refused")


class FakeBaseX:
    """Serves records from a dict of document name -> XML text."""

    def __init__(self, url: str = "http://localhost:8080/rest", **kwargs) -> None:
        self.url = url
        self.records: dict[str, str] = {}
        self.queries: list[tuple[str, dict]] = []
        self._alive = True

    async def __aenter__(self) -> "FakeBaseX":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return self._alive

    def add(self, document: str, xml: str) -> None:
        """Not async: a test populates the fake before making any request."""
        self.records[document] = xml

    async def fetch_record_text(self, database: str, document: str) -> str | None:
        """What the app actually calls: raw XML, unparsed.

        Absent documents return None -- a normal answer, not an error, since 14
        referenced modules were never ingested.
        """
        return self.records.get(document)

    async def fetch_record(self, database: str, document: str) -> ET.Element | None:
        xml = self.records.get(document)
        return ET.fromstring(xml) if xml else None

    async def existing_documents(self, database: str, module: str, ids: list[str]) -> set[str]:
        return {i for i in ids if f"{module}-{i}.xml" in self.records}

    async def count_documents(self, database: str) -> int:
        return len(self.records)

    async def query(self, xquery: str, **variables) -> str:
        self.queries.append((xquery, variables))
        return "\n".join(sorted(self.records))

    async def document_names(self, database: str) -> list[str]:
        return sorted(self.records)


class FakeBaseXDown(FakeBaseX):
    async def ping(self) -> bool:
        return False

    async def fetch_record_text(self, database: str, document: str):
        raise BaseXError("cannot reach BaseX at http://127.0.0.1:9/rest: refused")

    async def fetch_record(self, database: str, document: str):
        raise BaseXError("cannot reach BaseX at http://127.0.0.1:9/rest: refused")
