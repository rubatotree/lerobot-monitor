from pathlib import Path
from types import SimpleNamespace

from lerobot_monitor import model_hub
from lerobot_monitor.model_hub import ModelRegistry, parse_remote, search_hf_models
from lerobot_monitor.store import JsonStore


def test_parse_remote_accepts_repo_ids_urls_and_local_paths(tmp_path: Path) -> None:
    repo = parse_remote("lerobot/act_aloha")
    assert repo.source == "huggingface"
    assert repo.repo_id == "lerobot/act_aloha"

    url = parse_remote("https://huggingface.co/lerobot/act_aloha/tree/main")
    assert url.repo_id == "lerobot/act_aloha"
    assert url.revision == "main"

    local = tmp_path / "policy"
    local.mkdir()
    parsed = parse_remote(str(local))
    assert parsed.source == "local"
    assert parsed.path == str(local)


def test_search_hf_models_prefers_lerobot_filter(monkeypatch) -> None:
    calls: list[str | None] = []

    class FakeApi:
        def list_models(self, **kwargs):
            calls.append(kwargs.get("filter"))
            return [
                SimpleNamespace(
                    id="lerobot/act_aloha",
                    downloads=42,
                    likes=3,
                    last_modified="2026-09-20",
                    tags=["lerobot", "license:apache-2.0"],
                )
            ]

    monkeypatch.setattr(model_hub, "_hub_module", lambda: SimpleNamespace(HfApi=FakeApi))
    rows = search_hf_models("act", limit=5)

    assert calls == ["lerobot"]
    assert rows[0]["repo_id"] == "lerobot/act_aloha"
    assert rows[0]["tags"] == ["lerobot"]


def test_registry_upserts_duplicate_remote_and_updates_weights(tmp_path: Path, monkeypatch) -> None:
    store = JsonStore(tmp_path / "store.json")
    downloads: list[tuple[str, str]] = []

    def fake_download(repo_id: str, revision: str = "") -> str:
        downloads.append((repo_id, revision))
        path = tmp_path / f"{repo_id.replace('/', '--')}-{revision or 'latest'}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(model_hub, "download_hf_model", fake_download)
    registry = ModelRegistry(store, [])

    first = registry.register(remote="lerobot/act_aloha", name="ACT")
    second = registry.register(remote="https://huggingface.co/lerobot/act_aloha", name="ACT updated")

    assert first["id"] == second["id"]
    assert len(store.models()) == 1
    assert second["name"] == "ACT updated"
    assert second["repo_id"] == "lerobot/act_aloha"

    changed = registry.save(first["id"], {"remote": "lerobot/act_aloha_v2", "revision": "main"})
    assert changed["path"] == ""
    assert changed["missing"] is True

    updated = registry.update(first["id"])
    assert updated["path"]
    assert updated["repo_id"] == "lerobot/act_aloha_v2"
    assert downloads[-1] == ("lerobot/act_aloha_v2", "main")


def test_registry_uses_stable_id_for_local_model(tmp_path: Path) -> None:
    model_dir = tmp_path / "local-policy"
    model_dir.mkdir()
    store = JsonStore(tmp_path / "store.json")
    registry = ModelRegistry(store, [])

    first = registry.register(remote=str(model_dir), name="Local")
    second = registry.register(remote=str(model_dir), name="Local renamed")

    assert first["id"] == second["id"]
    assert len(store.models()) == 1
    assert second["source"] == "local"
    assert second["path"] == str(model_dir.resolve())
