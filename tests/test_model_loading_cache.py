"""Cached backbone metadata must not require a second copy of policy weights."""

from pathlib import Path

import pytest

from lerobot_monitor.policy import resolve_cached_vlm_path


@pytest.fixture
def vlm_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    constants = pytest.importorskip("huggingface_hub.constants")
    httpx = pytest.importorskip("httpx")
    cache = tmp_path / "hub"
    repo = cache / "models--owner--backbone"
    snapshot = repo / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text(snapshot.name, encoding="utf-8")
    (snapshot / "config.json").write_text('{"model_type":"smolvlm"}', encoding="utf-8")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))

    def reject_http(*args: object, **kwargs: object) -> None:
        pytest.fail("Cache resolution attempted HTTP")

    monkeypatch.setattr(httpx.Client, "send", reject_http)
    return snapshot


def test_metadata_only_backbone_uses_local_snapshot(vlm_snapshot: Path) -> None:
    assert resolve_cached_vlm_path("owner/backbone", require_weights=False) == str(vlm_snapshot.resolve())
    assert resolve_cached_vlm_path("owner/backbone", require_weights=True) is None


def test_weighted_backbone_does_not_require_robot_features(vlm_snapshot: Path) -> None:
    (vlm_snapshot / "model.safetensors").write_bytes(b"weights")
    assert resolve_cached_vlm_path("owner/backbone", require_weights=True) == str(vlm_snapshot.resolve())


def test_incomplete_sharded_backbone_is_rejected(vlm_snapshot: Path) -> None:
    (vlm_snapshot / "model.safetensors.index.json").write_text(
        '{"weight_map":{"a":"part1.safetensors","b":"part2.safetensors"}}', encoding="utf-8",
    )
    (vlm_snapshot / "part1.safetensors").write_bytes(b"weights")
    assert resolve_cached_vlm_path("owner/backbone", require_weights=True) is None
    (vlm_snapshot / "part2.safetensors").write_bytes(b"weights")
    assert resolve_cached_vlm_path("owner/backbone", require_weights=True) == str(vlm_snapshot.resolve())


def test_missing_backbone_does_not_download(vlm_snapshot: Path) -> None:
    assert resolve_cached_vlm_path("owner/missing", require_weights=False) is None


def test_local_backbone_requires_config(vlm_snapshot: Path) -> None:
    assert resolve_cached_vlm_path(str(vlm_snapshot), require_weights=False) == str(vlm_snapshot.resolve())
    (vlm_snapshot / "config.json").unlink()
    assert resolve_cached_vlm_path(str(vlm_snapshot), require_weights=False) is None


def test_cache_follows_main_ref_not_newest_directory(vlm_snapshot: Path) -> None:
    other = vlm_snapshot.parent / ("b" * 40)
    other.mkdir()
    (other / "config.json").write_text("{}", encoding="utf-8")
    assert resolve_cached_vlm_path("owner/backbone", require_weights=False) == str(vlm_snapshot.resolve())
