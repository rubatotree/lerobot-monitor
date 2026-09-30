from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_upload_hf_model_uses_env_token_and_write_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
    monkeypatch.delenv("HF_UPLOAD_ENDPOINT", raising=False)
    monkeypatch.setenv("HF_TOKEN", "hf_env_token")
    calls: list[tuple] = []

    class FakeApi:
        def __init__(self, endpoint=None, token=None):
            self.endpoint = endpoint
            self.token = token
            calls.append(("api", endpoint, token))

        @staticmethod
        def whoami():
            return {"name": "tester"}

        @staticmethod
        def create_repo(repo_id, *, repo_type, exist_ok):
            calls.append(("create_repo", repo_id, repo_type, exist_ok))

        @staticmethod
        def upload_folder(*, repo_id, repo_type, folder_path, revision):
            calls.append(("upload_folder", repo_id, repo_type, folder_path, revision))

    monkeypatch.setattr(model_hub, "_hub_module", lambda: SimpleNamespace(HfApi=FakeApi))
    model_hub.upload_hf_model("user/policy", str(tmp_path), "")

    assert calls == [
        ("api", "https://huggingface.co", "hf_env_token"),
        ("create_repo", "user/policy", "model", True),
        ("upload_folder", "user/policy", "model", str(tmp_path), None),
    ]


def test_upload_hf_model_reports_rejected_token_before_uploading(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_revoked")
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_UPLOAD_ENDPOINT", raising=False)
    uploaded: list[str] = []

    class FakeApi:
        def __init__(self, endpoint=None, token=None):
            self.endpoint = endpoint
            self.token = token

        @staticmethod
        def whoami():
            raise RuntimeError("401 Client Error. Invalid username or password.")

        @staticmethod
        def upload_folder(**kwargs):
            uploaded.append("upload_folder")

    monkeypatch.setattr(model_hub, "_hub_module", lambda: SimpleNamespace(HfApi=FakeApi))
    with pytest.raises(model_hub.ModelHubError) as caught:
        model_hub.upload_hf_model("user/policy", str(tmp_path), "")

    message = str(caught.value)
    assert "https://huggingface.co" in message
    assert "HF_TOKEN" in message
    assert uploaded == []


def test_search_hf_models_prefers_lerobot_filter(monkeypatch) -> None:
    calls: list[str | None] = []

    class FakeApi:
        def list_models(self, *, search, filter, limit, sort):
            # Mirrors huggingface_hub 1.x, which has no `direction` argument:
            # passing one raises TypeError exactly like the real client did.
            assert (search, filter, limit, sort) == ("act", "lerobot", 5, "downloads")
            calls.append(filter)
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
    assert changed["path"].endswith("lerobot--act_aloha_v2-main")
    assert changed["playable"] is False

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


def test_registry_uploads_registered_model(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "policy"
    model_dir.mkdir()
    store = JsonStore(tmp_path / "store.json")
    registry = ModelRegistry(store, [])
    row = registry.register(remote=str(model_dir), name="Local")
    row = store.put_model({**row, "repo_id": "user/local-policy"})

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        model_hub,
        "upload_hf_model",
        lambda repo_id, path, revision="": calls.append((repo_id, path, revision)),
    )

    uploaded = registry.upload(row["id"])
    assert uploaded["repo_id"] == "user/local-policy"
    assert calls == [("user/local-policy", str(model_dir.resolve()), "")]
