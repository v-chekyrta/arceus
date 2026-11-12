"""Saving a factory file from the console under concurrency and failure: two saves made from the same page, the
temporary file's name, and a write that fails after its version was recorded."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from arceus_runner.console import files
from arceus_runner.console.store import ConsoleStore

from tests.test_console_auth import PASSWORD


@pytest.fixture
def w(tmp_path: Path):
    db = tmp_path / "console" / "console.db"
    with ConsoleStore(db) as s:
        s.use_link(s.make_link("invite", "owner@example.com", {"admin"}, actor="test"), PASSWORD, "Owner")
    target = tmp_path / "data" / "notes.md"
    target.parent.mkdir()
    target.write_text("first\n")
    return db, target


def test_two_saves_from_the_same_page_one_succeeds_the_other_is_a_conflict(w, monkeypatch):
    db, target = w
    sha = files.read(target).sha
    both_read = threading.Barrier(2, timeout=5)
    real_read = files.read

    def slow_read(path):
        current = real_read(path)
        try:
            both_read.wait(timeout=0.5)  # without the lock both threads meet here with the same sha
        except threading.BrokenBarrierError:
            pass
        return current

    monkeypatch.setattr(files, "read", slow_read)
    results: list[object] = []

    def save(text: str) -> None:
        store = ConsoleStore(db)
        try:
            user = store.user_by_email("owner@example.com")
            results.append(files.save(store, "notes", target, text, sha, user))
        except files.Conflict as c:
            results.append(c)
        finally:
            store.close()

    threads = [threading.Thread(target=save, args=(t,)) for t in ("from Ann\n", "from Bob\n")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "bool"]
    assert [r for r in results if r is True] == [True]
    with ConsoleStore(db) as s:
        versions = s.versions("notes")
    assert len(versions) == 1
    assert target.read_text() == versions[0]["after_text"]


def test_the_temporary_file_has_a_name_of_its_own_each_time(tmp_path, monkeypatch):
    seen: list[str] = []
    real_replace = files.os.replace

    def replace(src, dst):
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(files.os, "replace", replace)
    for n in range(3):
        files.write_atomic(tmp_path / "f.txt", f"{n}\n".encode())
    assert len(set(seen)) == 3
    assert (tmp_path / "f.txt").read_text() == "2\n"


def test_a_write_that_fails_leaves_no_version_and_the_file_as_it_was(w, monkeypatch):
    db, target = w

    def broken(path, data, mode=0o600):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files, "write_atomic", broken)
    with ConsoleStore(db) as s:
        user = s.user_by_email("owner@example.com")
        with pytest.raises(OSError):
            files.save(s, "notes", target, "second\n", files.read(target).sha, user)
        assert s.versions("notes") == []
    assert target.read_text() == "first\n"


def test_the_earlier_text_is_in_the_history_before_the_file_is_replaced(w, monkeypatch):
    db, target = w
    seen: list[list[dict]] = []
    real_write = files.write_atomic

    def watching(path, data, mode=0o600):
        with ConsoleStore(db) as other:  # what a crash at this moment would leave behind
            seen.append(other.versions("notes"))
        real_write(path, data, mode)

    monkeypatch.setattr(files, "write_atomic", watching)
    with ConsoleStore(db) as s:
        user = s.user_by_email("owner@example.com")
        assert files.save(s, "notes", target, "second\n", files.read(target).sha, user)
    assert seen[0][0]["before_text"] == "first\n"
    assert target.read_text() == "second\n"


def test_a_failure_after_the_file_was_replaced_keeps_its_version_to_bring_the_old_text_back(w, monkeypatch):
    db, target = w
    real_fsync = files.os.fsync

    def fsync(fd):
        if _is_dir(fd):
            raise OSError(5, "Input/output error")  # syncing the folder, after the rename
        real_fsync(fd)

    monkeypatch.setattr(files.os, "fsync", fsync)
    with ConsoleStore(db) as s:
        user = s.user_by_email("owner@example.com")
        assert files.save(s, "notes", target, "second\n", files.read(target).sha, user)
        [version] = s.versions("notes")
        [saved] = [e for e in s.journal() if e["action"] == "file_saved"]
    assert target.read_text() == "second\n"
    assert (version["before_text"], version["after_text"]) == ("first\n", "second\n")
    assert "Input/output error" in saved["detail"]["folder_sync"]


def _is_dir(fd: int) -> bool:
    import stat
    return stat.S_ISDIR(files.os.fstat(fd).st_mode)
