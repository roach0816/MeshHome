"""Bot app recipes: what a catalog app is, and the engine that runs one.

A bot app (Settings → Bot → Apps; catalog in app/services/bot_apps.py) is a JSON *recipe*, never
code. It declares:

- settings the owner fills in (e.g. a weather station's address, named groups of device IDs);
- sources: HTTP GET requests returning JSON, either to the local network (the address comes from a
  setting and must resolve to a private address) or to internet hosts the recipe names;
- optionally one login (format 3): a JSON POST to the same local device that returns a session
  token, which the sources using it send along;
- commands: which sources to read, values to pick out of the JSON with a small path language, and a
  reply template that is trimmed to fit one direct message.

Nothing in a recipe can run code, reach a host it didn't declare, follow redirects, or put a
setting (an address, a key) into a reply: templates only see values read from the response. The
one narrow exception is a group command's no_group / unknown_group reply, which may list the group
names the owner chose for use over the air (and echo the word the sender typed). The word a mesh
user sends after a command only ever selects one of those groups; it never reaches a request.
The format is documented in docs/bot-apps.md; SCHEMA is the version this MeshHome understands.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from typing import Annotated, Any, Literal, NamedTuple
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

# 2: push sources (a device sends its readings to MeshHome).
# 3: groups and select settings, several sources per command, Basic auth and raw header values,
#    self-signed local HTTPS, per-source size limits, a login, and more path selectors.
SCHEMA = 3
DM_MAX_BYTES = 150
MAX_RESPONSE_BYTES = 512 * 1024  # a source's default; max_kb raises it
MAX_LOGIN_BYTES = 64 * 1024
MAX_TIMEOUT = 10.0
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
RESERVED_COMMANDS = {"help", "info", "ping"}  # built into the bot
MAX_GROUPS, MAX_GROUP_ITEMS, GROUPS_MAX_CHARS = 10, 6, 4000
MAX_ARG = 20
MAX_OPTIONS = 1000  # select options in one recipe

_ID = r"^[a-z0-9][a-z0-9-]{1,31}$"
_KEY = r"^[a-z][a-z0-9_]{0,31}$"
_NUM_UNIT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([^\d\s].*)?$")
_NAME = r"[A-Za-z0-9_\-]+"
_SEGMENT_V2 = re.compile(r"^([A-Za-z0-9_\-]+)?(?:\[(\d+|[A-Za-z0-9_\-]+=[^\]]*)\])?$")
_SEGMENT = re.compile(rf"^({_NAME})?(?:\[([^\]]*)\])?$")
_SELECTOR = re.compile(
    rf"^(?:(-?\d+)|(last:)?(?:has:({_NAME}(?:\.{_NAME})*)|({_NAME}(?:\.{_NAME})*)=(.*)))$", re.S
)
_REF = re.compile(r"\{([a-z][a-z0-9_]*)(?:\.(unit|label))?\}")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_PLACEHOLDER_V3 = re.compile(r"\{(login\.token|[a-z][a-z0-9_]*)\}")
_TOKEN = "{login.token}"
_LOCAL_URL = re.compile(r"^https?://\{([a-z][a-z0-9_]*)\}(?:[/?]|$)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GROUP_NAME = re.compile(r"^[a-z0-9]{1,20}$")
_GROUP_ITEM = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
DEFAULT_NO_GROUP = "Groups: {groups}"
DEFAULT_UNKNOWN_GROUP = "No group {arg}. Groups: {groups}"
LOCAL_DEVICE = "contacts a device on your local network"
SELF_SIGNED = " (accepts its self-signed certificate)"
SIGNS_IN = "signs in to the device with your password"


class RecipeError(Exception):
    """A recipe can't be used (invalid, or asks for something not allowed). Safe to show the owner."""


class SourceError(Exception):
    """A source couldn't be read. The message is for the owner (Settings), never sent over the air."""


class _Refused(SourceError):
    """The device answered 401 or 403 (a login may have expired)."""


# ---- the recipe format -------------------------------------------------------------------------


class Option(BaseModel):
    # Inserted into URLs as written: the pattern leaves out ? # @ % and spaces.
    value: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,59}$")
    label: str = Field(min_length=1, max_length=60)


OptionList = Annotated[list[Option], Field(min_length=1, max_length=60)]


class Setting(BaseModel):
    key: str = Field(pattern=_KEY)
    label: str = Field(min_length=1, max_length=60)
    # host: an IP address or hostname, optionally :port. secret: never shown again once saved.
    # groups (format 3): "name: id, id" lines, read by a command's `group`.
    # select (format 3): one of `options`, or of `options_by[<the depends_on setting's value>]`.
    type: Literal["host", "text", "secret", "groups", "select"] = "text"
    required: bool = True
    placeholder: str = Field(default="", max_length=80)
    help: str = Field(default="", max_length=400)
    options: OptionList | None = None
    depends_on: str | None = None
    options_by: Annotated[dict[str, OptionList], Field(max_length=30)] | None = None

    @model_validator(mode="after")
    def _select(self) -> Setting:
        listed, dependent = (
            self.options is not None,
            self.depends_on is not None or self.options_by is not None,
        )
        if self.type != "select":
            if listed or dependent:
                raise ValueError(f"Setting {self.key}: only a select has options")
            return self
        if listed == dependent or (dependent and (self.depends_on is None or self.options_by is None)):
            raise ValueError(
                f"Setting {self.key}: a select has either options, or depends_on with options_by"
            )
        for opts in [self.options or [], *(self.options_by or {}).values()]:
            if len({o.value for o in opts}) != len(opts):
                raise ValueError(f"Setting {self.key}: option values must be unique")
        return self

    def option_count(self) -> int:
        return len(self.options or []) + sum(len(v) for v in (self.options_by or {}).values())


