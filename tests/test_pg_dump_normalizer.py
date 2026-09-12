from __future__ import annotations

import pytest

from scripts.normalize_pg_dump import canonicalize_pg_dump


def test_removes_only_matching_pg_dump_restriction_wrapper() -> None:
    raw = (
        "-- PostgreSQL database dump\n"
        "\\restrict abc123_RANDOM-token\n"
        "CREATE TABLE public.example (id integer);\n"
        "-- text containing \\restrict must remain\n"
        "SELECT '\\unrestrict not-a-wrapper';\n"
        "\\unrestrict abc123_RANDOM-token\n"
        "-- PostgreSQL database dump complete\n"
    )

    assert canonicalize_pg_dump(raw) == (
        "-- PostgreSQL database dump\n"
        "CREATE TABLE public.example (id integer);\n"
        "-- text containing \\restrict must remain\n"
        "SELECT '\\unrestrict not-a-wrapper';\n"
        "-- PostgreSQL database dump complete\n"
    )


def test_dump_without_wrapper_is_unchanged() -> None:
    raw = "CREATE TABLE public.example (id integer);\n"
    assert canonicalize_pg_dump(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "\\restrict first\nCREATE TABLE x(id integer);\n",
        "CREATE TABLE x(id integer);\n\\unrestrict first\n",
        "\\restrict first\n\\restrict second\n\\unrestrict first\n",
        "\\restrict first\n\\unrestrict second\n",
    ],
)
def test_malformed_or_mismatched_wrapper_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_pg_dump(raw)
