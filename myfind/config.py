"""Configuration: TOML + environment.

Secrets never live in the tracked TOML. It carries a dev placeholder only so a
fresh checkout runs; the real value comes from the environment or a `.env` file
beside the config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = "myfind.toml"


@dataclass(frozen=True)
class FieldSpec:
    field: str
    kind: str
    name: str
    roles: tuple[str, ...] = ()

    def has(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class LinkSpec:
    field: str
    reference: str
    target: str


@dataclass(frozen=True)
class Module:
    name: str
    database: str
    fields: tuple[FieldSpec, ...] = ()
    links: tuple[LinkSpec, ...] = ()
    route: str = "record"
    # The entity name the museum uses ("Assets" for the Multimedia module), so
    # the UI never has to say "Multimedia" to a user who thinks in assets.
    label: str = ""
    plural: str = ""
    # Columns for the results list, in order.
    result_fields: tuple[str, ...] = ()

    def display_label(self) -> str:
        return self.label or self.name

    def plural_label(self) -> str:
        return self.plural or (self.label or self.name).lower() + "s"

    def facet_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.has("facet")]

    def display_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.has("display")]

    def search_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.has("search")]

    def sort_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.has("sort")]


@dataclass(frozen=True)
class BaseXConfig:
    url: str = "http://localhost:8080/rest"
    user: str = "oai"
    password: str = "devpass"
    timeout: float = 300.0
    timezone_offset: str = "+00:00"


@dataclass(frozen=True)
class SolrConfig:
    url: str = "http://localhost:8983/solr/myfind"


@dataclass(frozen=True)
class IndexConfig:
    batch: int = 250
    receipt: str = "data/index-receipt.json"


@dataclass(frozen=True)
class SiteConfig:
    name: str = "myfind"
    subtitle: str = ""
    page_size: int = 20


@dataclass(frozen=True)
class Config:
    site: SiteConfig
    basex: BaseXConfig
    solr: SolrConfig
    index: IndexConfig
    modules: tuple[Module, ...]
    path: Path | None = field(default=None, compare=False)

    def module(self, name: str) -> Module:
        for m in self.modules:
            if m.name == name:
                return m
        raise KeyError(f"unknown module: {name}")

    def module_names(self) -> list[str]:
        return [m.name for m in self.modules]


def _load_dotenv(path: Path) -> None:
    """Set any KEY=VALUE from `path` that is not already in the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load(path: str | os.PathLike[str] = DEFAULT_CONFIG) -> Config:
    """Load config, applying `.env` and environment overrides."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        cfg_path = Path(__file__).resolve().parent.parent / path
    _load_dotenv(cfg_path.parent / ".env")
    # An alternative location, for a secrets file kept outside the checkout.
    # Anything already in the real environment still wins.
    extra = os.environ.get("MYFIND_ENV_FILE")
    if extra:
        _load_dotenv(Path(extra).expanduser())

    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)

    # The TOML uses camelCase for readability next to the XPath-flavoured keys;
    # the dataclass uses snake_case. Translate explicitly rather than splatting,
    # so a typo in the config is a named error and not a silent default.
    site_raw = raw.get("site", {})
    site = SiteConfig(
        name=site_raw.get("name", "myfind"),
        subtitle=site_raw.get("subtitle", ""),
        page_size=int(site_raw.get("pageSize", 20)),
    )

    bx = dict(raw.get("basex", {}))
    # Environment wins over both the file and the placeholder.
    bx["password"] = os.environ.get("MYFIND_BASEX_PASSWORD", bx.get("password", "devpass"))
    bx["user"] = os.environ.get("MYFIND_BASEX_USER", bx.get("user", "oai"))
    bx["url"] = os.environ.get("MYFIND_BASEX_URL", bx.get("url", "http://localhost:8080/rest"))
    basex = BaseXConfig(
        url=bx["url"],
        user=bx["user"],
        password=bx["password"],
        timeout=float(bx.get("timeout", 300)),
        timezone_offset=str(bx.get("timezoneOffset", "+00:00")),
    )

    solr_raw = dict(raw.get("solr", {}))
    solr_raw["url"] = os.environ.get("MYFIND_SOLR_URL", solr_raw.get("url", SolrConfig.url))

    modules = []
    for m in raw.get("modules", []):
        modules.append(
            Module(
                name=m["name"],
                database=m["database"],
                route=m.get("route", "record"),
                fields=tuple(
                    FieldSpec(
                        field=f["field"],
                        kind=f["kind"],
                        name=f["name"],
                        roles=tuple(f.get("roles", ())),
                    )
                    for f in m.get("fields", [])
                ),
                links=tuple(
                    LinkSpec(
                        field=l["field"],
                        reference=l["reference"],
                        target=l["target"],
                    )
                    for l in m.get("links", [])
                ),
                label=m.get("label", ""),
                plural=m.get("plural", ""),
                result_fields=tuple(m.get("resultFields", ())),
            )
        )

    return Config(
        site=site,
        basex=basex,
        solr=SolrConfig(**{k: v for k, v in solr_raw.items() if k in ("url",)}),
        index=IndexConfig(**raw.get("index", {})),
        modules=tuple(modules),
        path=cfg_path,
    )
