from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-neo4j",
        action="store_true",
        default=False,
        help="Run tests that need a live Neo4j Enterprise instance (make up).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "neo4j: needs a live Neo4j instance")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-neo4j"):
        return
    skip = pytest.mark.skip(reason="needs a live Neo4j; run `make up` then `make rbac-check`")
    for item in items:
        if "neo4j" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def neo4j_uri() -> str:
    return os.environ.get("NEO4J_URI", "bolt://localhost:7687")


@pytest.fixture(scope="session")
def admin_auth() -> tuple[str, str]:
    return ("neo4j", os.environ.get("NEO4J_PASSWORD", "fincrimefincrime"))


@pytest.fixture(scope="session")
def demo_auth() -> tuple[str, str]:
    """The unprivileged role a customer demo actually runs as."""
    return ("analyst", os.environ.get("FINCRIME_DEMO_PASSWORD", "analystanalyst"))
