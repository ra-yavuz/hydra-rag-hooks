from pathlib import Path

import yaml

from hydra_rag_hooks import config as config_mod


def test_defaults_when_missing(tmp_path: Path):
    cfg = config_mod.load(tmp_path / "missing.yaml")
    assert cfg.get("top_k") == 5
    assert cfg.get("embedder", "kind") == "fastembed"


def test_lax_trigger_is_default_on():
    """Lax `rag <text>` triggers on by default; the colon is opt-in for
    users who hit false positives. Tests the resolved trigger list and
    the flag separately."""
    cfg = config_mod.Config()
    triggers = config_mod.triggers(cfg)
    assert "rag " in triggers
    assert cfg.get("lax_trigger") is True


def test_user_override_merges(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "top_k": 9,
        "embedder": {"model": "custom-model"},
    }))
    cfg = config_mod.load(p)
    assert cfg.get("top_k") == 9
    assert cfg.get("embedder", "model") == "custom-model"
    assert cfg.get("embedder", "kind") == "fastembed"  # default preserved


def test_set_and_save(tmp_path: Path):
    p = tmp_path / "config.yaml"
    cfg = config_mod.Config()
    cfg.set("embedder.kind", "http")
    cfg.set("top_k", 7)
    cfg.save(p)
    re = config_mod.load(p)
    assert re.get("embedder", "kind") == "http"
    assert re.get("top_k") == 7
