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

from arceus_runner.config import Config, ConfigError, parse_config

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

# Settings the console shows and never changes: they decide what runs on the machine (a shell command, the engine,
# the agent's MCP servers, the files the runner reads its environment and tokens from) or where the tokens go (the
# tracker's and the code host's address, the names of their tokens, Slack's), and how the console itself is
# reached. Changed in such a way, the console would be a way to run code on the VM or to send its tokens elsewhere;
# they are set on the server, over SSH.
LOCKED_KEYS = ("paths", "health.devstack_check", "bulava.mcp_config", "tracker.type", "tracker.url",
               "tracker.token_env", "code_host.type", "code_host.url", "code_host.token_env", "code_host.approver",
               "slack.token_env", "slack.webhook_env", "console", "metrics", "qa.instructions", "qa.reports",
               "architect.instructions", "architect.reports")

# Folders the console reads (cli._read_dirs): moved outside the data folder, setup.sh mounts them for it.
MOUNTED_KEYS = ("qa.reports", "qa.instructions", "architect.reports", "architect.instructions")

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

    @property
    def locked(self) -> bool:
        return is_locked(self.name)

    @property
    def mounted(self) -> bool:
        """A folder the console reads: outside the data folder, setup.sh has to mount it first."""
        return self.name in MOUNTED_KEYS


def _yaml(sequence: int = 4, offset: int = 2) -> YAML:
    y = YAML()  # round trip: comments, order and quoting stay
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=sequence, offset=offset)
    return y


def indent_style(text: str) -> tuple[int, int]:
    """How the file indents a list under its key, as ruamel's (sequence, offset): `key:\n    - a` is (4, 2),
    `key:\n  - a` under a key at the same column is (2, 0). New lines are written the file's way."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        m = re.match(r"^(\s*)[^\s#-][^:#]*:\s*(#.*)?$", line)
        if not m:
            continue
        nxt = next((x for x in lines[i + 1:] if x.strip() and not x.lstrip().startswith("#")), "")
        d = re.match(r"^(\s*)- ", nxt)
        if d and len(d.group(1)) >= len(m.group(1)):
            offset = len(d.group(1)) - len(m.group(1))
            return offset + 2, offset
    return 4, 2


def load_doc(text: str):
    doc = _yaml().load(text) if text.strip() else CommentedMap()
    return doc if doc is not None else CommentedMap()


def dump_doc(doc, style: tuple[int, int] = (4, 2)) -> str:
    buf = io.StringIO()
    _yaml(*style).dump(doc, buf)
    return buf.getvalue()


def split_comment(line: str) -> tuple[str, str]:
    """(the YAML on a line, its comment with the spaces before it): a # inside quotes is text, not a comment."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and not (quote == '"' and line[i - 1] == "\\"):
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            code = line[:i].rstrip()
            return code, line[len(code):]
    return line.rstrip(), ""


def _restyle(old: str, new: str) -> str:
    """A changed line in the original's style: `{ a: b }` with the spaces it had, its comment where it was."""
    old_code, old_comment = split_comment(old)
    new_code, new_comment = split_comment(new)
    m = re.match(r"^(\s*(?:- )?[^\s:#][^:#]*:\s)(\{.*\})$", new_code)
    if m and re.search(r":\s\{ ", old_code) and not m.group(2).startswith("{ "):
        new_code = f"{m.group(1)}{{ {m.group(2)[1:-1]} }}"
    if new_comment and old_comment:
        col = len(old) - len(old_comment.lstrip())  # where the original's # stood
        new_code = new_code.ljust(max(col - 1, len(new_code))) + " " + new_comment.lstrip()
    elif new_comment:
        new_code += new_comment
    return new_code


