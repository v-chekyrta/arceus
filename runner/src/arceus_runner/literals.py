"""Code in the agents' prose: file paths, lines, identifiers and commits, set apart from the words around them.

The agents are asked to put such things in backticks, as Markdown does. Older text, and text that forgot, still has
code a reader looks for, in shapes prose never takes, and those are marked as code too:

- a path with a folder and an extension, or a file name with a line (`dir/name.ext:12`);
- a commit: hex with both digits and letters, 7 to 40 long;
- a name, or a dotted chain of names (`Klass.method`), of which some part is code-shaped: an underscore inside it
  (`create_account`, `MAX_SIZE`, `__call__`), a capital after a small letter (`SearchView`, `getUser`), a call
  (`send()`), a decorator (`@transaction.atomic`), or three parts or more (`django.core.cache`).

Nothing else is guessed at: a word with a slash ("and/or"), a capitalised word, a version, and the names of well
known products that are written like class names (GitLab, JavaScript, PostgreSQL: PRODUCTS) stay prose.
"""
from __future__ import annotations

import re

# A fenced block (three backticks, one line or several) first, then an inline span: "```a``` and `b`" is two.
_MARKS = re.compile(r"```(.+?)```|`([^`\n]+)`", re.S)
# Never inside a longer word, a URL or a span already marked.
_SHAPES = re.compile(r"(?<![\w/.:`@-])(?:"
                     r"(?P<path>(?:[\w.-]+/)+[\w.-]*\.[A-Za-z]\w{0,7}(?::\d+(?:[-–]\d+)?)?"   # dir/name.ext[:12]
                     r"|[\w.-]+\.[A-Za-z]\w{0,7}:\d+(?:[-–]\d+)?)"                            # name.ext:12
                     r"|(?P<sha>(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40})"                # a commit
                     r"|(?P<name>@?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\))?)"                     # name, a.b.c, f()
                     r")(?![\w/`(])")
_UNDERSCORE = re.compile(r"[A-Za-z0-9]_[A-Za-z0-9]|^_+[A-Za-z0-9]")
_HUMP = re.compile(r"[a-z][A-Z]")
# Products whose names look like class names. Generic: any project's prose may name them.
PRODUCTS = frozenset("""
    GitLab GitHub BitBucket YouTrack JavaScript TypeScript CoffeeScript PostgreSQL MySQL MongoDB MariaDB DynamoDB
    ClickHouse RabbitMQ OpenSearch ElasticSearch GraphQL OpenAI WordPress PayPal LinkedIn YouTube WhatsApp iOS iPadOS
    macOS iPhone iPad eBay PowerShell DevOps CloudFlare DigitalOcean SharePoint OneDrive FastAPI NodeJS VSCode IntelliJ
    PyCharm PhpStorm WebStorm JetBrains SonarQube CodeRabbit LibreOffice OpenStack DataDog HubSpot SalesForce DocuSign
""".split())


def _is_code(name: str) -> bool:
    """Whether a name or a dotted chain of names has a part prose never has."""
    if name.startswith("@") or name.endswith("()"):
        return True
    parts = name.split(".")
    if len(parts) >= 3 and all(p[:1].islower() or p[:1] == "_" for p in parts):
        return True
    return any(_UNDERSCORE.search(p) or (_HUMP.search(p) and p not in PRODUCTS) for p in parts)


def spans(text: str) -> list[tuple[str, str]]:
    """The text as (part, kind) pairs, kind "text", "code" (inline) or "block" (fenced): what the backticks mark,
    then the code found by its shape in the rest. A backtick without its pair stays a character of the prose."""
    out: list[tuple[str, str]] = []
    at = 0
    for m in _MARKS.finditer(text or ""):
        out += _shapes(text[at:m.start()])
        out.append((m.group(1), "block") if m.group(1) is not None else (m.group(2), "code"))
        at = m.end()
    out += _shapes((text or "")[at:])
    return [(p, kind) for p, kind in out if p]


def _shapes(part: str) -> list[tuple[str, str]]:
    out, at = [], 0
    for m in _SHAPES.finditer(part):
        if m.group("name") is not None and not _is_code(m.group("name")):
            continue
        out += [(part[at:m.start()], "text"), (m.group(0), "code")]
        at = m.end()
    return [*out, (part[at:], "text")]


def marked(text: str) -> str:
    """The text with every code part in backticks, for a place that reads Markdown (Slack)."""
    fence = {"code": "`", "block": "```", "text": ""}
    return "".join(f"{fence[kind]}{p}{fence[kind]}" for p, kind in spans(text))


def balanced(text: str) -> str:
    """A text cut short inside a fenced block or a code span gets it closed before its ellipsis, so the rest of a
    message does not turn into code."""
    body = text.rstrip(" …")
    close = "```" if body.count("```") % 2 else "`" if body.replace("```", "").count("`") % 2 else ""
    return body + close + text[len(body):]
