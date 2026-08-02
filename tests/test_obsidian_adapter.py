from __future__ import annotations

from pathlib import Path

import obsidian_adapter


def test_obsidian_health_uses_configured_vault(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "HOME.md").write_text("# Home\n", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_CODEX_FOLDER", "Codex")
    result = obsidian_adapter.health_check()
    assert result["status"] == "healthy"
    assert result["vault_path"] == str(vault.resolve())
    assert result["note_count"] == 1


def test_obsidian_write_note_stays_inside_codex_folder(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_CODEX_FOLDER", "Codex")
    path = obsidian_adapter.write_note("连接测试", "hello")
    assert path.parent == vault / "Codex"
    assert path.exists()
    assert "hello" in path.read_text(encoding="utf-8")