def _is_aside(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _span(lines: list[str], start: int, col: int) -> tuple[int, int]:
    """The lines of the entry whose key is at (start, col): the key's line and what is indented under it (a list's
    `- ` lines at the key's own column too), without the blank and comment lines after it, which stay."""
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not _is_aside(line):
            indent = len(line) - len(line.lstrip())
            if indent < col or (indent == col and not line.lstrip().startswith("- ")):
                break
        end += 1
    while end > start + 1 and _is_aside(lines[end - 1]):
        end -= 1
    return start, end


def _node(doc, path: tuple[str, ...]):
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _exists(doc, path: tuple[str, ...]) -> bool:
    """The key is in the document, whatever its value (null included)."""
    if not path:
        return True
    parent = _node(doc, path[:-1]) if path[:-1] else doc
    return isinstance(parent, dict) and path[-1] in parent


def _on_own_line(doc, path: tuple[str, ...]) -> bool:
    """The entry at `path` starts a line of its own: its parent is a block mapping, not `{ ... }`."""
    parent = _node(doc, path[:-1]) if path[:-1] else doc
    return isinstance(parent, CommentedMap) and path[-1] in parent and not parent.fa.flow_style()


def _key_at(doc, path: tuple[str, ...]) -> tuple[int, int]:
    parent = _node(doc, path[:-1]) if path[:-1] else doc
    return parent.lc.key(path[-1])


def write(text: str, edits: list[tuple[tuple[str, ...], Any]]) -> str:
    """`text` with each (path, value) set (or removed with _REMOVE), touching only the lines of the entries that
    change. ruamel writes the changed entries; every other line is the original's, byte for byte."""
    if not edits:
        return text
    style = indent_style(text)
    doc = load_doc(text)
    before = load_doc(text)
    for path, value in edits:
        _set(doc, path, value)
    dumped = dump_doc(doc, style)
    after = load_doc(dumped)
    a, b = text.splitlines(keepends=True), dumped.splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n"
    ops: dict[tuple[int, int], list[str]] = {}
    for path, _ in edits:
        # The entry to rewrite: the deepest one on a line of its own in both texts (a value inside `{ }` is its
        # whole line); a key the original lacks goes in under its nearest ancestor that is there.
        anchor = path
        while len(anchor) > 1 and not (_exists(before, anchor) and _on_own_line(before, anchor)
                                       and (not _exists(after, anchor) or _on_own_line(after, anchor))):
            parent = _node(before, anchor[:-1])
            if not _exists(before, anchor) and isinstance(parent, CommentedMap) and not parent.fa.flow_style():
                break  # a new key under a block mapping that is there: inserted below
            anchor = anchor[:-1]
        if _exists(before, anchor):
            # The entry is there (with a value, or null): its lines are replaced, never added to.
            line, col = _key_at(before, anchor)
            old = _span(a, line, col)
            new_lines: list[str] = []
            if _exists(after, anchor):
                nline, ncol = _key_at(after, anchor)
                s, e = _span(b, nline, ncol)
                new_lines = [(" " * (col - ncol) + x if col >= ncol else x[ncol - col:]) for x in b[s:e]]
                if len(new_lines) == old[1] - old[0]:
                    new_lines = [_restyle(o.rstrip("\n"), n.rstrip("\n")) + "\n" for o, n in zip(a[old[0]:old[1]],
                                                                                         new_lines)]
            ops[old] = new_lines
        else:
            # A new key: its lines, at the end of its parent's entry (or of the file).
            parent = anchor[:-1]
            nline, ncol = _key_at(after, anchor)
            s, e = _span(b, nline, ncol)
            if parent:
                pline, pcol = _key_at(before, parent)
                _, pend = _span(a, pline, pcol)
                children = [k for k in _node(before, parent)] if isinstance(_node(before, parent), dict) else []
                col = _key_at(before, (*parent, children[0]))[1] if children else pcol + 2
            else:
                pend = len(a)
                while pend and _is_aside(a[pend - 1]):
                    pend -= 1
                col = 0
            ops[(pend, pend)] = ops.get((pend, pend), []) + [
                (" " * (col - ncol) + x if col >= ncol else x[ncol - col:]) for x in b[s:e]]
    # Outer entries win over entries inside them; then from the bottom up, so line numbers hold.
    spans = sorted(ops, key=lambda r: (r[0], -r[1]))
    kept: list[tuple[int, int]] = []
    for r in spans:
        if kept and r[0] < kept[-1][1] and r[1] <= kept[-1][1] and r != kept[-1] and r[0] != r[1]:
            continue
        kept.append(r)
    out = list(a)
    for r in sorted(kept, key=lambda r: (r[0], r[1]), reverse=True):
        out[r[0]:r[1]] = ops[r]
    result = "".join(out)
    return result if text.endswith("\n") or not result.endswith("\n") else result


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
        # Only a comment that starts on the key's own line is its hint. ruamel also hangs on
        # the last key of a block the blank lines and comments that follow it (the next
        # section's header, a commented-out key); that token starts with a newline.
        if tok is not None and not isinstance(tok, list) and getattr(tok, "value", "").lstrip(" \t").startswith("#"):
            return tok.value.lstrip(" \t").split("\n")[0].lstrip("#").strip()
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


def is_locked(name: str) -> bool:
    return any(name == k or name.startswith(k + ".") for k in LOCKED_KEYS)


def locked_changes(before: str, after: str) -> list[str]:
    """The locked settings (LOCKED_KEYS) whose value differs between two texts of config.yaml, read the way the
    runner reads them."""

    def load(text):
        try:
            return pyyaml.safe_load(text) or {}
        except pyyaml.YAMLError:
            return None
    a, b = load(before), load(after)
    if a is None or b is None:
        return []  # not YAML: the runner's own check refuses it anyway

    def pick(d, key):
        node = d
        for k in key.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        return node
    return [k for k in LOCKED_KEYS if pick(a, k) != pick(b, k)]


def edits(fields: list[Field], form) -> list[tuple[tuple[str, ...], Any]]:
    """What the form changes: (path, new value) for each field whose value differs from what the page showed. The
    locked settings are not read from the form at all."""
    out = []
    for f in flatten(fields):
        if f.locked:
            continue
        did, value = _parse(f, form)
        if did:
            out.append((f.path, value))
    return out


def apply(text: str, fields: list[Field], form, data_dir: Path | None = None) -> Change:
    """The file's text with the form's changes made, only to the lines of the keys that changed."""
    changes = edits(fields, form)
    if not changes:
        return Change(text, [], False)
    new = write(text, changes)
    return Change(new, [".".join(p) for p, _ in changes], needs_setup(text, new, data_dir))


def check(text: str, before: str | None = None) -> list[str]:
    """What the runner would say about this config.yaml: nothing when it loads. A key written twice in one mapping is
    refused too: PyYAML would quietly take the last one, and the form could not show which one counts. With `before`,
    a change to a locked setting is refused as well."""
    if before is not None:
        locked = locked_changes(before, text)
        if locked:
            return [f"{k}: set on the server, not in the console (it decides what runs on the machine or where the "
                    "tokens go): edit config.yaml over SSH" for k in locked]
    try:
        YAML(typ="rt").load(text)
    except Exception as e:  # ruamel's DuplicateKeyError; any other parse error parse_config words below
        if "duplicate key" in str(e):
            found = re.search(r'found duplicate key "([^"]+)"', str(e))
            where = re.search(r"line (\d+)", str(e).split("found duplicate key", 1)[-1])
            return [f"config.yaml: the key {found.group(1) if found else '?'} is written twice in the same place"
                    + (f" (line {where.group(1)})" if where else "") + ": keep one"]
    try:
        parse_config(text)
    except ConfigError as e:
        return e.problems
    return []


def outside_mounts(text: str, data_dir: Path | None) -> set[Path] | None:
    """The folders the console reads that are outside the data folder (setup.sh mounts them into its container), or
    None when the text does not load."""
    from arceus_runner.console.cli import _read_dirs
    try:
        cfg = parse_config(text)
    except ConfigError:
        return None
    data = data_dir.resolve() if data_dir else None
    return {d for d in _read_dirs(cfg) if not (data and (d == data or data in d.parents))}


def needs_setup(before: str, after: str, data_dir: Path | None = None) -> bool:
    """Whether the change between two texts touches what setup.sh reads: its own keys, or a folder the console
    reads moving out of what its container already sees."""
    try:
        a, b = pyyaml.safe_load(before) or {}, pyyaml.safe_load(after) or {}
    except pyyaml.YAMLError:
        return True

    def pick(d, key):
        node = d
        for k in key.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        return node
    if any(pick(a, k) != pick(b, k) for k in SETUP_KEYS):
        return True
    new = outside_mounts(after, data_dir)
    return new is not None and not new <= (outside_mounts(before, data_dir) or set())
