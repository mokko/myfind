"""BaseX REST client - synchronous, because Flask is.

The async/httpx pool in the sibling OAI-PMH provider project exists because
Starlette's event loop must not block. Flask is WSGI: requests are already
served one-per-thread, so a plain blocking client is the honest shape here and
an async client would only add an event loop to babysit.

Transport is BaseX's documented POST envelope:

    <query xmlns="http://basex.org/rest">
      <text><![CDATA[ ...xquery... ]]></text>
      <variable name="db" value="..."/>
    </query>

Values are escaped into the XML, so a stray & or < cannot corrupt the request.

Two rules this module exists to enforce, both learned on this corpus:

1. **Never build XQuery by f-string-ing a user string into it.** Every value
   from the caller goes through a bound external variable, so the only thing
   interpolated into query text is a name this module has already validated.
2. **A record is fetched by document name, not by searching for it.** The store
   is one document per record (`Object-447469.xml`), so `doc()` is an O(1) name
   lookup while `collection()[@id = ...]` is a full scan of 47,855 documents --
   measured at 10.3 s against 1.5 s on this box. Record pages and linked-record
   pages use the name; only the indexer reads whole collections.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import httpx

REST_NS = "http://basex.org/rest"

# A database name we are willing to interpolate into a query literal. BaseX
# database names are identifiers; a name carrying a space, quote or newline
# could end the literal early, so it is refused rather than escaped.
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
# A stored document name, as returned by db:list ("Object-447469.xml").
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _xq_string(value: str) -> str:
    """A single-quoted XQuery string literal (quote doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def _validated(name: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.fullmatch(name):
        raise ValueError(f"not a valid BaseX {what}: {name!r}")
    return name


def doc_name(module: str, record_id: str | int) -> str:
    """The stored document name for a record: ('Object', 447469) -> Object-447469.xml."""
    name = f"{module}-{record_id}.xml"
    return _validated(name, _DOC_NAME_RE, "document name")


def doc_path(database: str, document: str) -> str:
    """The path argument for fn:doc(): 'sync_Object/Object-447469.xml'."""
    return f"{_validated(database, _DB_NAME_RE, 'database name')}/{_validated(document, _DOC_NAME_RE, 'document name')}"


class BaseXError(Exception):
    """BaseX reported a failure."""


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

    # -- transport --------------------------------------------------------

    @staticmethod
    def _body(query: str, variables: dict[str, object]) -> bytes:
        parts = [
            f'<query xmlns="{REST_NS}">',
            f"<text><![CDATA[{query}]]></text>",
        ]
        for name, value in variables.items():
            # A CDATA close in the payload would end the query text early, and
            # values are XML-escaped below anyway -- but the escape happens
            # *after* a potential "]]>" has already been written, so it is
            # split here rather than silently truncating the query.
            parts.append(
                f"<variable name={quoteattr(str(name))} "
                f"value={quoteattr(escape(str(value)))}/>"
            )
        parts.append("</query>")
        return "".join(parts).encode("utf-8")

    def query(self, xquery: str, **variables: object) -> str:
        """Run a query, return the raw serialised output."""
        if "]]>" in xquery:
            raise ValueError("query text may not contain a CDATA close (]]>)")
        resp = self.client.post(self.url, content=self._body(xquery, variables))
        if resp.status_code >= 400:
            raise BaseXError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")
        return resp.text

    def query_xml(self, xquery: str, **variables: object) -> ET.Element | None:
        """Run a query whose result is a single well-formed document."""
        text = self.query(xquery, **variables).strip()
        if not text:
            return None
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc

    def query_nodes(self, xquery: str, **variables: object) -> list[ET.Element]:
        """Run a query returning a *sequence* of elements.

        A `for ... return <x/>` is N top-level elements, which is not a
        well-formed document and cannot be parsed as one. Wrapping is how the
        sequence is reclaimed.
        """
        text = self.query(xquery, **variables).strip()
        if not text:
            return []
        if text.startswith("<?xml"):
            text = text.split("?>", 1)[1].lstrip()
        try:
            return list(ET.fromstring(f"<wrap>{text}</wrap>"))
        except ET.ParseError as exc:
            raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc

    # -- operations -------------------------------------------------------

    def ping(self) -> bool:
        """True if the server answers a trivial expression.

        Note the port: BaseX 12 uses 8080 for REST. Older docs say 8984 and are
        wrong for this build.
        """
        try:
            return self.query("1 to 3").strip() == "1\n2\n3"
        except Exception:
            return False

    def fetch_record(self, database: str, document: str) -> ET.Element | None:
        """Fetch one stored record by document name. O(1) name lookup.

        Returns the root element (`application`), or None when the document is
        absent -- which is a normal answer here, not an error: the store holds
        three modules, and records reference ~14 others that were never
        ingested, including the owner on every object.

        **The `doc-available()` guard is load-bearing.** A bare
        `doc('sync_Object/Object-999999999.xml')` is not "no result" -- BaseX
        raises `[basex:doc] Database path ... yields no documents` and the REST
        layer answers HTTP 500. Without the guard, following one dangling link
        is a 500 rather than a graceful "not in this index", and a typo in a URL
        looks like a server fault.
        """
        path = doc_path(database, document)
        query = (
            "declare variable $p external;\n"
            "if (doc-available($p)) then doc($p) else ()"
        )
        return self.query_xml(query, p=path)

    def count_documents(self, database: str) -> int:
        text = self.query(f"count(collection({_xq_string(database)}))")
        return int(text.strip() or 0)

    def existing_documents(
        self, database: str, module: str, ids: list[str]
    ) -> set[str]:
        """Which of these record ids exist as stored documents.

        **One query, not one per id.** A record page with 20 linked records would
        otherwise be 20 round trips.

        Ids are filtered to digits before being sent: they come from a stored
        `moduleItemId` attribute, but they arrive here via a URL and there is no
        reason to interpolate anything but a number into a path expression.
        """
        safe = [str(i) for i in ids if str(i).isdigit()]
        if not safe:
            return set()
        query = (
            "declare variable $db external;\n"
            "declare variable $module external;\n"
            "declare variable $ids external;\n"
            "for $i in tokenize($ids, ',')\n"
            "where doc-available($db || '/' || $module || '-' || $i || '.xml')\n"
            "return $i"
        )
        text = self.query(
            query, db=database, module=module, ids=",".join(safe)
        )
        return {line.strip() for line in text.split("\n") if line.strip()}
