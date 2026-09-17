"""Pure, deterministic payload and content fingerprint vectors."""

import hashlib

import pytest

from news.domain.fingerprints import (
    content_fingerprint,
    fingerprint_input_length,
    payload_hash,
)


def test_payload_hash_uses_canonical_utf8_json():
    first = {"b": 2, "a": "Título"}
    reordered = {"a": "Título", "b": 2}
    expected = hashlib.sha256(b'{"a":"T\xc3\xadtulo","b":2}').hexdigest()
    assert payload_hash(first) == payload_hash(reordered) == expected
    assert payload_hash({"a": "Títula", "b": 2}) != expected


def test_payload_hash_sorts_nested_mapping_keys():
    assert payload_hash({"nested": {"b": 2, "a": 1}}) == payload_hash({"nested": {"a": 1, "b": 2}})


@pytest.mark.parametrize("bad", [None, [1, 2], {"value": object()}, {"n": float("nan")}])
def test_payload_hash_rejects_unsupported_input(bad):
    with pytest.raises((TypeError, ValueError)):
        payload_hash(bad)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("Título", "a b", ""), ("TÍTULO", "a   b", "")),
        (("Ｔｉｔｌｅ", "a b", ""), ("title", "a\n\tb", "")),
        (("Title", "", "a b"), ("TITLE", "", "a\t\nb")),
        (("Title", "a b", "ignored"), ("title", "a b", "different")),
    ],
)
def test_equivalent_content_vectors(left, right):
    assert content_fingerprint(*left) == content_fingerprint(*right)
    assert fingerprint_input_length(*left) == fingerprint_input_length(*right)


def test_body_changes_and_case_remain_significant():
    assert content_fingerprint("Title", "body one", "") != content_fingerprint(
        "Title", "body two", ""
    )
    assert content_fingerprint("Title", "Body", "") != content_fingerprint("Title", "body", "")


def test_description_is_used_only_for_empty_body():
    assert content_fingerprint("Title", "", "description") != content_fingerprint(
        "Title", "body", "description"
    )
    assert content_fingerprint("Title", "body", "first") == content_fingerprint(
        "Title", "body", "second"
    )


def test_fingerprint_input_length_matches_hashed_material():
    args = ("Ｔｉｔｌｅ", "a  b\n c", "unused")
    material = "title\na b c"
    assert fingerprint_input_length(*args) == len(material)
    assert content_fingerprint(*args) == hashlib.sha256(material.encode("utf-8")).hexdigest()
    assert content_fingerprint(*args) == content_fingerprint(*args)