class BasicAuth(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=200)


class Auth(BaseModel):
    basic: BasicAuth


_REQUEST_FIELDS = {"url", "headers", "timeout", "cache_seconds", "auth", "verify_tls", "max_kb", "login"}
_PUSH_FIELDS = {"format", "max_age", "help"}
_V3_SOURCE_FIELDS = {"auth", "verify_tls", "max_kb", "login"}


class Source(BaseModel):
    # local: MeshHome requests the URL; its host comes from a setting and must be on the local network.
    # internet: MeshHome requests the URL; its host must be one of the recipe's `hosts`.
    # push (schema 2): MeshHome requests nothing. A device on the local network sends readings to
    #   this app's upload address (/push/<token>), and commands read the latest one.
    network: Literal["local", "internet", "push"]
    url: str = Field(default="", max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=6.0, gt=0, le=MAX_TIMEOUT)
    cache_seconds: int = Field(default=60, ge=0, le=3600)
    # Format 3. auth: Basic credentials from settings. verify_tls: false accepts a local device's
    # self-signed certificate. max_kb: the largest response read. login: the recipe login whose
    # session token ({login.token}) this source sends.
    auth: Auth | None = None
    verify_tls: bool = True
    max_kb: int = Field(default=MAX_RESPONSE_BYTES // 1024, ge=1, le=2048)
    login: str | None = None
    # push only. query: a GET query string or a form body, as a flat object of strings; json: a JSON
    # body. max_age: seconds a reading counts as current. help: where the setting is on the device.
    format: Literal["query", "json"] = "query"
    max_age: int = Field(default=300, ge=10, le=86400)
    help: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _fields_for_network(self) -> Source:
        given = self.model_fields_set
        if self.network == "push":
            if wrong := given & _REQUEST_FIELDS:
                raise ValueError(f"A push source can't set {', '.join(sorted(wrong))}")
        else:
            if wrong := given & _PUSH_FIELDS:
                raise ValueError(f"Only a push source can set {', '.join(sorted(wrong))}")
            if not re.match(r"^https?://", self.url):
                raise ValueError("A source URL starts with http:// or https://")
            if self.network != "local" and not self.verify_tls:
                raise ValueError("Only a local source can turn off verify_tls")
            if self.network != "local" and self.login:
                raise ValueError("Only a local source can use a login")
            if self.auth and any(k.lower() == "authorization" for k in self.headers):
                raise ValueError("A source can't set both auth and an Authorization header")
        return self


class Login(BaseModel):
    """A JSON POST to the device that returns a session token (format 3; e.g. Pi-hole v6)."""

    url: str = Field(max_length=500)
    body: dict[str, str] = Field(default_factory=dict, max_length=10)
    token_from: str = Field(max_length=200)
    ttl_from: str = Field(default="", max_length=200)
    ttl_max: int = Field(default=1800, ge=60, le=86400)
    timeout: float = Field(default=6.0, gt=0, le=MAX_TIMEOUT)
    verify_tls: bool = True


class Candidate(BaseModel):
    path: str = Field(max_length=200)
    label: str = Field(default="", max_length=40)


class Value(BaseModel):
    # Paths tried in order; the first that gives a usable value wins (its label is {name.label}).
    from_: list[str | Candidate] = Field(alias="from", min_length=1, max_length=10)
    # number: "3.40mph" → 3.4 with unit "mph"; compass: degrees → "NE"; text: as sent (≤ 60 chars);
    # count (format 3): how many items the list at the path has (or match its last filter);
    # duration (format 3): seconds → "1h23m".
    format: Literal["number", "compass", "text", "count", "duration"] = "number"
    # Where the unit comes from when it isn't part of the value, and how to show it.
    unit_from: list[str] = Field(default_factory=list, max_length=5)
    unit_map: dict[str, str] = Field(default_factory=dict)
    # Format 3. source: which of the command's sources to read (default: its first). map: text
    # values to show differently, matched ignoring case ("ON" → "open"); "" leaves the value out.
    source: str | None = None
    map: dict[str, Annotated[str, Field(max_length=60)]] = Field(default_factory=dict, max_length=40)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _map_is_for_text(self) -> Value:
        if self.map and self.format != "text":
            raise ValueError("map works with format text")
        return self

    def candidates(self) -> list[Candidate]:
        return [c if isinstance(c, Candidate) else Candidate(path=c) for c in self.from_]

    def paths(self) -> list[str]:
        return [c.path for c in self.candidates()] + self.unit_from


class Part(BaseModel):
    # Each item is a template; items are joined with `item_join`, parts with `join`.
    items: list[str] = Field(min_length=1, max_length=12)
    # end: when the reply is too long, items are dropped from the end of this part (then the part).
    drop: Literal["none", "end"] = "none"


class Reply(BaseModel):
    parts: list[Part] = Field(default_factory=list, max_length=12)
    join: str = Field(default=" · ", max_length=5)
    item_join: str = Field(default=" ", max_length=5)
    max_bytes: int = Field(default=DM_MAX_BYTES, ge=20, le=DM_MAX_BYTES)
    empty: str = Field(default="No data right now.", max_length=140)
    unavailable: str = Field(default="Not available right now.", max_length=140)
    # Group commands (format 3): `each` is worded once per item and joined with `join`, dropping
    # from the end to fit. no_group / unknown_group may use {groups} and {arg}, and nothing else.
    each: str | None = Field(default=None, max_length=200)
    no_group: str | None = Field(default=None, max_length=140)
    unknown_group: str | None = Field(default=None, max_length=140)


class Command(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9]{1,19}$")
    help: str = Field(min_length=1, max_length=40)
    source: str | None = None
    sources: list[str] = Field(default_factory=list, max_length=3)  # format 3, instead of source
    group: str | None = None  # format 3: a groups setting; "/<name> <group>" reads each of its IDs
    values: dict[str, Value] = Field(max_length=40)
    reply: Reply

    @field_validator("name")
    @classmethod
    def _not_builtin(cls, v: str) -> str:
        if v in RESERVED_COMMANDS:
            raise ValueError(f"/{v} is built into the bot")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Command:
        if bool(self.source) == bool(self.sources):
            raise ValueError(f"/{self.name} reads either one source or a list of sources")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError(f"/{self.name} lists a source twice")
        r = self.reply
        if self.group:
            if self.sources:
                raise ValueError(f"/{self.name}: a group command reads one source")
            if r.each is None or r.parts:
                raise ValueError(f"/{self.name}: a group command words its reply with each, not parts")
        else:
            if r.each is not None or r.no_group is not None or r.unknown_group is not None:
                raise ValueError(f"/{self.name}: each, no_group and unknown_group are for group commands")
            if not r.parts:
                raise ValueError(f"/{self.name}'s reply has no parts")
        return self

    def source_names(self) -> list[str]:
        return [self.source] if self.source else list(self.sources)


def _path_ok_v2(path: str) -> bool:
    """Whether a path is valid in formats 1 and 2 (no dotted filters, last:, has:, or [-n])."""
    return all(seg and (m := _SEGMENT_V2.match(seg)) and any(m.groups()) for seg in path.split("."))


def _path_ok(path: str) -> bool:
    try:
        parse_path(path)
        return True
    except ValueError:
        return False


class Recipe(BaseModel):
    schema_: int = Field(alias="schema")
    id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, max_length=60)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    summary: str = Field(min_length=1, max_length=200)
    # Groups apps in the catalog ("Weather", "Home automation", …). Optional: "Other" when missing.
    category: str = Field(
        default="Other", min_length=1, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9 &/+-]*$"
    )
    description: str = Field(default="", max_length=2000)
    author: str = Field(default="", max_length=80)
    homepage: str = Field(default="", max_length=200)
    hosts: list[str] = Field(default_factory=list, max_length=10)  # internet hosts it may contact
    settings: list[Setting] = Field(default_factory=list, max_length=10)
    sources: dict[str, Source] = Field(min_length=1, max_length=5)
    logins: dict[str, Login] = Field(default_factory=dict)  # format 3: at most one
    commands: list[Command] = Field(min_length=1, max_length=10)

    model_config = {"populate_by_name": True}

    def _v3_feature(self) -> str | None:
        """The first thing this recipe uses that needs format 3, if any."""
        for s in self.settings:
            if s.type in ("groups", "select"):
                return f"Setting {s.key} (type {s.type})"
        for name, src in self.sources.items():
            if used := src.model_fields_set & _V3_SOURCE_FIELDS:
                return f"Source {name}'s {sorted(used)[0]}"
        if self.logins:
            return "A login"
        for cmd in self.commands:
            used = (cmd.model_fields_set & {"group", "sources"}) | (
                cmd.reply.model_fields_set & {"each", "no_group", "unknown_group"}
            )
            if used:
                return f"/{cmd.name}'s {sorted(used)[0]}"
            for key, v in cmd.values.items():
                if v.source or v.map or v.format in ("count", "duration"):
                    return f"/{cmd.name}'s value {key}"
                for path in v.paths():
                    if not _path_ok_v2(path) and _path_ok(path):
                        return f"The path {path}"
        return None

    @model_validator(mode="after")
    def _consistent(self) -> Recipe:
        if self.schema_ > SCHEMA:
            raise ValueError(f"This app needs a newer MeshHome (recipe format {self.schema_})")
        v3 = self.schema_ >= 3
        if not v3 and (feature := self._v3_feature()):
            raise ValueError(f'{feature} needs recipe format 3 ("schema": 3)')
        settings = {s.key: s for s in self.settings}
        if len(settings) != len(self.settings):
            raise ValueError("Setting keys must be unique")
        if v3 and "item" in settings:
            raise ValueError('"item" is reserved: use another setting key')
        for i, s in enumerate(self.settings):
            if s.depends_on is None:
                continue
            parent = next((p for p in self.settings[:i] if p.key == s.depends_on), None)
            if parent is None or parent.type != "select" or parent.options is None:
                raise ValueError(f"Setting {s.key}: depends_on must name an earlier select with options")
            if set(s.options_by or {}) != {o.value for o in parent.options}:
                raise ValueError(f"Setting {s.key}: options_by needs one list for each {parent.key} option")
        if sum(s.option_count() for s in self.settings) > MAX_OPTIONS:
            raise ValueError(f"An app can have at most {MAX_OPTIONS} options")
        names = [c.name for c in self.commands]
        if len(set(names)) != len(names):
            raise ValueError("Command names must be unique")
        pushes = [name for name, src in self.sources.items() if src.network == "push"]
        if pushes and self.schema_ < 2:
            raise ValueError('Push sources need recipe format 2 ("schema": 2)')
        if len(pushes) > 1:
            raise ValueError("An app can have one push source")
        if len(self.logins) > 1:
            raise ValueError("An app can have one login")
        group_sources = {c.source for c in self.commands if c.group}
        other_sources = {n for c in self.commands if not c.group for n in c.source_names()}

        def placeholders(what: str, templates: list[str], *, item: bool = False, token: bool = False) -> None:
            pattern = _PLACEHOLDER_V3 if v3 else _PLACEHOLDER
            used = {p for t in templates for p in pattern.findall(t)}
            if v3 and "item" in used:
                if not item:
                    raise ValueError(f"{what} uses {{item}}, which only a source read by group commands can")
                used.discard("item")
            if "login.token" in used:
                if not token:
                    raise ValueError(f"{what} uses {_TOKEN}, which only a source with a login can")
                used.discard("login.token")
            if unknown := used - settings.keys():
                raise ValueError(f"{what} uses unknown settings: {', '.join(sorted(unknown))}")
            if grouped := sorted(k for k in used if settings[k].type == "groups"):
                raise ValueError(f"{what} can't use {grouped[0]}: a command's group reads it")

        def local_host(what: str, url: str) -> str:
            m = _LOCAL_URL.match(url)
            if not m or m.group(1) not in settings or settings[m.group(1)].type != "host":
                raise ValueError(f"{what}: a local source's host must be a host setting")
            return m.group(1)

        for name, src in self.sources.items():
            if src.network == "push":
                continue
            templates = [src.url, *src.headers.values()]
            if src.auth:
                templates += [src.auth.basic.username, src.auth.basic.password]
            placeholders(
                f"Source {name}",
                templates,
                item=name in group_sources and name not in other_sources,
                token=bool(src.login),
            )
            host = urlsplit(src.url).hostname or ""
            if src.network == "local":
                key = local_host(f"Source {name}", src.url)
                if src.login:
                    login = self.logins.get(src.login)
                    if login is None:
                        raise ValueError(f"Source {name} uses an unknown login {src.login}")
                    if _LOCAL_URL.match(login.url) is None or _LOCAL_URL.match(login.url).group(1) != key:
                        raise ValueError(f"Login {src.login} must use the same host setting as source {name}")
            elif host not in self.hosts:
                raise ValueError(f"Source {name} contacts {host or 'an unknown host'}, which isn't in hosts")
        for name, login in self.logins.items():
            if not any(s.login == name for s in self.sources.values()):
                raise ValueError(f"Login {name} isn't used by any source")
            local_host(f"Login {name}", login.url)
            placeholders(f"Login {name}", [login.url, *login.body.values()])
            for path in (login.token_from, login.ttl_from):
                if path:
                    parse_path(path)
        for cmd in self.commands:
            for src_name in cmd.source_names():
                if src_name not in self.sources:
                    raise ValueError(f"/{cmd.name} reads an unknown source {src_name}")
            if cmd.group and (cmd.group not in settings or settings[cmd.group].type != "groups"):
                raise ValueError(f"/{cmd.name}'s group must name a groups setting")
            for key, v in cmd.values.items():
                if v.source is not None and v.source not in cmd.source_names():
                    raise ValueError(f"/{cmd.name}'s value {key} reads a source the command doesn't")
                for path in v.paths():
                    parse_path(path)
            r = cmd.reply
            for template in [i for part in r.parts for i in part.items] + ([r.each] if r.each else []):
                for ref, _ in _REF.findall(template):
                    if ref not in cmd.values:
                        raise ValueError(f"/{cmd.name}'s reply uses an unknown value {{{ref}}}")
            for template in (r.no_group, r.unknown_group):
                for ref, sub in _REF.findall(template or ""):
                    if ref not in ("groups", "arg") or sub:
                        raise ValueError(
                            f"/{cmd.name}'s no_group and unknown_group can only use {{groups}} and {{arg}}"
                        )
        return self

    def command(self, name: str) -> Command | None:
        return next((c for c in self.commands if c.name == name), None)

    def contacts(self) -> list[str]:
        """What it talks to, in the owner's words, e.g. "contacts api.example.com"."""
        out = []
        local = [s for s in self.sources.values() if s.network == "local"]
        if local:
            insecure = any(not s.verify_tls for s in local) or any(
                not lg.verify_tls for lg in self.logins.values()
            )
            out.append(LOCAL_DEVICE + (SELF_SIGNED if insecure else ""))
        out += [f"contacts {h}" for h in self.hosts]
        if self.logins:
            out.append(SIGNS_IN)
        if self.push_source():
            out.append("receives data from a device on your local network")
        return out

    def push_source(self) -> tuple[str, Source] | None:
        return next(((n, s) for n, s in self.sources.items() if s.network == "push"), None)


