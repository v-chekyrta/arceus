"""config.yaml as a form: its fields worked out from the runner's own schema (config.Config), and a change written
back into the file without touching anything else.

- **Every setting is there.** Each field of a section's model is a form field: a checkbox, a number, a choice, a
  text, a list of lines, a set of checkboxes. What a field cannot hold (a map, a list of objects, a block that may be
  absent) is shown as its YAML, edited in place; so nothing the schema allows is left out of the form.
- **The value shown** is what config.yaml says when it says it, as written there, else the default.
- **A change touches only its lines.** The file is read with ruamel.yaml, which keeps comments, order and quoting;
  only the keys whose submitted value differs from what was shown are set. Nothing changed: the same bytes.
- **The check is the runner's** (config.parse_config): the same YAML loader (PyYAML), the same schema, the same
  words. A text value PyYAML would read as something else (`09:00`, `off`, `1.0`) is written quoted.
"""

from __future__ import annotations

import difflib
import io
import re
import types
import typing
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml as pyyaml
from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

from arceus_runner.config import Config, ConfigError, is_legacy, parse_config

# Sections of the form: the top-level keys of config.yaml, in the order the example file has them, plus `general`
# for the two plain keys at the top.
TITLES = {
    "general": ("Project and mode", "The project's name and whether the runner only observes or takes work."),
    "tracker": ("Issue tracker", "Where tickets come from, who may queue them, the tags and states the runner uses."),
    "code_host": ("Code host", "The repositories, the bot that pushes and opens merge requests, the approver."),
    "slack": ("Slack", "The project's channel: what is told there and when."),
    "schedule": ("Schedule", "Working hours and how often each service is polled."),
    "http": ("HTTP", "Timeouts and backoff of the calls to the tracker and the code host."),
    "limits": ("Limits", "Review rounds, CI fixes, reminders and timeouts of a task."),
    "health": ("Health", "When disk, memory and tokens are a warning or a failure."),
    "bulava": ("Engine and models", "Claude's and Codex's models and depth, the checks before Codex."),
    "security": ("Security screen", "Who decides on held texts, the classifier's model."),
    "review_agent": ("Code review on request", "/review on a ticket: what it checks per repository."),
    "qa": ("Test runs", "Testing the platform: on/off, schedule, where reports go."),
    "architect": ("Architecture reviews", "On/off, schedule, which repositories, what is not the team's."),
    "cleanup": ("Cleanup", "What is removed between tasks and when."),
    "console": ("Console", "This console's address, port, trusted proxies and sessions."),
    "metrics": ("Metrics", "The runner's Prometheus endpoint."),
    "paths": ("Paths", "Where the workspace, the engine, the database and the secrets file are."),
}
# What a change needs before it takes effect. Everything the runner reads needs it restarted; these are also read by
# setup.sh, which writes them into systemd units, Docker files or nginx, so setup has to run first.
SETUP_KEYS = ("console", "paths", "bulava.claude_checks", "bulava.mcp_config", "metrics")

KINDS_SIMPLE = {"bool", "int", "float", "str", "choice", "duration", "time", "path", "lines", "multi"}


@dataclass
class Field:
    path: tuple[str, ...]          # the keys from the top of config.yaml: ("schedule", "working_hours", "days")
    label: str
    kind: str                      # bool int float str choice duration time path lines multi yaml group
    shown: Any = None              # the value as the form shows it: bool, or text, or a list of chosen options
    options: list[str] = field(default_factory=list)
    nullable: bool = False
    in_file: bool = False          # config.yaml says it (else the default is shown)
    hint: str = ""                 # the comment config.yaml has beside it
    children: list[Field] = field(default_factory=list)

    @property
    def name(self) -> str:
        return ".".join(self.path)

    @property
    def needs_setup(self) -> bool:
        return any(self.name == k or self.name.startswith(k + ".") for k in SETUP_KEYS)


def _yaml() -> YAML:
    y = YAML()  # round trip: comments, order and quoting stay
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_doc(text: str):
    doc = _yaml().load(text) if text.strip() else CommentedMap()
    return doc if doc is not None else CommentedMap()


def dump_doc(doc) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _norm(line: str) -> str:
    """A line as ruamel would write it: `{ a: b }` as `{a: b}`, the space before a comment as one."""
    line = re.sub(r"\{\s+", "{", re.sub(r"\s+\}", "}", line))
    return re.sub(r"\s+#", " #", line).rstrip()


def _restyle(old: str, new: str) -> str:
    """A changed line in the original's style: spaces inside flow braces as it had them, its comment where it was."""
    if "{ " in old.split("#")[0]:
        head, sep, comment = new.partition(" #")
        head = re.sub(r"\{(?=[^\s}])", "{ ", head)
        head = re.sub(r"(?<=[^\s{])\}", " }", head)
        new = head + (sep + comment if sep else "")
    old_hash, new_hash = old.find(" #"), new.find(" #")
    if old_hash > 0 and new_hash > 0:
        col = len(old) - len(old[old_hash:].lstrip())  # where the comment's # stood
        head = new[:new_hash].rstrip()
        new = head.ljust(max(col - 1, len(head))) + " " + new[new_hash:].lstrip()
    return new


