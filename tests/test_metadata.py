import json
from pathlib import Path

import pytest

import ownerwatch_mcp

tomllib = pytest.importorskip("tomllib")   # Python 3.11+

ROOT = Path(__file__).resolve().parent.parent


def test_versions_and_registry_name_agree():
    """The MCP Registry checks the PyPI README for `mcp-name: <server.json name>`; the versions must agree too."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    server = json.loads((ROOT / "server.json").read_text())
    assert project["version"] == ownerwatch_mcp.__version__ == server["version"] == server["packages"][0]["version"]
    assert server["packages"][0]["identifier"] == project["name"]
    assert f"<!-- mcp-name: {server['name']} -->" in (ROOT / "README.md").read_text()
    assert len(server["description"]) <= 100