def load(raw: bytes | str | dict) -> Recipe:
    """Parse and check a recipe. RecipeError (with a readable reason) if it can't be used."""
    try:
        data = json.loads(raw) if isinstance(raw, bytes | str) else raw
        return Recipe.model_validate(data)
    except (ValueError, TypeError) as exc:
        detail = exc.errors()[0]["msg"] if hasattr(exc, "errors") else str(exc)
        raise RecipeError(f"Invalid app: {detail}") from None


# ---- settings ------------------------------------------------------------------------------------


def parse_groups(text: str) -> dict[str, list[str]]:
    """A groups setting, "garage: cover.left, cover.right" per line → {"garage": [...]}, in order.
    Names are lowercased. ValueError with a reason."""
    groups: dict[str, list[str]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        name, colon, rest = line.partition(":")
        name = name.strip().lower()
        if not colon or not _GROUP_NAME.match(name):
            raise ValueError(f"line {n}: start with a name (letters and digits, up to 20) and a colon")
        if name in groups:
            raise ValueError(f"the group {name} appears twice")
        items = [i.strip() for i in rest.split(",") if i.strip()]
        if not items:
            raise ValueError(f"the group {name} has no IDs")
        if len(items) > MAX_GROUP_ITEMS:
            raise ValueError(f"the group {name} has more than {MAX_GROUP_ITEMS} IDs")
        if bad := next((i for i in items if not _GROUP_ITEM.match(i)), None):
            raise ValueError(f"{bad[:40]} isn't an ID (letters, digits and _ . : - only)")
        groups[name] = items
    if len(groups) > MAX_GROUPS:
        raise ValueError(f"at most {MAX_GROUPS} groups")
    return groups


def _format_groups(groups: dict[str, list[str]]) -> str:
    return "\n".join(f"{name}: {', '.join(items)}" for name, items in groups.items())


def _options(s: Setting, chosen: dict[str, str]) -> list[str]:
    """The values a select may have, given the settings chosen before it."""
    if s.options is not None:
        return [o.value for o in s.options]
    return [o.value for o in (s.options_by or {}).get(chosen.get(s.depends_on or "", ""), [])]


def check_settings(recipe: Recipe, values: dict[str, str], *, stale_ok: bool = False) -> dict[str, str]:
    """The settings to store: known keys only, trimmed and validated. ValueError with a reason.

    stale_ok: a select whose value is no longer one of its options is left unset instead of
    refused (an update changed its options; the app then needs settings, see `problem`)."""
    out: dict[str, str] = {}
    for s in recipe.settings:
        v = str(values.get(s.key) or "").strip()
        if s.type == "select":
            if v and v in _options(s, out):
                out[s.key] = v
            elif (v or s.required) and not stale_ok:
                raise ValueError(f"{s.label}: choose one")
            continue
        if not v:
            if s.required:
                raise ValueError(f"{s.label} is required")
            continue
        if len(v) > (GROUPS_MAX_CHARS if s.type == "groups" else 253):
            raise ValueError(f"{s.label} is too long")
        if s.type == "groups":
            try:
                v = _format_groups(parse_groups(v))
            except ValueError as exc:
                raise ValueError(f"{s.label}: {exc}") from None
        elif recipe.schema_ >= 3 and _CONTROL.search(v):
            raise ValueError(f"{s.label} can't contain control characters or line breaks")
        if s.type == "host" and (any(c.isspace() for c in v) or any(c in v for c in "/@?#")):
            raise ValueError(f"{s.label}: enter only an IP address or hostname (optionally :port)")
        out[s.key] = v
    return out


def problem(recipe: Recipe, settings: dict[str, str]) -> str | None:
    """Why saved settings can't be used as they are (e.g. after an update), or None."""
    try:
        check_settings(recipe, settings)
    except ValueError as exc:
        return str(exc)
    return None


def argument(text: str) -> str | None:
    """What a command's argument is: the first word, lowercased, at most MAX_ARG characters."""
    words = (text or "").split(None, 1)
    return words[0].lower()[:MAX_ARG] if words else None


# ---- reading the source --------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}
# Login sessions: key → (expires, monotonic; token). Tokens are never logged or put in replies.
_sessions: dict[str, tuple[float, str]] = {}
_session_locks: dict[str, asyncio.Lock] = {}


def _resolve(host: str) -> list[str]:
    """IP addresses of a host (tests replace this)."""
    return list({ai[4][0] for ai in socket.getaddrinfo(host, None)})


async def _local_only(host: str) -> None:
    try:
        addrs = [host] if _is_ip(host) else await asyncio.to_thread(_resolve, host)
    except OSError:
        raise SourceError(f"Couldn't find {host} on the network") from None
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise SourceError(f"{host} isn't on the local network ({a})")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _fill(
    template: str,
    settings: dict[str, str],
    recipe: Recipe,
    *,
    to: Literal["url", "header", "body"],
    item: str | None = None,
    token: str | None = None,
) -> str:
    """Put settings into a template. URLs: URL-encoded, except a host or select (fixed by the
    recipe) as written. Headers: as written from format 3 (URL-encoded before), refusing control
    characters. A login body: as written (it's sent as JSON)."""
    types = {s.key: s.type for s in recipe.settings}
    v3 = recipe.schema_ >= 3

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if v3 and name == "item":
            return quote(item or "", safe="")
        v = (token or "") if name == "login.token" else settings.get(name, "")
        if to == "body" or (to == "header" and v3):
            if to == "header" and _CONTROL.search(v):
                raise SourceError("A setting has a control character, which can't go in a header")
            return v
        if to == "url" and types.get(name) in ("host", "select"):
            return v
        return quote(v, safe="")

    return (_PLACEHOLDER_V3 if v3 else _PLACEHOLDER).sub(sub, template)


def _check_selects(recipe: Recipe, settings: dict[str, str]) -> None:
    """Selects go into URLs as written, so only ever one of their options."""
    for s in recipe.settings:
        if s.type != "select":
            continue
        v = settings.get(s.key)
        if (v and v not in _options(s, settings)) or (not v and s.required):
            raise SourceError(f"{s.label}: choose one (the app's settings need updating)")


def decode_upload(src: Source, query: str, body: bytes, content_type: str) -> Any:
    """What a push source received, as data for its commands. ValueError if it can't be read.

    query: `query` is the request's query string (already merged with anything a device appended to
    the path); a form body is added to it. Keys that repeat keep their last value."""
    if src.format == "json":
        try:
            data = json.loads(body)
        except ValueError:
            raise ValueError("Expected a JSON body") from None
        if not isinstance(data, dict | list):
            raise ValueError("Expected a JSON object or array")
        return data
    pairs = parse_qsl(query, keep_blank_values=True)
    if body and "json" not in content_type:
        pairs += parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    out: dict[str, str] = {}
    for key, value in pairs:
        key = key.lstrip("?&/").strip()
        if key:
            out[key] = value
    if not out:
        raise ValueError("No readings in the upload")
    return out


async def _request(
    method: str,
    url: str,
    *,
    label: str,
    timeout: float,
    verify: bool,
    limit: int,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
    body: dict[str, str] | None = None,
) -> Any:
    """One request, never following redirects; the JSON it returned."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=verify) as client:
            async with client.stream(
                method, url, headers={"Accept": "application/json", **(headers or {})}, auth=auth, json=body
            ) as res:
                if res.status_code in (401, 403):
                    raise _Refused(f"{label} answered with HTTP {res.status_code}")
                if res.status_code != 200 and not (method == "POST" and 200 <= res.status_code < 300):
                    raise SourceError(f"{label} answered with HTTP {res.status_code}")
                raw = b""
                async for chunk in res.aiter_bytes():
                    raw += chunk
                    if len(raw) > limit:
                        raise SourceError(f"{label} sent too much data")
        return json.loads(raw)
    except httpx.TimeoutException:
        raise SourceError(f"{label} didn't answer in time") from None
    except (httpx.HTTPError, ValueError):
        raise SourceError(f"Couldn't read {label}") from None


async def _session(recipe: Recipe, name: str, settings: dict[str, str], *, stale: str | None) -> str:
    """A session token for the recipe's login, reused until it is about to expire. stale: a token
    the device just refused; a new one is fetched unless another request already did."""
    login = recipe.logins[name]
    url = _fill(login.url, settings, recipe, to="url")
    body = {k: _fill(v, settings, recipe, to="body") for k, v in login.body.items()}
    # Per device and credentials, so a token is never sent to a different device.
    key = hashlib.sha256(json.dumps([recipe.id, name, url, body]).encode()).hexdigest()
    async with _session_locks.setdefault(key, asyncio.Lock()):
        hit = _sessions.get(key)
        if hit and time.monotonic() < hit[0] and hit[1] != stale:
            return hit[1]
        _sessions.pop(key, None)
        await _local_only(urlsplit(url).hostname or "")
        try:
            data = await _request(
                "POST", url, label="the device", timeout=login.timeout, verify=login.verify_tls,
                limit=MAX_LOGIN_BYTES, body=body,
            )  # fmt: skip
        except _Refused:
            raise SourceError("The device didn't accept the password") from None
        token = get_path(data, login.token_from)
        if token is None or isinstance(token, dict | list | bool) or not str(token).strip():
            raise SourceError("The device's sign-in reply had no session")
        token = str(token).strip()
        if _CONTROL.search(token) or len(token) > 1000:
            raise SourceError("The device's session can't be used")
        ttl = login.ttl_max
        if login.ttl_from:
            given = get_path(data, login.ttl_from)
            if isinstance(given, int | float) and not isinstance(given, bool):
                ttl = min(given, ttl)
        if ttl - 60 > 0:
            _sessions[key] = (time.monotonic() + ttl - 60, token)
        return token


async def fetch(
    recipe: Recipe,
    source_name: str,
    settings: dict[str, str],
    *,
    use_cache: bool = True,
    item: str | None = None,
) -> Any:
    src = recipe.sources[source_name]
    if src.network == "push":
        from app.services import push_inbox  # the store the upload route fills

        data = push_inbox.latest(recipe.id, src.max_age)
        if data is None:
            if push_inbox.received_at(recipe.id) is None:
                raise SourceError("Nothing has been received from the device yet")
            raise SourceError(f"Nothing received from the device in the last {src.max_age} seconds")
        return data
    _check_selects(recipe, settings)
    url = _fill(src.url, settings, recipe, to="url", item=item)  # without the session token
    host = urlsplit(url).hostname or ""
    if src.network == "local":
        await _local_only(host)
    elif host not in recipe.hosts:  # a setting can't move it elsewhere
        raise SourceError(f"{host} isn't one of this app's hosts")
    key = f"{recipe.id}:{url}"
    hit = _cache.get(key)
    if use_cache and hit and time.monotonic() - hit[0] < src.cache_seconds:
        return hit[1]
    auth = None
    if src.auth:
        auth = httpx.BasicAuth(
            _fill(src.auth.basic.username, settings, recipe, to="header"),
            _fill(src.auth.basic.password, settings, recipe, to="header"),
        )
    label = host if src.network == "internet" else "the device"
    token = None
    for attempt in (1, 2):
        if src.login:
            token = await _session(recipe, src.login, settings, stale=token)
        headers = {
            k: _fill(v, settings, recipe, to="header", item=item, token=token) for k, v in src.headers.items()
        }
        try:
            data = await _request(
                "GET",
                _fill(src.url, settings, recipe, to="url", item=item, token=token),
                label=label,
                timeout=src.timeout,
                verify=src.verify_tls,
                limit=src.max_kb * 1024,
                headers=headers,
                auth=auth,
            )
            break
        except _Refused:
            if not src.login or attempt == 2:  # a login: sign in again once, and retry once
                raise
    if src.cache_seconds:
        _cache[key] = (time.monotonic(), data)
    else:
        _cache.pop(key, None)
    return data


# ---- picking values out of the response -----------------------------------------------------------


class Selector(NamedTuple):
    """[n] (negative from the end), [field=value], [has:field], either with last: in front."""

    index: int | None = None
    field: tuple[str, ...] = ()
    want: str | None = None  # None: has:field
    last: bool = False


def _split_path(path: str) -> list[str]:
    """Split on dots outside [ ]."""
    out, cur, inside = [], "", False
    for ch in path:
        if ch == "." and not inside:
            out.append(cur)
            cur = ""
            continue
        inside = (inside or ch == "[") and ch != "]"
        cur += ch
    return [*out, cur]


def parse_path(path: str) -> list[tuple[str | None, Selector | None]]:
    """ "common_list[id=0x02].val" → [("common_list", Selector(field=("id",), want="0x02")), ("val", None)]."""
    out = []
    for seg in _split_path(path):
        m = _SEGMENT.match(seg)
        if not seg or not m or (m.group(1) is None and m.group(2) is None):
            raise ValueError(f"Bad path: {path}")
        sel = None
        if m.group(2) is not None:
            s = _SELECTOR.match(m.group(2))
            if not s:
                raise ValueError(f"Bad path: {path}")
            index, last, has, field, want = s.groups()
            if index is not None:
                sel = Selector(index=int(index))
            else:
                sel = Selector(field=tuple((has or field).split(".")), want=want, last=bool(last))
        out.append((m.group(1), sel))
    return out


_MISSING = object()


def _walk(data: Any, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        elif isinstance(data, list) and k.isdigit() and int(k) < len(data):
            data = data[int(k)]
        else:
            return _MISSING
    return data


def _matches(x: Any, sel: Selector) -> bool:
    if not isinstance(x, dict):
        return False
    v = _walk(x, sel.field)
    if sel.want is None:
        return v is not _MISSING and v is not None and v != ""
    return str("" if v is _MISSING else v).lower() == sel.want.lower()


def get_path(data: Any, path: str, *, count: bool = False) -> Any:
    """Follow a path; None if any step is missing. [n] indexes a list ([-1]: the last item);
    [field=value] picks the first item whose field (a dotted path) equals value, ignoring case;
    [has:field] the first whose field is present and not empty; last: picks the last match instead.

    count: the number of items in the list the path ends at, or, if its last step filters, the
    number of matching items. None when there's no list there."""
    steps = parse_path(path)
    cur = data
    for i, (key, sel) in enumerate(steps):
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if sel is not None:
            if not isinstance(cur, list):
                return None
            if sel.index is not None:
                cur = cur[sel.index] if -len(cur) <= sel.index < len(cur) else None
            elif count and i == len(steps) - 1:
                return sum(1 for x in cur if _matches(x, sel))
            else:
                cur = next((x for x in (reversed(cur) if sel.last else cur) if _matches(x, sel)), None)
            if cur is None:
                return None
    if count:
        return len(cur) if isinstance(cur, list) else None
    return cur


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def compass(degrees: float) -> str:
    return COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]


