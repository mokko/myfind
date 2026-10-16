"""The asynchronous transports, at the HTTP level.

The route tests replace both clients with fakes, which leaves the async
transports themselves unexercised -- `basex.py`'s async half and `solr.py`'s were
the two least-covered parts of the project. These drive them through
`httpx.MockTransport`, so they assert the **bytes and query parameters actually
sent** without needing Solr or BaseX running.

The most valuable test here is the drift check: the sync and async clients share
their request text, and that is the whole reason both exist. If they ever send
different bytes for the same call, this catches it -- and nothing else would,
because the fakes bypass the transports entirely.
"""

from __future__ import annotations

import httpx
import pytest

from myfind.basex import (
    PING_QUERY,
    AsyncBaseXClient,
    BaseXClient,
    BaseXError,
    build_body,
    count_documents_query,
    document_names_query,
    existing_documents_query,
    fetch_record_query,
    parse_nodes,
    parse_single,
)
from myfind.config import load
from myfind.solr import AsyncSolrClient, SolrClient, SolrError


@pytest.fixture
def anyio_backend():
    """Run these on asyncio. anyio ships both asyncio and trio backends."""
    return "asyncio"


def capture(body: str = "", status: int = 200, json_body: dict | None = None):
    """A MockTransport that records the request and replies with a canned body."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        seen["method"] = request.method
        if json_body is not None:
            return httpx.Response(status, json=json_body)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), seen


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# BaseX: the request
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_async_client_posts_the_documented_envelope():
    transport, seen = capture(body="1\n2\n3")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://basex/rest", user="u", password="p", client=http)
        out = await bx.query(PING_QUERY)

    assert out == "1\n2\n3"
    assert seen["method"] == "POST"
    assert seen["body"] == build_body(PING_QUERY, {})
    assert seen["url"] == "http://basex/rest"


@pytest.mark.anyio
async def test_the_async_client_declares_its_external_variables_in_the_prolog():
    """Missing `declare variable ... external` is `[XPST0008] Undeclared variable`.

    Asserted on the outgoing text because the failure only appears against a real
    BaseX -- the fakes never see a query.
    """
    transport, seen = capture(body="")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://basex/rest", user="u", password="p", client=http)
        await bx.existing_documents("sync_Object", "Object", ["1"])

    sent = seen["body"].decode()
    assert "declare variable $db external" in sent
    assert "declare variable $module external" in sent
    assert "declare variable $ids external" in sent
    assert "tokenize($ids, ',')" in sent


@pytest.mark.anyio
async def test_sync_and_async_send_identical_bytes_for_the_same_call():
    """The reason the request text is shared rather than written twice.

    Two implementations of one query drift, and the drift shows up as a different
    answer from the same URL depending on which transport served it.
    """
    bodies = {}
    for label, make in (
        ("sync", lambda t: BaseXClient(url="http://b/rest", user="u", password="p", client=t)),
        ("async", lambda t: AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=t)),
    ):
        transport, seen = capture(body="<a/>")
        if label == "sync":
            with httpx.Client(transport=transport) as http:
                make(http).fetch_record("sync_Object", "Object-447469.xml")
        else:
            async with httpx.AsyncClient(transport=transport) as http:
                await make(http).fetch_record("sync_Object", "Object-447469.xml")
        bodies[label] = seen["body"]

    assert bodies["sync"] == bodies["async"]
    assert b"Object-447469.xml" in bodies["sync"]


# ---------------------------------------------------------------------------
# BaseX: the answers
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_an_absent_document_is_none_not_an_error():
    """The doc-available guard means an empty reply, and empty means None."""
    transport, _ = capture(body="")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.fetch_record_text("sync_Object", "Object-1.xml") is None
        assert await bx.fetch_record("sync_Object", "Object-1.xml") is None


@pytest.mark.anyio
async def test_fetch_record_text_returns_raw_xml_without_parsing():
    """It must hand back text: the app parses it in a worker thread, off the loop."""
    transport, _ = capture(body="  <application><modules/></application>  ")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        raw = await bx.fetch_record_text("sync_Object", "Object-1.xml")
    assert raw == "<application><modules/></application>"


@pytest.mark.anyio
async def test_existing_documents_reads_the_newline_separated_answer():
    transport, _ = capture(body="636116\n4014992\n")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        found = await bx.existing_documents("sync_Multimedia", "Multimedia", ["1", "2"])
    assert found == {"636116", "4014992"}


@pytest.mark.anyio
async def test_non_numeric_ids_never_reach_the_query():
    """Ids come from a URL; only digits continue on to a path expression."""
    transport, seen = capture(body="")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.existing_documents("db", "M", ["7", "../etc/passwd", ""]) == set()
    assert b"passwd" not in seen["body"]


@pytest.mark.anyio
async def test_no_ids_means_no_request_at_all():
    transport, seen = capture(body="")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.existing_documents("db", "M", ["nope"]) == set()
    assert seen == {}  # not one round trip


@pytest.mark.anyio
async def test_count_and_names_read_their_scalar_answers():
    transport, seen = capture(body="47853")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.count_documents("sync_Object") == 47853
    assert count_documents_query("sync_Object") in seen["body"].decode()

    transport, seen = capture(body="Object-1.xml\nObject-2.xml\n")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.document_names("sync_Object") == ["Object-1.xml", "Object-2.xml"]
    assert document_names_query("sync_Object") in seen["body"].decode()


@pytest.mark.anyio
async def test_a_ping_that_errors_is_false_not_an_exception():
    """/healthz has to be able to report the outage, not fail with it."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        assert await bx.ping() is False