def keep_untouched(original: str, dumped: str) -> str:
    """The dumped text with every line ruamel only reformatted put back as it was: a change shows as its own lines
    only, and nothing else in the file moves by a byte."""
    a, b = original.splitlines(keepends=True), dumped.splitlines(keepends=True)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += a[i1:i2]
        elif op == "replace" and i2 - i1 == j2 - j1:
            for old, new in zip(a[i1:i2], b[j1:j2]):
                if _norm(old) == _norm(new):
                    out.append(old)
                else:
                    end = "\n" if new.endswith("\n") else ""
                    out.append(_restyle(old.rstrip("\n"), new.rstrip("\n")) + end)
        else:
            out += b[j1:j2]
    return "".join(out)


def duration_text(td: timedelta) -> str:
    s = int(td.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s and s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"


def _strip_annotated(tp):
    while typing.get_origin(tp) is typing.Annotated:
        tp = typing.get_args(tp)[0]
    return tp


def _optional(tp) -> tuple[Any, bool]:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1 and len(typing.get_args(tp)) == 2:
            return _strip_annotated(args[0]), True
    return tp, False


def _is_model(tp) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _kind(tp) -> tuple[str, list[str]]:
    """The form field for a type: (kind, options)."""
    tp = _strip_annotated(tp)
    if tp is bool:
        return "bool", []
    if tp is int:
        return "int", []
    if tp is float:
        return "float", []
    if tp is str:
        return "str", []
    if tp is timedelta:
        return "duration", []
    if tp is time:
        return "time", []
    if isinstance(tp, type) and issubclass(tp, Path):
        return "path", []
    if typing.get_origin(tp) is Literal:
        return "choice", [str(a) for a in typing.get_args(tp)]
    if typing.get_origin(tp) is list:
        (inner,) = typing.get_args(tp) or (Any,)
        inner = _strip_annotated(inner)
        if inner is str:
            return "lines", []
        if typing.get_origin(inner) is Literal:
            return "multi", [str(a) for a in typing.get_args(inner)]
    return "yaml", []


def _raw_get(doc, path: tuple[str, ...]):
    """(present, value) of `path` in the YAML document."""
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _comment(doc, path: tuple[str, ...]) -> str:
    """The comment at the end of the key's line in config.yaml, if any."""
    parent_present, parent = _raw_get(doc, path[:-1]) if len(path) > 1 else (True, doc)
    if not parent_present or not isinstance(parent, CommentedMap):
        return ""
    try:
        tokens = parent.ca.items.get(path[-1])
    except AttributeError:
        return ""
    if not tokens:
        return ""
    for tok in tokens:
        if tok is not None and not isinstance(tok, list) and getattr(tok, "value", "").lstrip().startswith("#"):
            return tok.value.strip().lstrip("#").strip().split("\n")[0]
    return ""


def _scalar_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, timedelta):
        return duration_text(value)
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _snippet(value) -> str:
    """A YAML value as the editor shows it: block style, comments kept when it came from the file."""
    if value is None:
        return ""
    if isinstance(value, BaseModel):
        value = _plain(value.model_dump(mode="python", by_alias=True))
    buf = io.StringIO()
    _yaml().dump(value, buf)
    text = buf.getvalue()
    return "" if text.strip() in ("{}", "[]", "null", "...") else text.removesuffix("...\n")


def _plain(value):
    """A model's dump as YAML writes it: durations as 30m, times as 09:00, paths as text."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (timedelta, time, Path)):
        return _scalar_text(value)
    return value


def fields_of(model_cls: type[BaseModel], value: BaseModel | None, doc, path: tuple[str, ...]) -> list[Field]:
    out = []
    for name, info in model_cls.model_fields.items():
        key = info.alias or name
        fpath = (*path, key)
        present, raw = _raw_get(doc, fpath)
        current = getattr(value, name) if value is not None else info.get_default(call_default_factory=True)
        tp, nullable = _optional(_strip_annotated(info.annotation))
        hint = _comment(doc, fpath)
        if _is_model(tp) and (current is not None or not nullable):
            sub_cls = type(current) if isinstance(current, BaseModel) else tp
            out.append(Field(fpath, key, "group", in_file=present, hint=hint,
                             children=fields_of(sub_cls, current, doc, fpath)))
            continue
        kind, options = _kind(tp)
        if kind == "yaml":
            shown = _snippet(raw) if present else _snippet(current)
        elif kind == "bool":
            shown = bool(raw) if present and isinstance(raw, bool) else bool(current)
        elif kind in ("lines", "multi"):
            items = raw if present and isinstance(raw, list) else (current or [])
            shown = [str(x) for x in items]
        elif present and raw is not None and not isinstance(raw, (dict, list)):
            shown = str(raw)
        else:
            shown = _scalar_text(current)
        out.append(Field(fpath, key, kind, shown, options, nullable, present, hint))
    return out


def sections(cfg: Config, text: str) -> list[tuple[str, list[Field]]]:
    """Every section of the form, with its fields, from the loaded config and the file's text."""
    doc = load_doc(text)
    general = [f for f in fields_of(Config, cfg, doc, ()) if f.name in ("project", "mode")]
    out = [("general", general)]
    for name, info in Config.model_fields.items():
        if name in ("project", "mode"):
            continue
        value = getattr(cfg, name)
        tp, nullable = _optional(_strip_annotated(info.annotation))
        present, raw = _raw_get(doc, (name,))
        if value is None:
            # An optional block that is not there (slack): its YAML, to add it.
            out.append((name, [Field((name,), name, "yaml", "", nullable=True, in_file=present)]))
            continue
        out.append((name, fields_of(type(value), value, doc, (name,))))
    return out