def duration(seconds: float) -> str:
    """5 → "5s", 83 → "1m23s", 5000 → "1h23m", 200000 → "2d7h"."""
    s = int(max(0, seconds))
    d, h, m, sec = s // 86400, s % 86400 // 3600, s % 3600 // 60, s % 60
    if d:
        return f"{d}d{h}h" if h else f"{d}d"
    if h:
        return f"{h}h{m}m" if m else f"{h}h"
    if m:
        return f"{m}m{sec}s" if sec else f"{m}m"
    return f"{sec}s"


def extract(spec: Value, data: Any) -> dict[str, str] | None:
    """{"value", "unit", "label"} as text, or None if no candidate gives a usable value."""
    for cand in spec.candidates():
        if spec.format == "count":
            n = get_path(data, cand.path, count=True)
            if n is not None:
                return {"value": str(n), "unit": "", "label": cand.label}
            continue
        raw = get_path(data, cand.path)
        if raw is None or isinstance(raw, dict | list):
            continue
        if spec.format == "text":
            text = str(raw).strip()
            if spec.map:
                text = {k.lower(): v for k, v in spec.map.items()}.get(text.lower(), text)
            text = text[:60]
            if text:
                return {"value": text, "unit": "", "label": cand.label}
            continue
        m = _NUM_UNIT.match(str(raw))
        if not m:
            continue
        number, unit = float(m.group(1)), (m.group(2) or "").strip()
        if spec.format == "compass":
            return {"value": compass(number), "unit": "", "label": cand.label}
        if spec.format == "duration":
            return {"value": duration(number), "unit": "", "label": cand.label}
        for p in spec.unit_from:
            u = get_path(data, p)
            if u not in (None, "") and not isinstance(u, dict | list):
                unit = str(u).strip()
                break
        mapped = {k.upper().lstrip("°"): v for k, v in spec.unit_map.items()}
        unit = mapped.get(unit.upper().lstrip("°"), unit)
        return {"value": _num(number), "unit": unit, "label": cand.label}
    return None


