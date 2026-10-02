"""Capture file layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from capquery.paths import capture_file, captures_root, migrations_file, sanitize, split_nodeid, state_file

ROOT = Path("/project")


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        (
            "tests/shop/test_orders.py::test_create",
            "tests/captures/shop/test_orders.py/test_create.yaml",
        ),
        (
            "tests/shop/test_orders.py::TestOrders::test_create",
            "tests/captures/shop/test_orders.py/TestOrders.test_create.yaml",
        ),
        (
            "tests/shop/test_orders.py::test_create[1-2]",
            "tests/captures/shop/test_orders.py/test_create[1-2].yaml",
        ),
        (
            "tests/shop/nested/deep/test_x.py::test_y",
            "tests/captures/shop/nested/deep/test_x.py/test_y.yaml",
        ),
        ("test_root_level.py::test_x", "captures/test_root_level.py/test_x.yaml"),
    ],
)
def test_capture_file_layout(nodeid, expected):
    assert capture_file(ROOT, nodeid) == ROOT / expected


def test_captures_directory_sits_next_to_the_test_directory():
    assert captures_root(ROOT, "tests/shop/test_orders.py") == ROOT / "tests" / "captures"
    assert captures_root(ROOT, "test_root_level.py") == ROOT / "captures"


def test_migrations_and_state_files():
    assert migrations_file(ROOT) == ROOT / "captures" / "migrations.yaml"
    assert state_file(ROOT) == ROOT / "captures" / ".capquery-state.yaml"


def test_split_nodeid():
    assert split_nodeid("tests/a.py::test_b") == ("tests/a.py", "test_b")
    assert split_nodeid("tests/a.py") == ("tests/a.py", "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("test_simple", "test_simple"),
        ("test[1]", "test[1]"),
        ("Test::test", "Test__test"),
        ("weird/name with spaces", "weird_name_with_spaces"),
        ("colon:name", "colon_name"),
        ("", "_"),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_never_produces_a_path_separator():
    for raw in ("a/b", "a\\b", "a:b", "a?b", "a*b"):
        assert "/" not in sanitize(raw)
        assert "\\" not in sanitize(raw)


def test_module_and_test_separator_is_normalized_before_sanitizing():
    # capture_file() turns "Test::test" into "Test.test" before sanitizing it
    from capquery.paths import capture_file

    path = capture_file(ROOT, "tests/test_a.py::Test::test_x")
    assert path.name == "Test.test_x.yaml"
    assert path.parent.name == "test_a.py"