def flatten(fields: list[Field]) -> list[Field]:
    out = []
    for f in fields:
        out += flatten(f.children) if f.kind == "group" else [f]
    return out


class Invalid(ValueError):
    pass


def _parse(f: Field, form) -> tuple[bool, Any]:
    """(changed, the new value as YAML should hold it) for one field from the submitted form."""
    name = f.name
    if f.kind == "bool":
        new = form.get(name) == "on"
        return new != f.shown, new
    if f.kind == "multi":
        chosen = [o for o in f.options if o in form.getlist(name)]
        return set(chosen) != set(f.shown), chosen
    text = form.get(name)
    if text is None:
        return False, None  # not on this page
    text = str(text).replace("\r\n", "\n")
    if f.kind == "lines":
        items = [line.strip() for line in text.split("\n") if line.strip()]
        return items != f.shown, items
    if f.kind == "yaml":
        if text.strip() == (f.shown or "").strip():
            return False, None
        if not text.strip():
            return True, _REMOVE
        try:
            return True, _yaml().load(text)
        except Exception as e:  # ruamel raises many kinds; all mean the snippet is not YAML
            raise Invalid(f"{name}: not valid YAML ({type(e).__name__}: {str(e).splitlines()[0][:160]})") from None
    text = text.strip()
    if text == str(f.shown or "").strip():
        return False, None
    if text == "" and f.nullable:
        return True, None
    if f.kind == "int":
        try:
            return True, int(text)
        except ValueError:
            raise Invalid(f"{name}: a whole number, not {text!r}") from None
    if f.kind == "float":
        try:
            return True, float(text)
        except ValueError:
            raise Invalid(f"{name}: a number, not {text!r}") from None
    if f.kind == "choice" and text not in f.options:
        raise Invalid(f"{name}: one of {', '.join(f.options)}")
    return True, _quoted(text)


_REMOVE = object()


def _quoted(text: str):
    """A text value as YAML has to carry it so that PyYAML (the runner's loader) reads it back as this text."""
    try:
        back = pyyaml.safe_load(text)
    except pyyaml.YAMLError:
        back = None
    return text if isinstance(back, str) and back == text else DoubleQuotedScalarString(text)


def _set(doc, path: tuple[str, ...], value) -> None:
    node = doc
    for key in path[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = CommentedMap()
        node = node[key]
    if value is _REMOVE:
        node.pop(path[-1], None)
    else:
        if isinstance(value, str) and "\n" in value:
            value = LiteralScalarString(value)
        node[path[-1]] = value


@dataclass
class Change:
    text: str                      # config.yaml with the changes
    changed: list[str]             # the settings that changed
    needs_setup: bool


def apply(text: str, fields: list[Field], form) -> Change:
    """The file's text with the form's changes made, only to the keys that changed."""
    doc = load_doc(text)
    changed, setup = [], False
    for f in flatten(fields):
        did, value = _parse(f, form)
        if not did:
            continue
        _set(doc, f.path, value)
        changed.append(f.name)
        setup = setup or f.needs_setup
    if not changed:
        return Change(text, [], False)
    return Change(keep_untouched(text, dump_doc(doc)), changed, setup)


def check(text: str) -> list[str]:
    """What the runner would say about this config.yaml: nothing when it loads."""
    try:
        parse_config(text)
    except ConfigError as e:
        return e.problems
    return []


def needs_setup(before: str, after: str) -> bool:
    """Whether the change between two texts touches what setup.sh reads."""
    try:
        a, b = pyyaml.safe_load(before) or {}, pyyaml.safe_load(after) or {}
    except pyyaml.YAMLError:
        return True

    def pick(d, key):
        node = d
        for k in key.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        return node
    return any(pick(a, k) != pick(b, k) for k in SETUP_KEYS)


def legacy(text: str) -> bool:
    try:
        return is_legacy(pyyaml.safe_load(text))
    except pyyaml.YAMLError:
        return False
