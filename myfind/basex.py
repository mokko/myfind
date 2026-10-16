"""BaseX REST clients -- one synchronous, one asynchronous.

Two transports over one set of requests. The query text, the escaping and the XML
parsing live in module-level functions used by **both** clients, because the
alternative -- writing each query twice -- is how a sync and an async path drift
apart, and the drift surfaces as a different answer from the same URL depending
on which one served it.

Which to use:

- `BaseXClient` (sync) for the indexer and command-line tools. They are
  sequential by nature and an event loop buys them nothing.
- `AsyncBaseXClient` for the app. Under WSGI every request blocked on its backend
  call; under ASGI a blocking call inside a coroutine stalls every other request
  sharing that event loop.

Transport is BaseX's documented POST envelope:

    <query xmlns="http://basex.org/rest">
      <text><![CDATA[ ...xquery... ]]></text>
      <variable name="db" value="..."/>
    </query>

Values are escaped into the XML, so a stray & or < cannot corrupt the request.

Three rules this module exists to enforce, all learned on this corpus:

1. **Never build XQuery by f-string-ing a user string into it.** Values go
   through bound external variables; only names this module has validated are
   interpolated.
2. **A record is fetched by document name, not by searching for it.** The store
   is one document per record (`Object-447469.xml`), so `doc()` is an O(1) name
   lookup while `collection()[@id = ...]` is a full scan of 47,855 documents --
   measured at 10.3 s against 1.5 s.
3. **External variables must be declared in the query prolog**, or BaseX answers
   `[XPST0008] Undeclared variable: $db`. That is why every query below is a
   complete string carrying its own `declare` lines.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import httpx

REST_NS = "http://basex.org/rest"

# A database name we are willing to interpolate into a query literal. BaseX
# database names are identifiers; a name carrying a space, quote or newline could
# end the literal early, so it is refused rather than escaped.
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
# A stored document name, as returned by db:list ("Object-447469.xml").
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


class BaseXError(Exception):
    """BaseX reported a failure, or could not be reached."""


# ---------------------------------------------------------------------------
# Requests -- one definition each, shared by both clients
# ---------------------------------------------------------------------------

def fetch_record_query() -> str:
    """One stored record by document name.

    **The `doc-available()` guard is load-bearing.** A bare
    `doc('sync_Object/Object-999999999.xml')` is not "no result" -- BaseX raises
    `[basex:doc] Database path ... yields no documents` and the REST layer
    answers HTTP 500. Without the guard, following one dangling link is a server
    fault rather than a graceful "not in this index", and a typo in a URL looks
    like a bug.
    """
    return (
        "declare variable $p external;\n"
        "if (doc-available($p)) then doc($p) else ()"
    )


def existing_documents_query() -> str:
    """Which of these record ids exist as stored documents.

    **One query, not one per id**: a record page with 20 linked records is
    otherwise 20 round trips.
    """
    return (
        "declare variable $db external;\n"
        "declare variable $module external;\n"
        "declare variable $ids external;\n"
        "for $i in tokenize($ids, ',')\n"
        "where doc-available($db || '/' || $module || '-' || $i || '.xml')\n"
        "return $i"
    )


def document_names_query(database: str) -> str:
    """Every stored document name in a database.

    `db:list()` is a name lookup, not a scan -- which is what makes enumerating
    the store cheap enough to drive the indexer from it.
    """
    return f"db:list({_xq_string(_validated(database, _DB_NAME_RE, 'database name'))})"


def count_documents_query(database: str) -> str:
    return f"count(collection({_xq_string(_validated(database, _DB_NAME_RE, 'database name'))}))"


#: True if the server answers a trivial expression. Note the port: BaseX 12 uses
#: 8080 for REST; older docs say 8984 and are wrong for this build.
PING_QUERY = "1 to 3"


# ---------------------------------------------------------------------------
# Parsing -- also shared
# ---------------------------------------------------------------------------

def parse_single(text: str) -> ET.Element | None:
    """A result that is one well-formed document, or None when empty."""
    text = text.strip()
    if not text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc


def parse_nodes(text: str) -> list[ET.Element]:
    """A result that is a *sequence* of elements.

    A `for ... return <x/>` is N top-level elements, which is not a well-formed
    document and cannot be parsed as one. Wrapping is how the sequence is
    reclaimed.
    """
    text = text.strip()
    if not text:
        return []
    if text.startswith("<?xml"):
        text = text.split("?>", 1)[1].lstrip()
    try:
        return list(ET.fromstring(f"<wrap>{text}</wrap>"))
    except ET.ParseError as exc:
        raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _xq_string(value: str) -> str:
    """A single-quoted XQuery string literal (quote doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def _validated(name: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.fullmatch(name):
        raise ValueError(f"not a valid BaseX {what}: {name!r}")
    return name


def doc_name(module: str, record_id: str | int) -> str:
    """The stored document name for a record: ('Object', 447469) -> Object-447469.xml."""
    return _validated(f"{module}-{record_id}.xml", _DOC_NAME_RE, "document name")


def doc_path(database: str, document: str) -> str:
    """The path argument for fn:doc(): 'sync_Object/Object-447469.xml'."""
    return (
        f"{_validated(database, _DB_NAME_RE, 'database name')}/"
        f"{_validated(document, _DOC_NAME_RE, 'document name')}"
    )


def build_body(query: str, variables: dict[str, object]) -> bytes:
    """The documented POST envelope, XML-escaped.

    A CDATA close in the query text would end it early, so it is refused rather
    than silently truncating the query.
    """
    if "]]>" in query:
        raise ValueError("query text may not contain a CDATA close (]]>)")
    parts = [f'<query xmlns="{REST_NS}">', f"<text><![CDATA[{query}]]></text>"]
    for name, value in variables.items():
        parts.append(
            f"<variable name={quoteattr(str(name))} "
            f"value={quoteattr(escape(str(value)))}/>"
        )
    parts.append("</query>")
    return "".join(parts).encode("utf-8")


def _safe_ids(ids: list[str]) -> list[str]:
    """Ids arrive from a URL, so only digits continue on to a path expression."""
    return [str(i) for i in ids if str(i).isdigit()]


def _unreachable(exc: Exception, url: str) -> BaseXError:
    """An unreachable BaseX must look like a failed BaseX, not a transport crash.

    The app answers 503 for the first and a bare 500 for the second, so this
    distinction is the difference between "search is down" and "the app is
    broken".
    """
    return BaseXError(f"cannot reach BaseX at {url}: {exc}")


def _http_error(resp: httpx.Response) -> BaseXError:
    return BaseXError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")


# ---------------------------------------------------------------------------
# Synchronous client -- indexer and CLI tools
# ---------------------------------------------------------------------------

class BaseXClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = (user, password)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def __enter__(self) -> "BaseXClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/query+xml; charset=utf-8"},
            )
        return self._client

    def query(self, xquery: str, **variables: object) -> str:
        """Run a query, return the raw serialised output."""
        try:
            resp = self.client.post(self.url, content=build_body(xquery, variables))
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.text

    def query_xml(self, xquery: str, **variables: object) -> ET.Element | None:
        return parse_single(self.query(xquery, **variables))

    def query_nodes(self, xquery: str, **variables: object) -> list[ET.Element]:
        return parse_nodes(self.query(xquery, **variables))

    def ping(self) -> bool:
        try:
            return self.query(PING_QUERY).strip() == "1\n2\n3"
        except Exception:
            return False

    def fetch_record(self, database: str, document: str) -> ET.Element | None:
        """One record by document name. O(1), and None when absent."""
        return self.query_xml(fetch_record_query(), p=doc_path(database, document))

    def existing_documents(self, database: str, module: str, ids: list[str]) -> set[str]:
        safe = _safe_ids(ids)
        if not safe:
            return set()
        text = self.query(
            existing_documents_query(), db=database, module=module, ids=",".join(safe)
        )
        return {line.strip() for line in text.split("\n") if line.strip()}

    def count_documents(self, database: str) -> int:
        return int(self.query(count_documents_query(database)).strip() or 0)

    def document_names(self, database: str) -> list[str]:
        return [
            n.strip()
            for n in self.query(document_names_query(database)).split("\n")
            if n.strip()
        ]


