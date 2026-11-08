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


def test_report_footer_present_and_suppressible(monkeypatch):
    """The probe report carries a short, price-free 'Work with me' pointer, suppressible via env."""
    from ownerwatch_mcp import server
    monkeypatch.delenv(server.NO_FOOTER_ENV, raising=False)
    f = server._footer()
    assert f.startswith("\n\n---\n")
    assert "Work with me" in f
    assert "github.com/imal1ice/ownerwatch" in f
    assert "$" not in f  # no price in outreach
    monkeypatch.setenv(server.NO_FOOTER_ENV, "1")
    assert server._footer() == ""