# ---------------------------------------------------------------------------
# BaseX: failures
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_an_unreachable_basex_becomes_a_basex_error():
    """The app answers 503 for this and a bare 500 for an httpx error."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        with pytest.raises(BaseXError, match="cannot reach BaseX"):
            await bx.query(PING_QUERY)


@pytest.mark.anyio
async def test_an_error_status_keeps_basexs_own_message():
    transport, _ = capture(body="[XPST0008] Undeclared variable: $db.", status=400)
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        with pytest.raises(BaseXError) as exc:
            await bx.query(PING_QUERY)
    assert "HTTP 400" in str(exc.value)
    assert "Undeclared variable" in str(exc.value)


@pytest.mark.anyio
async def test_a_response_that_is_not_xml_is_reported_as_such():
    transport, _ = capture(body="<unclosed>")
    async with httpx.AsyncClient(transport=transport) as http:
        bx = AsyncBaseXClient(url="http://b/rest", user="u", password="p", client=http)
        with pytest.raises(BaseXError, match="not XML"):
            await bx.query_xml(PING_QUERY)


def test_a_cdata_close_in_a_query_is_refused():
    """It would end the CDATA early and silently truncate the query."""
    with pytest.raises(ValueError, match="CDATA"):
        build_body("for $x in 1 ]]> return $x", {})


def test_parsing_helpers_are_shared_not_duplicated():
    """Both transports parse through these, so a REPLACEMENT here fixes both."""
    assert parse_single("") is None
    assert len(parse_nodes("<a/><b/>")) == 2
    assert parse_nodes("") == []
    # A stray XML declaration inside a wrapper is illegal; it is stripped.
    assert len(parse_nodes('<?xml version="1.0"?><a/>')) == 1


def test_the_query_constants_are_complete_sentences():
    """A fragment would need a prolog elsewhere; these carry their own declares."""
    assert "declare variable $p external" in fetch_record_query()
    assert "doc-available($p)" in fetch_record_query()
    assert "declare variable $ids external" in existing_documents_query()


# ---------------------------------------------------------------------------
# Solr: the request and the answers
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_search_sends_the_facet_exclusion_pair():
    """Asserted on the wire, not on a fake: this is the one-way-door bug's shape.

    Selecting a facet value must not collapse that facet to the value itself, so
    the filter is tagged and the facet names that tag.
    """
    transport, seen = capture(json_body={"response": {"numFound": 0, "docs": []}})
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        await solr.search(q="*:*", facets=["object_type"], filters=[("object_type", "Zeichnung")])

    assert seen["params"]["fq"] == '{!tag=object_type}object_type_facet:"Zeichnung"'
    assert seen["params"]["facet.field"] == "{!ex=object_type}object_type_facet"


@pytest.mark.anyio
async def test_async_search_returns_the_response_body():
    payload = {"response": {"numFound": 26, "docs": [{"id": "Object-1"}]}}
    transport, _ = capture(json_body=payload)
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        res = await solr.search(q="Watteau", module="Object")
    assert res["response"]["numFound"] == 26


@pytest.mark.anyio
async def test_an_unreachable_solr_becomes_a_solr_error():
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        with pytest.raises(SolrError, match="cannot reach Solr"):
            await solr.count()


@pytest.mark.anyio
async def test_an_unreachable_solr_reports_not_alive_rather_than_raising():
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        assert await solr.alive() is False


@pytest.mark.anyio
async def test_a_solr_error_status_keeps_the_message():
    transport, _ = capture(json_body={"error": {"msg": "undefined field x_facet"}}, status=400)
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        with pytest.raises(SolrError) as exc:
            await solr.search(q="*:*")
    assert "undefined field x_facet" in str(exc.value)


# ---------------------------------------------------------------------------
# Solr: writes and schema
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_index_sends_documents_and_returns_the_count():
    transport, seen = capture(json_body={"responseHeader": {"status": 0}})
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        docs = [{"id": "Object-1"}, {"id": "Object-2"}]
        assert await solr.index(docs) == 2
    assert b"Object-1" in seen["body"]
    assert "update" in seen["url"]


@pytest.mark.anyio
async def test_async_indexing_nothing_makes_no_request():
    transport, seen = capture(json_body={})
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        assert await solr.index([]) == 0
    assert seen == {}


@pytest.mark.anyio
async def test_async_delete_all_and_commit():
    transport, seen = capture(json_body={"responseHeader": {"status": 0}})
    async with httpx.AsyncClient(transport=transport) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        await solr.delete_all()
        assert b'"*:*"' in seen["body"]
        await solr.commit()
        assert "commit=true" in seen["url"]


@pytest.mark.anyio
async def test_async_schema_is_idempotent_over_the_async_transport(config):
    """`add-field` on an existing field is the normal case on a re-run."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="Field 'id' already exists.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        result = await solr.ensure_schema(config)

    assert result["created"] == 0
    assert result["existing"] == calls["n"] > 0


@pytest.mark.anyio
async def test_async_schema_propagates_an_unexpected_error(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="insufficient permissions")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        solr = AsyncSolrClient(url="http://solr/myfind", client=http)
        with pytest.raises(SolrError, match="insufficient permissions"):
            await solr.ensure_schema(config)


def test_both_clients_build_the_same_schema(config):
    """The schema is derived by a shared function, so the sync path -- the one the
    indexer uses -- and the async path cannot disagree about a field's type."""
    from myfind.solr import build_schema_fields

    fields = build_schema_fields(config)
    names = [f["name"] for f in fields]
    assert len(names) == len(set(names)), "a field declared twice"
    assert "object_number_sort" in names
    assert next(f for f in fields if f["name"] == "object_number_sort")["type"] == "string"


def test_the_sync_client_still_works_for_the_indexer(config):
    """The indexer is a CLI: it must keep using the synchronous client."""
    with SolrClient("http://solr/myfind") as solr:
        assert solr.url == "http://solr/myfind"
    assert load("myfind.toml").solr.url.endswith("/myfind")
