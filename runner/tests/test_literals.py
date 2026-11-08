"""Code in the agents' prose: marked where its shape says so, prose left alone."""

from __future__ import annotations

import pytest

from arceus_runner.literals import balanced, marked


@pytest.mark.parametrize("text,expected", [
    ("in pkg/mod/views.py:49 and AGENTS.md:105", "in `pkg/mod/views.py:49` and `AGENTS.md:105`"),
    ("lines core/a_b.py:10-20.", "lines `core/a_b.py:10-20`."),
    ("make_account and MAX_SIZE", "`make_account` and `MAX_SIZE`"),
    ("@transaction.atomic runs send() and exc.response.json()",
     "`@transaction.atomic` runs `send()` and `exc.response.json()`"),
    ("kept in django.core.cache", "kept in `django.core.cache`"),
    ("at 965e1ef45bfd", "at `965e1ef45bfd`"),
    # Class names, chains and dunders: what an agent writes of the code without backticks.
    ("OrderView is AllowAny and raises ValidationError", "`OrderView` is `AllowAny` and raises `ValidationError`"),
    ("Report.__call__ logs it; getUser and self.cache_key", "`Report.__call__` logs it; `getUser` and `self.cache_key`"),
    ("changes to Order, OrderLine, CartItem.", "changes to Order, `OrderLine`, `CartItem`."),
    ("already `marked` stays once", "already `marked` stays once"),
    ("Before ```one``` and `two`", "Before ```one``` and `two`"),
    ("a block ```x = 1\ny = 2``` then make_x", "a block ```x = 1\ny = 2``` then `make_x`"),
])
def test_code_is_marked(text, expected):
    assert marked(text) == expected


@pytest.mark.parametrize("text", [
    "and/or the rest", "GitLab, JavaScript and iOS", "Odoo, LMS and XBlock", "v1.2 and 3.5 s", "e.g. this", "user@example.com",
    "https://gl.example/a_b/c.py", "deadbeef and 1234567", "README.md alone", "an odd ` backtick",
])
def test_prose_is_left_alone(text):
    assert marked(text) == text


def test_a_cut_inside_code_is_closed_before_the_ellipsis():
    assert balanced("see `pkg/very/long …") == "see `pkg/very/long` …"
    assert balanced("whole `code` here") == "whole `code` here"
    assert balanced("see ```x = 1 …") == "see ```x = 1``` …"
    assert balanced("a ```b``` and `c …") == "a ```b``` and `c` …"
