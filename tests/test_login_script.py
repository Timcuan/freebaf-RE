"""Tests for scripts/login.py token store + env-append logic."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


def _load_login_module(tmp_path: Path):
    """Load scripts/login.py as a module with the package root on sys.path."""
    src = Path(__file__).resolve().parent.parent / "scripts" / "login.py"
    spec = importlib.util.spec_from_file_location("freebaf_login", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebaf_login"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_store_roundtrip(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    store = tmp_path / "tokens.json"
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(store))

    entries = []
    stored = mod.StoredToken(
        index=0,
        user_id="u1",
        email="a@x.com",
        name="A",
        auth_token="tok-1",
        fingerprint_id="fb-1",
        fingerprint_hash="hash-1",
        added_at=time.time(),
        source="freebuff",
    )
    entries.append(mod.asdict(stored))
    path = mod.save_store(entries)
    assert path == store
    assert store.exists()
    mode = store.stat().st_mode & 0o777
    assert mode == 0o600

    loaded = mod.load_store()
    assert len(loaded) == 1
    assert loaded[0]["auth_token"] == "tok-1"
    assert loaded[0]["email"] == "a@x.com"


def test_store_missing_returns_empty(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(tmp_path / "nope.json"))
    assert mod.load_store() == []


def test_store_corrupt_returns_empty(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    store = tmp_path / "tokens.json"
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(store))
    store.write_text("{not json", encoding="utf-8")
    assert mod.load_store() == []


def test_append_env_creates_file(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    path = mod.append_env("tok-A")
    assert path == tmp_path / ".env"
    content = path.read_text(encoding="utf-8")
    assert "FREEBUFF_TOKEN=tok-A" in content


def test_append_env_appends_comma_separated(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("FREEBUFF_TOKEN=tok-A\nOTHER=v\n", encoding="utf-8")
    mod.append_env("tok-B")
    content = env.read_text(encoding="utf-8")
    lines = content.splitlines()
    token_line = next(l for l in lines if l.startswith("FREEBUFF_TOKEN="))
    assert token_line == "FREEBUFF_TOKEN=tok-A,tok-B"
    assert "OTHER=v" in lines


def test_append_env_dedupes(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("FREEBUFF_TOKEN=tok-A,tok-B\n", encoding="utf-8")
    mod.append_env("tok-A")
    content = env.read_text(encoding="utf-8")
    token_line = next(l for l in content.splitlines() if l.startswith("FREEBUFF_TOKEN="))
    assert token_line.count("tok-A") == 1
    assert token_line.count("tok-B") == 1
    assert set(token_line[len("FREEBUFF_TOKEN="):].split(",")) == {"tok-A", "tok-B"}


def test_cmd_list_empty(tmp_path, monkeypatch, capsys):
    mod = _load_login_module(tmp_path)
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(tmp_path / "tokens.json"))
    rc = mod.cmd_list(type("A", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "no tokens stored" in out


def test_cmd_export_with_entries(tmp_path, monkeypatch, capsys):
    mod = _load_login_module(tmp_path)
    store = tmp_path / "tokens.json"
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(store))
    entries = [
        {"index": 0, "auth_token": "tok-A", "email": "a@x.com"},
        {"index": 1, "auth_token": "tok-B", "email": "b@x.com"},
    ]
    store.write_text(json.dumps(entries), encoding="utf-8")
    rc = mod.cmd_export(type("A", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "FREEBUFF_TOKEN=tok-A,tok-B" in out


def test_cmd_remove_reindexes(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    store = tmp_path / "tokens.json"
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(store))
    entries = [
        {"index": 0, "auth_token": "tok-A", "email": "a@x.com"},
        {"index": 1, "auth_token": "tok-B", "email": "b@x.com"},
        {"index": 2, "auth_token": "tok-C", "email": "c@x.com"},
    ]
    store.write_text(json.dumps(entries), encoding="utf-8")
    args = type("A", (), {"index": 1})()
    rc = mod.cmd_remove(args)
    assert rc == 0
    loaded = mod.load_store()
    assert len(loaded) == 2
    assert [e["auth_token"] for e in loaded] == ["tok-A", "tok-C"]
    assert [e["index"] for e in loaded] == [0, 1]


def test_cmd_remove_out_of_range(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(tmp_path / "tokens.json"))
    args = type("A", (), {"index": 5})()
    rc = mod.cmd_remove(args)
    assert rc == 1


def test_token_store_path_override(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    custom = tmp_path / "custom" / "store.json"
    monkeypatch.setenv("FREEBUFF_TOKEN_STORE", str(custom))
    assert mod.token_store_path() == custom


def test_token_store_path_default(tmp_path, monkeypatch):
    mod = _load_login_module(tmp_path)
    monkeypatch.delenv("FREEBUFF_TOKEN_STORE", raising=False)
    # Should resolve to ~/.config/freebaf-re/tokens.json
    expected = Path.home() / ".config" / "freebaf-re" / "tokens.json"
    assert mod.token_store_path() == expected
