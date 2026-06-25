"""Tests for freebuff2api/stealth.py — fingerprint generation + persistence."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from freebuff2api import stealth
from freebuff2api.stealth import (
    AccountFingerprint,
    DEFAULT_CLI_USER_AGENT,
    LEGACY_PREFIX,
    LEGACY_SUFFIX_LEN,
    clear_fingerprint,
    cli_user_agent,
    generate_legacy_fingerprint,
    generate_sdk_fingerprint,
    get_or_create_fingerprint,
    is_valid_fingerprint,
    load_fingerprint_store,
    rotate_fingerprint,
    save_fingerprint_store,
)


def test_legacy_fingerprint_format():
    fp = generate_legacy_fingerprint()
    assert fp.startswith(LEGACY_PREFIX)
    suffix = fp[len(LEGACY_PREFIX):]
    assert len(suffix) == LEGACY_SUFFIX_LEN
    # base64url alphabet only
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in suffix)


def test_legacy_fingerprint_unique():
    fps = {generate_legacy_fingerprint() for _ in range(50)}
    assert len(fps) == 50


def test_sdk_fingerprint_format():
    fp = generate_sdk_fingerprint()
    assert fp.startswith("codebuff-sdk-")
    assert len(fp) > len("codebuff-sdk-")


def test_is_valid_fingerprint_recognizes_legacy():
    assert is_valid_fingerprint(generate_legacy_fingerprint())


def test_is_valid_fingerprint_recognizes_enhanced():
    assert is_valid_fingerprint("enhanced-abc123")


def test_is_valid_fingerprint_recognizes_sdk():
    assert is_valid_fingerprint("codebuff-sdk-abc123")


def test_is_valid_fingerprint_rejects_old_fb_format():
    # The old format we used to send — invalid upstream
    assert not is_valid_fingerprint("fb-0123456789abcdef")


def test_is_valid_fingerprint_rejects_none_and_empty():
    assert not is_valid_fingerprint(None)
    assert not is_valid_fingerprint("")
    assert not is_valid_fingerprint("random")


def test_cli_user_agent_default():
    assert cli_user_agent() == DEFAULT_CLI_USER_AGENT
    assert "codebuff/" in DEFAULT_CLI_USER_AGENT


def test_cli_user_agent_env_override(monkeypatch):
    monkeypatch.setenv("FREEBUFF_CLI_USER_AGENT", "codebuff/9.9.9")
    assert cli_user_agent() == "codebuff/9.9.9"


def test_get_or_create_fingerprint_persists(tmp_path, monkeypatch):
    store = tmp_path / "fingerprints.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(store))
    token = "tok-1"
    fp1 = get_or_create_fingerprint(token)
    assert isinstance(fp1, AccountFingerprint)
    assert is_valid_fingerprint(fp1.fingerprint_id)
    assert fp1.user_agent == DEFAULT_CLI_USER_AGENT
    assert store.exists()
    # Second call returns the SAME fingerprint (stable per token)
    fp2 = get_or_create_fingerprint(token)
    assert fp2.fingerprint_id == fp1.fingerprint_id
    assert fp2.account_id == fp1.account_id


def test_get_or_create_fingerprint_different_tokens_different_fps(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "fp.json"))
    fp1 = get_or_create_fingerprint("tok-A")
    fp2 = get_or_create_fingerprint("tok-B")
    assert fp1.fingerprint_id != fp2.fingerprint_id
    assert fp1.account_id != fp2.account_id


def test_get_or_create_fingerprint_no_persist(tmp_path, monkeypatch):
    store = tmp_path / "fp.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(store))
    fp = get_or_create_fingerprint("tok-1", persist=False)
    assert is_valid_fingerprint(fp.fingerprint_id)
    assert not store.exists()


def test_get_or_create_fingerprint_custom_user_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "fp.json"))
    fp = get_or_create_fingerprint("tok-1", user_agent="codebuff/2.0.0")
    assert fp.user_agent == "codebuff/2.0.0"


def test_rotate_fingerprint_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "fp.json"))
    token = "tok-1"
    fp1 = get_or_create_fingerprint(token)
    fp2 = rotate_fingerprint(token)
    assert fp1.fingerprint_id != fp2.fingerprint_id
    assert is_valid_fingerprint(fp2.fingerprint_id)
    # Store now has the new fingerprint
    fp3 = get_or_create_fingerprint(token)
    assert fp3.fingerprint_id == fp2.fingerprint_id


def test_clear_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "fp.json"))
    token = "tok-1"
    get_or_create_fingerprint(token)
    assert clear_fingerprint(token) is True
    # Second clear returns False (already gone)
    assert clear_fingerprint(token) is False


def test_clear_fingerprint_unknown_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "fp.json"))
    assert clear_fingerprint("never-seen") is False


def test_save_load_store_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "fp.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(path))
    store = {"abc": {"fingerprint_id": "codebuff-cli-XXXXXXXX", "created_at": 1.0}}
    save_fingerprint_store(store)
    loaded = load_fingerprint_store()
    assert loaded == store
    # File should be 0600
    assert (path.stat().st_mode & 0o777) == 0o600


def test_load_store_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(tmp_path / "nope.json"))
    assert load_fingerprint_store() == {}


def test_load_store_corrupt_returns_empty(tmp_path, monkeypatch):
    path = tmp_path / "fp.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(path))
    path.write_text("{not json", encoding="utf-8")
    assert load_fingerprint_store() == {}


def test_get_or_create_fingerprint_handles_corrupt_store(tmp_path, monkeypatch):
    path = tmp_path / "fp.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(path))
    path.write_text("{not json", encoding="utf-8")
    # Should not raise — generates fresh and overwrites
    fp = get_or_create_fingerprint("tok-1")
    assert is_valid_fingerprint(fp.fingerprint_id)
    # Store is now valid JSON with the new entry
    loaded = load_fingerprint_store()
    assert "tok-1" not in loaded  # keyed by account_id hash, not token
    assert len(loaded) == 1


def test_fingerprint_store_path_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "store.json"
    monkeypatch.setenv("FREEBUFF_FINGERPRINT_STORE", str(custom))
    assert stealth.fingerprint_store_path() == custom


def test_fingerprint_store_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv("FREEBUFF_FINGERPRINT_STORE", raising=False)
    expected = Path.home() / ".config" / "freebaf-re" / "fingerprints.json"
    assert stealth.fingerprint_store_path() == expected
