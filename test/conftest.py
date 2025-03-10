from __future__ import annotations

import logging
import os
import pathlib
import sys
from logging import getLogger as get_logger
from typing import Any, Generic, TypeVar

import pytest
from typing_extensions import NamedTuple  # For Generic NamedTuples
from typing_extensions import Literal

pytest.register_assert_rewrite("test.testutils")

# List of simple attributes to use in test:
simple_arguments: list[tuple[type, Any, Any]] = [
    # type, passed value, expected (parsed) value
    (int, "123", 123),
    (int, 123, 123),
    (int, "-1", -1),
    (float, "123.0", 123.0),
    (float, "'0.0'", 0.0),
    (float, "0.123", 0.123),
    (float, "0.123", 0.123),
    (float, 0.123, 0.123),
    (float, 123, 123.0),
    (bool, "True", True),
    (bool, "False", False),
    (bool, "true", True),
    (bool, "false", False),
    (bool, "yes", True),
    (bool, "no", False),
    (bool, "T", True),
    (bool, "F", False),
    (str, "bob", "bob"),
    (str, "'bob'", "bob"),
    (str, "''", ""),
    (str, "[123]", "[123]"),
    (str, "123", "123"),
]


class SimpleAttributeTuple(NamedTuple):
    field_type: type
    passed_cmdline_value: str
    expected_value: Any


@pytest.fixture(params=simple_arguments)
def simple_attribute(request):
    """Test fixture that produces an tuple of (type, passed value, expected value)"""
    some_type, passed_value, expected_value = request.param
    logging.debug(
        f"Attribute type: {some_type}, passed value: '{passed_value}', expected: '{expected_value}'"
    )
    return SimpleAttributeTuple(
        field_type=some_type, passed_cmdline_value=passed_value, expected_value=expected_value
    )


T = TypeVar("T")


class SimpleAttributeWithDefault(NamedTuple, Generic[T]):
    field_type: type[T]
    passed_cmdline_value: str
    expected_value: T
    default_value: T


# TODO: Also add something like `[Optional[t] for t in simple_arguments]`!
default_values_for_type = {int: [0, -111], str: ["bob", ""], float: [0.0, 1e2], bool: [True, False]}


@pytest.fixture(
    params=[
        SimpleAttributeWithDefault(some_type, passed_value, expected_value, default_value)
        for some_type, passed_value, expected_value in simple_arguments
        for default_value in default_values_for_type[some_type]
    ]
)
def simple_attribute_with_default(request: pytest.FixtureRequest):
    return request.param


@pytest.fixture(autouse=True, params=["simple", "verbose"])
def simple_and_advanced_api(request, monkeypatch):
    api: Literal["simple", "verbose"] = request.param
    monkeypatch.setitem(os.environ, "SIMPLE_PARSING_API", api)
    yield


@pytest.fixture
def assert_equals_stdout(capsys):
    def strip(string):
        return "".join(string.split())

    def should_equal(expected: str, file_path: str | None = None):
        if "optional arguments" in expected and sys.version_info >= (3, 10):
            expected = expected.replace("optional arguments", "options")
        out = capsys.readouterr().out
        assert strip(out) == strip(expected), file_path

    return should_equal


@pytest.fixture(scope="session", autouse=True)
def setup_logging():
    project_logger = get_logger("simple_parsing")
    # project_logger.setLevel(
    #     logging.DEBUG
    #     if "-vv" in sys.argv
    #     else logging.INFO
    #     if "-v" in sys.argv
    #     else logging.WARNING
    # )
    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setFormatter(
        logging.Formatter(
            "%(levelname)s {%(pathname)s:%(lineno)d} - %(message)s",
            "%m-%d %H:%M:%S"
            # "%(asctime)-15s::%(levelname)s::%(pathname)s::%(lineno)d::%(message)s"
        )
    )
    project_logger.addHandler(ch)


@pytest.fixture(scope="module")
def parser():
    from .testutils import TestParser

    _parser = TestParser()
    return _parser


@pytest.fixture
def no_stdout(capsys, caplog):
    """Asserts that no output was produced on stdout.

    Args:
        capsys (pytest.fixture): The capsys fixture
    """
    with caplog.at_level(logging.DEBUG):
        yield
    captured = capsys.readouterr()
    if captured.out != "":
        pytest.fail(f"Test generated some output in stdout: '{captured.out}'")
    if captured.err != "":
        pytest.fail(f"Test generated some output in stderr: '{captured.err}'")


@pytest.fixture
def no_warnings(caplog):
    yield
    for when in ("setup", "call"):
        messages = [x.message for x in caplog.get_records(when) if x.levelno == logging.WARNING]
        if messages:
            pytest.fail(f"warning messages encountered during testing: {messages}")


@pytest.fixture
def silent(no_stdout, no_warnings):
    """
    Test fixture that will make a test fail if it prints anything to stdout or
    logs warnings
    """


@pytest.fixture
def logs_warning(caplog):
    yield
    messages = [x.message for x in caplog.get_records("call") if x.levelno == logging.WARNING]
    if not messages:
        pytest.fail(f"No warning messages were logged: {messages}")


def pytest_ignore_collect(collection_path: pathlib.Path):
    # We can only test the decorator on Py38+.
    # TODO: Remove this when we drop support for Py37
    if sys.version_info < (3, 8) and collection_path.stem == "test_decorator":
        return True
    return False
