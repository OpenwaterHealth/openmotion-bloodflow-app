"""Critical-error code registry — pure data, no hardware or Qt."""

import pytest

import error_codes

pytestmark = pytest.mark.unit


def test_lookup_known_code_returns_entry():
    err = error_codes.lookup("E-101")
    assert err.code == "E-101"
    assert err.category == "startup"
    assert err.title  # non-empty
    assert err.message
    assert err.suggested_action


def test_registry_entries_are_complete_and_unique():
    codes = list(error_codes.ERROR_CODES.keys())
    assert codes, "registry is empty"
    assert len(codes) == len(set(codes)), "duplicate codes"
    for code, err in error_codes.ERROR_CODES.items():
        assert err.code == code, f"key/value code mismatch for {code}"
        assert err.title.strip()
        assert err.message.strip()
        assert err.suggested_action.strip()
        assert err.category in {"startup", "safety", "scan"}


def test_category_matches_code_prefix():
    prefix_to_category = {"E-1": "startup", "E-2": "safety", "E-3": "scan"}
    for code, err in error_codes.ERROR_CODES.items():
        prefix = code[:3]
        assert prefix in prefix_to_category, f"unexpected prefix for {code}"
        assert err.category == prefix_to_category[prefix], (
            f"{code} category {err.category!r} != {prefix_to_category[prefix]!r}"
        )


def test_lookup_unknown_code_returns_generic_fallback():
    err = error_codes.lookup("E-999")
    # Never raise inside an error path — return a usable generic entry.
    assert err.code == "E-999"
    assert err.title.strip()
    assert err.message.strip()


def test_expected_catalog_codes_present():
    expected = {
        "E-101", "E-102", "E-103", "E-104", "E-105", "E-106",
        "E-201", "E-202",
        "E-301", "E-302",
    }
    assert expected == set(error_codes.ERROR_CODES.keys())
