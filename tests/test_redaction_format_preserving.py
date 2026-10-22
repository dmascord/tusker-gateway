"""Format-preserving authenticated redaction regression tests."""
import re
import pytest
from tusker_gateway.redaction import Tokenizer, RedactionRuleSet, _BufferedRestorer

KEY = b"0123456789abcdef0123456789abcdef"


def tokenizer():
    return Tokenizer(KEY, RedactionRuleSet(format_preserving_aliases=True))


@pytest.mark.parametrize("original", [
    "alice@example.com", "a.b+tag@sub.example.org",
    "person_123@example-observer.test",
])
def test_email_alias_is_valid_stable_and_reversible(original):
    t = tokenizer()
    alias = t.tokenize_text(original)
    assert alias != original
    assert alias.endswith(".redact.invalid")
    assert re.fullmatch(r"[^@\\s]+@[^@\\s]+", alias)
    assert t.tokenize_text(original) == alias
    assert t.tokenize_text(alias) == alias
    assert t.restore_text(alias) == original


@pytest.mark.parametrize("original", [
    "sk-" + "a" * 35,
    "ghp_" + "b" * 35,
    "gho_" + "c" * 35,
    "github_pat_" + "d" * 35,
    "npm_" + "e" * 35,
    "xoxb-" + "f" * 35,
    "AIza" + "g" * 35,
])
def test_key_alias_preserves_prefix_and_restores(original):
    t = tokenizer()
    alias = t.tokenize_text(original)
    assert alias != original
    assert alias.startswith(original[:4])
    assert t.tokenize_text(alias) == alias
    assert t.restore_text(alias) == original


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 19, 4096])
def test_streamed_email_and_key_alias_round_trip(chunk):
    t = tokenizer()
    original = "Contact alice@example.com using " + "sk-" + "a" * 35 + " now!"
    wire = t.tokenize_text(original)
    r = _BufferedRestorer(t)
    actual = "".join(r.feed(wire[i:i+chunk]) for i in range(0, len(wire), chunk)) + r.flush()
    assert actual == original


def test_wrong_key_and_modified_alias_do_not_restore():
    t = tokenizer()
    alias = t.tokenize_text("alice@example.com")
    other = Tokenizer(b"x" * 32, RedactionRuleSet(format_preserving_aliases=True))
    assert other.restore_text(alias) == alias
    mutated = alias.replace("u@", "x@")
    assert t.restore_text(mutated) == mutated


def test_legacy_mode_remains_default():
    t = Tokenizer(KEY, RedactionRuleSet())
    assert t.tokenize_text("alice@example.com").startswith("[TUSKER:email:")


def test_mixed_old_and_new_tokens_restore():
    old = Tokenizer(KEY, RedactionRuleSet())
    new = tokenizer()
    original = "alice@example.com"
    assert new.restore_text(old.tokenize_text(original) + " " + new.tokenize_text(original)) == original + " " + original