# ---- the reply -----------------------------------------------------------------------------------


def _render(template: str, values: dict[str, dict[str, str]]) -> str | None:
    """Fill a template. Text in [ ] is kept only if all its values exist; None if a value outside
    [ ] is missing (the item is then left out)."""

    def fill(t: str) -> str | None:
        missing = False

        def sub(m: re.Match) -> str:
            nonlocal missing
            v = values.get(m.group(1))
            if v is None:
                missing = True
                return ""
            return v[m.group(2) or "value"]

        out = _REF.sub(sub, t)
        return None if missing else out

    pieces = re.split(r"(\[[^\[\]]*\])", template)
    out = []
    for p in pieces:
        if p.startswith("[") and p.endswith("]"):
            out.append(fill(p[1:-1]) or "")
        else:
            filled = fill(p)
            if filled is None:
                return None
            out.append(filled)
    return "".join(out).strip()


def _fit(text: str, limit: int) -> str:
    raw = text.encode()
    return text if len(raw) <= limit else raw[: limit - 3].decode(errors="ignore").rstrip() + "..."


def _values(cmd: Command, responses: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Each value read from its source's response (values of a source that failed are missing)."""
    first = cmd.source_names()[0]
    out = {}
    for k, spec in cmd.values.items():
        src = spec.source or first
        if src in responses and (v := extract(spec, responses[src])) is not None:
            out[k] = v
    return out


def compose(cmd: Command, data: Any) -> str:
    """The reply for one response: the most important parts first, extras dropped to fit.
    A command with several `sources` takes {source name: response} (failed sources left out)."""
    values = _values(cmd, data if cmd.sources else {cmd.source: data})
    r = cmd.reply
    parts: list[tuple[list[str], bool]] = []
    for part in r.parts:
        items = [t for item in part.items if (t := _render(item, values))]
        if items:
            parts.append((items, part.drop == "end"))
    if not parts:
        return r.empty

    def text() -> str:
        return r.join.join(r.item_join.join(items) for items, _ in parts if items)

    while len(text().encode()) > r.max_bytes:
        droppable = [p for p in reversed(parts) if p[1] and p[0]]
        if not droppable:
            break
        droppable[0][0].pop()
    return _fit(text(), r.max_bytes)


def compose_group(cmd: Command, responses: list[Any]) -> str:
    """A group command's reply: `each` worded per response, in group order, joined; dropped from
    the end to fit."""
    r = cmd.reply
    items = [t for data in responses if (t := _render(r.each or "", _values(cmd, {cmd.source: data})))]
    if not items:
        return r.empty
    while len(items) > 1 and len(r.join.join(items).encode()) > r.max_bytes:
        items.pop()
    return _fit(r.join.join(items), r.max_bytes)


def _group_text(template: str, groups: list[str], arg: str) -> str:
    """no_group / unknown_group: the owner's group names and the word sent, in one pass (so a
    word like "{groups}" stays as sent)."""
    names = ", ".join(groups) or "none set up"
    return _REF.sub(lambda m: names if m.group(1) == "groups" else arg, template)


async def _gather(calls: list) -> tuple[list[Any], list[SourceError]]:
    """Run fetches together: (responses, None where one failed) and the errors."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    errors = [r for r in results if isinstance(r, SourceError)]
    if other := next((r for r in results if isinstance(r, BaseException) and r not in errors), None):
        raise other
    return [None if isinstance(r, SourceError) else r for r in results], errors


async def run(
    recipe: Recipe, command: str, settings: dict[str, str], arg: str | None = None, *, use_cache: bool = True
) -> str:
    """The reply to one command. SourceError if the source can't be read (the caller decides what
    goes over the air: the recipe's `unavailable` text, never the error's details).

    arg: the word after the command (see `argument`). Only a group command uses it, and only to
    choose one of the owner's groups: it never goes into a request."""
    cmd = recipe.command(command)
    if cmd is None:
        raise RecipeError(f"{recipe.name} has no /{command}")
    if cmd.group:
        try:
            groups = parse_groups(settings.get(cmd.group, ""))
        except ValueError:
            raise SourceError("The groups setting can't be read") from None
        r = cmd.reply
        if not arg:
            return _fit(_group_text(r.no_group or DEFAULT_NO_GROUP, list(groups), ""), r.max_bytes)
        if arg not in groups:
            return _fit(_group_text(r.unknown_group or DEFAULT_UNKNOWN_GROUP, list(groups), arg), r.max_bytes)
        assert cmd.source is not None
        responses, errors = await _gather(
            [fetch(recipe, cmd.source, settings, use_cache=use_cache, item=i) for i in groups[arg]]
        )
        if len(errors) == len(responses):
            raise errors[0]
        return compose_group(cmd, [d for d in responses if d is not None])
    if cmd.source:
        return compose(cmd, await fetch(recipe, cmd.source, settings, use_cache=use_cache))
    responses, errors = await _gather([fetch(recipe, n, settings, use_cache=use_cache) for n in cmd.sources])
    if len(errors) == len(responses):
        raise errors[0]
    return compose(cmd, {n: d for n, d in zip(cmd.sources, responses, strict=True) if d is not None})