# ---------------------------------------------------------------------------
# Asynchronous client -- the app
# ---------------------------------------------------------------------------

class AsyncBaseXClient:
    """The same surface, awaited.

    Takes an injected `httpx.AsyncClient` so the app can share one connection
    pool across requests, instead of opening a pool per request as the sync
    client must (under WSGI a request per thread made per-request clients the
    honest shape; under ASGI that would throw away connection reuse entirely).
    """

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = (user, password)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "AsyncBaseXClient":
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
            self._client = httpx.AsyncClient(
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/query+xml; charset=utf-8"},
            )
        return self._client

    async def query(self, xquery: str, **variables: object) -> str:
        try:
            resp = await self.client.post(self.url, content=build_body(xquery, variables))
        except httpx.HTTPError as exc:
            raise _unreachable(exc, self.url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.text

    async def query_xml(self, xquery: str, **variables: object) -> ET.Element | None:
        return parse_single(await self.query(xquery, **variables))

    async def query_nodes(self, xquery: str, **variables: object) -> list[ET.Element]:
        return parse_nodes(await self.query(xquery, **variables))

    async def ping(self) -> bool:
        try:
            return (await self.query(PING_QUERY)).strip() == "1\n2\n3"
        except Exception:
            return False

    async def fetch_record(self, database: str, document: str) -> ET.Element | None:
        return await self.query_xml(
            fetch_record_query(), p=doc_path(database, document)
        )

    async def fetch_record_text(self, database: str, document: str) -> str | None:
        """The raw XML as text, deliberately *not* parsed.

        Exists so the parse can happen off the event loop. `ET.fromstring` is CPU
        work, and CPU work on a coroutine's thread stalls every other request
        sharing that loop -- a single record with large CLOBs would delay everyone
        else. The app fetches text here and parses it in a worker thread.
        """
        text = await self.query(fetch_record_query(), p=doc_path(database, document))
        return text.strip() or None

    async def existing_documents(self, database: str, module: str, ids: list[str]) -> set[str]:
        safe = _safe_ids(ids)
        if not safe:
            return set()
        text = await self.query(
            existing_documents_query(), db=database, module=module, ids=",".join(safe)
        )
        return {line.strip() for line in text.split("\n") if line.strip()}

    async def count_documents(self, database: str) -> int:
        return int((await self.query(count_documents_query(database))).strip() or 0)

    async def document_names(self, database: str) -> list[str]:
        return [
            n.strip()
            for n in (await self.query(document_names_query(database))).split("\n")
            if n.strip()
        ]
