"""Check plugins: each loads, its example is valid, and the built-in ones behave."""
import os
import time

import pytest

import checks
import server
from checks import REGISTRY
from checks.data import dig, last_row


def test_all_plugins_load():
    assert not checks.LOAD_ERRORS
    assert {"http", "tcp", "quote", "process", "path", "json", "csv"} <= set(REGISTRY)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_example_is_a_valid_feed(name):
    ct = REGISTRY[name]
    assert ct.doc, f"{name} needs a docstring (shown in the editor)"
    assert ct.example, f"{name} needs an example (the editor's + Add template)"
    panels = server.parse_config("panels:\n" + "\n".join("  " + ln for ln in ct.example.splitlines()))
    assert len(panels) == 1


def test_dig():
    data = {"a": {"items": [{"v": 1}, {"v": 2}]}}
    assert dig(data, "a.items.0.v") == 1
    assert dig(data, "a.items.-1.v") == 2
    with pytest.raises(KeyError):
        dig(data, "a.nope")


def test_last_row():
    text = "Date,Spent,Remaining\n2026-09-01,100,900\n2026-10-01,250,650\n,,\n"
    row = last_row(text)
    assert row["Remaining"] == "650" and row["rows"] == 2
    assert last_row(text, ["Spent"]) == {"Spent": "250", "rows": 2}


def test_json_check_with_secret(monkeypatch):
    seen = {}

    class Resp:
        def json(self):
            return {"queue": {"length": 150}, "state": "ok"}

    def fake_get(url, headers=None, params=None, **kw):
        seen["headers"] = headers
        return Resp()

    monkeypatch.setattr("checks.data.http_get", fake_get)
    server.upsert("api-q", {"check_spec": '{"type": "json", "url": "https://x", "headers": '
                                           '{"Authorization": "Bearer ${SECRET_TEST_KEY}"}, '
                                           '"fields": {"queued": "queue.length"}, "above": 100}'})
    server.run_check("api-q")
    row = server.to_dict(server._row("api-q"))
    assert seen["headers"]["Authorization"] == "Bearer s3cret"
    assert row["status"] == "yellow" and row["stats"]["queued"] == 150


def test_path_check(tmp_path):
    for i in range(3):
        (tmp_path / f"out{i}.nc").write_bytes(b"x" * 1000)
    (tmp_path / "notes.txt").write_text("hi")
    old = tmp_path / "old.nc"
    old.write_text("x")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    res = REGISTRY["path"].run({"path": str(tmp_path), "pattern": "*.nc"})
    assert res["status"] == "green" and res["stats"]["files"] == "4"
    assert REGISTRY["path"].run({"path": str(old), "max_age": "6h"})["status"] == "red"
    assert REGISTRY["path"].run({"path": str(tmp_path / "missing")})["status"] == "red"


def test_process_check():
    assert REGISTRY["process"].run({"name": "pytest"})["status"] == "green"
    assert REGISTRY["process"].run({"name": "no-such-process-xyz"})["status"] == "red"


def test_broken_check_turns_red(monkeypatch):
    def boom(spec):
        raise RuntimeError("upstream exploded")
    monkeypatch.setattr(REGISTRY["tcp"], "run", boom)
    server.upsert("broken", {"check_spec": '{"type": "tcp", "host": "x", "port": 1}'})
    server.run_check("broken")
    row = server._row("broken")
    assert row["status"] == "red" and "upstream exploded" in row["error"]
