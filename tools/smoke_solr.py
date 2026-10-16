#!/usr/bin/env python3
"""End-to-end smoke test: BaseX -> map -> Solr -> query.

This is the indexer's first 20 records, not a fixture. It exists to answer one
question with evidence: can real MuseumPlus RIA records be pulled out of BaseX,
mapped, indexed and searched on this box?

    python3 tools/smoke_solr.py

Reads BaseX credentials from the environment, or from any `.env` at
MYFIND_ENV_FILE (see myfind/config.py):
    MYFIND_BASEX_USER / MYFIND_BASEX_PASSWORD
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from xml.etree import ElementTree as ET

BASEX = os.environ.get("MYFIND_BASEX_URL", "http://localhost:8080/rest")
SOLR = os.environ.get("MYFIND_SOLR_URL", "http://localhost:8983/solr/smoke")
DB = "sync_Object"
SAMPLE = 20

# The mapping, in one place. Each entry is (Solr field, element kind, name).
# Only fields verified present in the real export are listed.
FIELDS = [
    ("title", "virtualField", "ObjObjectTitleVrt"),
    ("object_number", "dataField", "ObjObjectNumberTxt"),
    ("object_number_sort", "dataField", "ObjObjectNumberSortedTxt"),
    ("object_type", "dataField", "ObjTechnicalTermClb"),
    ("date_display", "virtualField", "ObjDateVrt"),
    ("material_technique", "virtualField", "ObjMaterialTechniqueVrt"),
]


def _load_env() -> None:
    """Pick up the sibling project's .env so no secret is pasted into a command."""
    if os.environ.get("MYFIND_BASEX_PASSWORD"):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "..", ".env"),
        os.environ.get("MYFIND_ENV_FILE", ""),
    ):
        if not os.path.exists(cand):
            continue
        for line in open(cand, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "OAI_BASEX_PASSWORD":
                os.environ.setdefault("MYFIND_BASEX_PASSWORD", v)


def basex_query(query: str, **variables: str | int) -> str:
    """BaseX REST POST envelope. Values are escaped into the XML.

    External variables must be **declared in the query prolog** or BaseX answers
    `[XPST0008] Undeclared variable: $db`. They also arrive untyped, so an
    `xs:integer` declaration is emitted when the Python value is an int -- that
    is what makes `1 to $limit` work instead of failing XPTY0004.
    """
    from xml.sax.saxutils import escape, quoteattr

    prolog = []
    for name, value in variables.items():
        decl = f"declare variable ${name} external;"
        if isinstance(value, int):
            decl = f"declare variable ${name} as xs:integer external;"
        prolog.append(decl)
    query = "\n".join(prolog + [query])

    parts = ['<query xmlns="http://basex.org/rest">', f"<text><![CDATA[{query}]]></text>"]
    for name, value in variables.items():
        parts.append(
            f"<variable name={quoteattr(name)} value={quoteattr(escape(str(value)))}/>"
        )
    parts.append("</query>")
    body = "".join(parts).encode("utf-8")
    req = urllib.request.Request(
        BASEX,
        data=body,
        headers={"Content-Type": "application/query+xml; charset=utf-8"},
        method="POST",
    )
    user = os.environ.get("MYFIND_BASEX_USER", "oai")
    pw = os.environ.get("MYFIND_BASEX_PASSWORD", "")
    import base64

    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # BaseX puts the query error in the body. Losing it turns a one-line
        # fix into a debugging session, so it is always reported.
        detail = exc.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"BaseX HTTP {exc.code}: {detail[:600]}") from None


def map_record(doc: ET.Element) -> dict | None:
    """One stored RIA document -> one Solr document."""
    item = doc.find(".//moduleItem")
    if item is None:
        return None
    out: dict[str, object] = {"type": "Object", "id": item.get("id")}
    for field, kind, name in FIELDS:
        values = [
            (el.findtext("value") or "").strip()
            for el in item.findall(f"{kind}[@name='{name}']")
        ]
        values = [v for v in values if v]
        if not values:
            continue
        out[field] = values if len(values) > 1 else values[0]
    return out


def solr(path: str, payload: object | None = None, ctype: str = "application/json"):
    url = f"{SOLR}/{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": ctype}, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    _load_env()
    if not os.environ.get("MYFIND_BASEX_PASSWORD"):
        print("no BaseX password in env or <oai-provider>/.env", file=sys.stderr)
        return 2

    print(f"1. BaseX: pull {SAMPLE} documents named ...")
    names = [
        n
        for n in basex_query(f"db:list('{DB}')[1 to {SAMPLE}]").split("\n")
        if n.strip()
    ]
    print(f"   got {len(names)} names, first={names[0] if names else '-'}")

    print("2. BaseX: fetch them by document name in one query ...")
    # $limit is passed as an int so the client declares it xs:integer.
    xml = basex_query(
        "for $n in db:list($db)[1 to $limit] return doc($db || '/' || $n)",
        db=DB,
        limit=SAMPLE,
    )
    root = ET.fromstring(f"<batch>{xml}</batch>")
    records = [r for r in (map_record(d) for d in root) if r]
    print(f"   mapped {len(records)} records")
    print(f"   sample: {json.dumps(records[0], ensure_ascii=False)[:200]}")

    print("3. Solr: index ...")
    res = solr("update/json/docs?commit=true", records)
    print(f"   {res.get('responseHeader', {}).get('status')}")

    print("4. Solr: query for 'Zeichnung' in the object type ...")
    q = solr("select?q=object_type:Zeichnung&rows=3&fl=id,title,object_number")
    n = q["response"]["numFound"]
    print(f"   numFound={n}")
    for d in q["response"]["docs"][:3]:
        print(f"   - id={d.get('id')} no={d.get('object_number')} title={d.get('title')}")

    print("5. Solr: count all indexed ...")
    print(f"   {solr('select?q=*:*&rows=0')['response']['numFound']} documents in core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
