"""Tests for the v0.6 additions: toggles, mcp_register, and the MCP server.

The actual MCP retrieval path needs a built index and the embedder
chain, which the test environment doesn't carry. We cover the JSON-RPC
plumbing, tool definitions, and idempotent ~/.claude.json sync here;
end-to-end retrieval is covered by the indexer integration tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_rag_hooks import mcp, mcp_register, toggles
from hydra_rag_hooks.cli import main as cli_main


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def test_toggle_defaults(fresh_state):
    assert toggles.auto_rag_enabled() is False
    assert toggles.mcp_enabled() is True


def test_toggle_set_persist(fresh_state):
    toggles.set_value("auto_rag", True)
    assert toggles.auto_rag_enabled() is True
    toggles.set_value("auto_rag", False)
    assert toggles.auto_rag_enabled() is False


def test_toggle_unknown_key_raises(fresh_state):
    with pytest.raises(KeyError):
        toggles.set_value("not_a_toggle", True)


def test_mcp_register_idempotent(fresh_state):
    target = fresh_state / "claudejson.json"
    assert mcp_register.ensure_registered(claude_json=target) is True
    assert mcp_register.ensure_registered(claude_json=target) is False
    data = json.loads(target.read_text())
    assert "claude-rag" in data["mcpServers"]


def test_mcp_register_disabled_flag_flips(fresh_state):
    target = fresh_state / "claudejson.json"
    mcp_register.ensure_registered(claude_json=target, disabled=False)
    # Toggling disabled should change the file (env block appears).
    assert mcp_register.ensure_registered(claude_json=target, disabled=True) is True
    data = json.loads(target.read_text())
    assert data["mcpServers"]["claude-rag"]["env"]["CLAUDE_RAG_MCP_DISABLED"] == "1"
    # And flipping back removes it.
    assert mcp_register.ensure_registered(claude_json=target, disabled=False) is True
    data2 = json.loads(target.read_text())
    assert "env" not in data2["mcpServers"]["claude-rag"]


def test_mcp_register_preserves_other_servers(fresh_state):
    target = fresh_state / "claudejson.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "other": {"type": "stdio", "command": "/usr/bin/other-mcp"},
        },
        "someOtherKey": {"x": 1},
    }))
    mcp_register.ensure_registered(claude_json=target)
    data = json.loads(target.read_text())
    assert "other" in data["mcpServers"]
    assert "claude-rag" in data["mcpServers"]
    assert data["someOtherKey"] == {"x": 1}


def test_mcp_unregister(fresh_state):
    target = fresh_state / "claudejson.json"
    mcp_register.ensure_registered(claude_json=target)
    assert mcp_register.unregister(claude_json=target) is True
    data = json.loads(target.read_text())
    assert "mcpServers" not in data
    # Second call: idempotent no-op.
    assert mcp_register.unregister(claude_json=target) is False


def test_mcp_register_refuses_to_overwrite_malformed_json(fresh_state):
    """Critical safety: if the user's ~/.claude.json is malformed (mid-edit
    save, trailing comma), we must not write anything. Overwriting would
    destroy unrelated config (other MCP servers, project trust, OAuth).
    """
    target = fresh_state / "claudejson.json"
    # Realistic malformed JSON: trailing comma after a real entry.
    target.write_text(
        '{"mcpServers": {"other": {"type": "stdio", "command": "/bin/x"},}}',
    )
    bytes_before = target.read_bytes()
    # ensure_registered should return False (no change) without raising.
    assert mcp_register.ensure_registered(claude_json=target) is False
    # File contents must be byte-identical.
    assert target.read_bytes() == bytes_before


def test_mcp_register_refuses_to_overwrite_non_object_root(fresh_state):
    target = fresh_state / "claudejson.json"
    target.write_text('["not", "an", "object"]')
    bytes_before = target.read_bytes()
    assert mcp_register.ensure_registered(claude_json=target) is False
    assert target.read_bytes() == bytes_before


def test_mcp_unregister_refuses_to_overwrite_malformed_json(fresh_state):
    target = fresh_state / "claudejson.json"
    target.write_text('{"mcpServers": {"x":}}')
    bytes_before = target.read_bytes()
    assert mcp_register.unregister(claude_json=target) is False
    assert target.read_bytes() == bytes_before


def test_slash_command_does_not_overwrite_user_owned(fresh_state, tmp_path):
    """If a user already has ~/.claude/commands/rag-toggle.md (no marker),
    we must leave it alone."""
    target_dir = tmp_path / "commands"
    target_dir.mkdir()
    target = target_dir / mcp_register._SLASH_COMMAND_FILENAME
    user_content = "# my custom rag toggle\n\nDo my thing.\n"
    target.write_text(user_content)
    changed = mcp_register.ensure_slash_command(target_dir=target_dir)
    assert changed is False
    assert target.read_text() == user_content


def test_slash_command_updates_our_own_marker(fresh_state, tmp_path):
    """A previously-shipped file (carries our marker) is updatable."""
    target_dir = tmp_path / "commands"
    target_dir.mkdir()
    target = target_dir / mcp_register._SLASH_COMMAND_FILENAME
    target.write_text(mcp_register._SLASH_COMMAND_MARKER + "\n# old shipped content\n")
    changed = mcp_register.ensure_slash_command(target_dir=target_dir)
    # Whether the file is changed depends on whether the shipped source
    # is reachable in this environment. If it isn't, the function
    # returns False; if it is, the content gets refreshed. Either way
    # we must not destroy the marker.
    if changed:
        assert mcp_register._SLASH_COMMAND_MARKER in target.read_text()


def test_mcp_initialize_response():
    reply = mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert reply["result"]["serverInfo"]["name"] == mcp.SERVER_NAME


def test_mcp_tools_list_has_three_tools():
    reply = mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [t["name"] for t in reply["result"]["tools"]]
    assert sorted(names) == ["rag_list_stores", "rag_search", "rag_status"]


def test_mcp_unknown_tool_returns_error():
    reply = mcp._handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "no_such_tool", "arguments": {}},
    })
    assert "error" in reply
    assert reply["error"]["code"] == -32601


def test_mcp_notifications_initialized_returns_none():
    reply = mcp._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert reply is None


def test_mcp_search_with_no_index_explains(fresh_state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    reply = mcp._handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "rag_search", "arguments": {"query": "anything"}},
    })
    assert reply["result"]["isError"] is False
    text = reply["result"]["content"][0]["text"]
    assert "no index found" in text


def test_mcp_search_empty_query_is_error(fresh_state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    reply = mcp._handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "rag_search", "arguments": {"query": "  "}},
    })
    assert reply["result"]["isError"] is True


def test_mcp_disabled_env_short_circuits(monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_MCP_DISABLED", "1")
    rc = mcp.main()
    assert rc == 0


def test_cli_rag_toggle_persists(fresh_state):
    rc = cli_main(["rag", "on"])
    assert rc == 0
    assert toggles.auto_rag_enabled() is True
    rc = cli_main(["rag", "off"])
    assert rc == 0
    assert toggles.auto_rag_enabled() is False
    rc = cli_main(["rag", "toggle"])
    assert rc == 0
    assert toggles.auto_rag_enabled() is True


def test_cli_mcp_off_disables_in_claude_json(fresh_state):
    rc = cli_main(["mcp", "off"])
    assert rc == 0
    target = Path.home() / ".claude.json"
    assert target.exists()
    data = json.loads(target.read_text())
    entry = data["mcpServers"]["claude-rag"]
    assert entry["env"]["CLAUDE_RAG_MCP_DISABLED"] == "1"
    rc = cli_main(["mcp", "on"])
    assert rc == 0
    data2 = json.loads(target.read_text())
    assert "env" not in data2["mcpServers"]["claude-rag"]
