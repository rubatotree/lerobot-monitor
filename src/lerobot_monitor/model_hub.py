"""Model library: scan local caches, register remote addresses, refresh weights.

Registered entries live in the JSON store, so the library survives restarts and
users can edit the remote address of any model. ``huggingface_hub`` is imported
lazily: the monitor also runs without LeRobot's dependencies installed, and in
that case only local paths and already-cached repos are available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .library import is_policy_dir, list_local_models, policy_config
from .store import JsonStore

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class ModelHubError(RuntimeError):
    """Raised when a model address cannot be parsed or resolved."""


@dataclass(frozen=True)
class ModelRemote:
    """A parsed model address: either a Hugging Face repo or a local directory."""

    source: str
    remote: str
    repo_id: str = ""
    path: str = ""
    revision: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    text = _SLUG.sub("-", str(value or "").strip()).strip("-.")
    return text[:120] or "model"


def _path_key(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def parse_remote(remote: str, *, revision: str = "") -> ModelRemote:
    """Parse an ``org/name`` repo id, a huggingface.co URL, or a local path."""
    text = str(remote or "").strip()
    if not text:
        raise ModelHubError("remote address is empty")
    if text.lower().startswith("hf:"):
        text = text[len("hf:") :].strip()
    if text.startswith(("http://", "https://")):
        return _parse_hf_url(text, revision=revision)
    if _REPO_ID.match(text) and not Path(text).expanduser().exists():
        return ModelRemote(source="huggingface", remote=remote.strip(), repo_id=text, revision=revision.strip())
    return ModelRemote(source="local", remote=remote.strip(), path=str(Path(text).expanduser()))


def _parse_hf_url(url: str, *, revision: str = "") -> ModelRemote:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in HF_HOSTS:
        raise ModelHubError(f"only huggingface.co model URLs are supported, got {parsed.netloc or url}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ModelHubError(f"not a model URL: {url}")
    repo_id = "/".join(parts[:2])
    if not _REPO_ID.match(repo_id):
        raise ModelHubError(f"not a model URL: {url}")
    found_revision = revision.strip()
    if not found_revision and len(parts) >= 4 and parts[2] in {"tree", "resolve"}:
        found_revision = parts[3]
    return ModelRemote(source="huggingface", remote=url, repo_id=repo_id, revision=found_revision)


def _hub_module() -> Any:
    try:
        import huggingface_hub  # noqa: PLC0415 - optional, LeRobot brings it in
    except ImportError as exc:
        raise ModelHubError(
            "huggingface_hub is not installed in this environment; use a local path instead"
        ) from exc
    return huggingface_hub


def search_hf_models(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Search the Hub, preferring LeRobot policies but falling back to all models."""
    text = str(query or "").strip()
    if not text:
        raise ModelHubError("search query is empty")
    api = _hub_module().HfApi()

    def fetch(tag: str | None) -> list[Any]:
        # huggingface_hub 1.x dropped the `direction` argument; the Hub API
        # already returns `sort="downloads"` in descending order.
        return list(api.list_models(search=text, filter=tag, limit=limit, sort="downloads"))

    try:
        models = fetch("lerobot")
        if not models:
            models = fetch(None)
    except Exception as exc:  # noqa: BLE001 - surface any Hub/transport failure to the UI
        raise ModelHubError(f"Hugging Face search failed: {exc}") from exc
    return [
        {
            "repo_id": str(model.id),
            "downloads": int(getattr(model, "downloads", 0) or 0),
            "likes": int(getattr(model, "likes", 0) or 0),
            "last_modified": str(getattr(model, "last_modified", "") or ""),
            "tags": [tag for tag in (getattr(model, "tags", None) or []) if not tag.startswith("license:")][:6],
        }
        for model in models
    ]


def download_hf_model(repo_id: str, revision: str = "") -> str:
    """Fetch a repo into the Hugging Face cache and return the snapshot directory."""
    hub = _hub_module()
    kwargs: dict[str, Any] = {"repo_id": repo_id}
    if revision:
        kwargs["revision"] = revision
    try:
        return str(hub.snapshot_download(**kwargs))
    except Exception as exc:  # noqa: BLE001 - Hub errors are many and all user-facing
        raise ModelHubError(f"could not download {repo_id}: {exc}") from exc


def upload_hf_model(repo_id: str, path: str, revision: str = "") -> None:
    """Upload a local policy directory to its Hugging Face model repo."""
    hub = _hub_module()
    try:
        hub.create_repo(repo_id, repo_type="model", exist_ok=True)
        hub.upload_folder(
            repo_id=repo_id,
            repo_type="model",
            folder_path=path,
            revision=revision or None,
        )
    except Exception as exc:  # noqa: BLE001 - Hub/auth errors are user-facing
        raise ModelHubError(f"could not upload {repo_id}: {exc}") from exc


def resolve_remote(remote: str, *, revision: str = "") -> dict[str, Any]:
    """Turn a remote address into a local policy directory."""
    parsed = parse_remote(remote, revision=revision)
    if parsed.source == "local":
        path = Path(parsed.path).expanduser()
        if not path.is_dir():
            raise ModelHubError(f"local model path not found: {path}")
        return {"source": "local", "path": str(path.resolve()), "repo_id": "", "revision": ""}
    return {
        "source": "huggingface",
        "path": download_hf_model(parsed.repo_id, parsed.revision),
        "repo_id": parsed.repo_id,
        "revision": parsed.revision,
    }


def describe_path(path: str) -> dict[str, Any]:
    """Live facts about a weights directory, re-read on every listing."""
    root = Path(path)
    if not root.exists():
        return {"playable": False, "policy_type": "", "mtime": 0, "missing": True}
    config = policy_config(root)
    return {
        "playable": is_policy_dir(root),
        "policy_type": str(config.get("type") or ""),
        "mtime": int(root.stat().st_mtime),
        "missing": False,
    }


class ModelRegistry:
    """Scan results plus user-registered models, merged into one library list."""

    def __init__(self, store: JsonStore, roots: list[Path]) -> None:
        self.store = store
        self.roots = [Path(root) for root in roots]

    def list(self) -> list[dict[str, Any]]:
        scanned = list_local_models(self.roots)
        return merge_models(scanned, self.store.models())

    def get(self, model_id: str) -> dict[str, Any]:
        entry = self.store.model(model_id)
        if entry is None:
            raise KeyError(model_id)
        return self._decorate(entry)

    def register(
        self,
        *,
        remote: str,
        name: str = "",
        revision: str = "",
        note: str = "",
        download: bool = True,
    ) -> dict[str, Any]:
        """Register a remote address; weights are fetched unless ``download`` is false."""
        parsed = parse_remote(remote, revision=revision)
        existing = self._find_registered(parsed)
        entry: dict[str, Any] = dict(existing) if existing is not None else {
            "id": self._unique_id(parsed),
            "created_utc": _utc_now(),
            "path": "",
            "note": "",
        }
        entry.update(
            {
                "name": name.strip() or entry.get("name") or parsed.repo_id or Path(parsed.path).name or _slug(remote),
                "source": parsed.source,
                "remote": parsed.remote,
                "repo_id": parsed.repo_id,
                "revision": parsed.revision or str(entry.get("revision") or ""),
                "updated_utc": _utc_now(),
            }
        )
        if note:
            entry["note"] = note
        warning = ""
        if parsed.source == "local" or download:
            try:
                entry.update(resolve_remote(remote, revision=revision))
            except ModelHubError as exc:
                if parsed.source == "local":
                    raise
                warning = str(exc)
        saved = self.store.put_model(entry)
        return {**self._decorate(saved), "warning": warning}

    def save(self, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Edit name / remote address / revision / note of a registered model."""
        entry = self.store.model(model_id)
        if entry is None:
            scanned = next((row for row in self.list() if str(row.get("id") or "") == str(model_id)), None)
            if scanned is None:
                raise KeyError(model_id)
            entry = {
                key: value
                for key, value in scanned.items()
                if key not in {"managed", "playable", "missing"}
            }
            entry.setdefault("created_utc", _utc_now())
        if "name" in payload and payload["name"] is not None:
            entry["name"] = str(payload["name"]).strip() or entry.get("name") or model_id
        if "note" in payload and payload["note"] is not None:
            entry["note"] = str(payload["note"])
        source = payload.get("remote", payload.get("path"))
        if source is not None:
            remote = str(source).strip()
            if not remote:
                raise ModelHubError("remote address is empty")
            parsed = parse_remote(remote)
            requested_revision = (
                str(payload["revision"]).strip()
                if payload.get("revision") is not None
                else parsed.revision
            )
            entry.update(
                {
                    "source": parsed.source,
                    "remote": parsed.remote,
                    "repo_id": parsed.repo_id,
                    "revision": requested_revision,
                }
            )
            entry.update(resolve_remote(remote, revision=str(entry.get("revision") or "")))
        elif payload.get("revision") is not None:
            entry["revision"] = str(payload["revision"]).strip()
        entry["updated_utc"] = _utc_now()
        return self._decorate(self.store.put_model(entry))

    def update(self, model_id: str) -> dict[str, Any]:
        """Re-resolve the remote address, pulling the newest weights for a repo."""
        entry = self.store.model(model_id)
        if entry is None:
            row = next((item for item in self.list() if str(item.get("id") or "") == str(model_id)), None)
            if row is None:
                raise KeyError(model_id)
            entry = dict(row)
        remote = str(entry.get("remote") or "")
        if not remote:
            raise ModelHubError("model has no remote address to update from")
        entry.update(resolve_remote(remote, revision=str(entry.get("revision") or "")))
        entry["updated_utc"] = _utc_now()
        return self._decorate(self.store.put_model(entry))

    def delete(self, model_id: str) -> None:
        if self.store.model(model_id) is not None:
            self.store.delete_model(model_id)

    def upload(self, model_id: str) -> dict[str, Any]:
        entry = self.store.model(model_id)
        if entry is None:
            row = next((item for item in self.list() if str(item.get("id") or "") == str(model_id)), None)
            if row is None:
                raise KeyError(model_id)
            entry = dict(row)
        repo_id = str(entry.get("repo_id") or "").strip()
        path = str(entry.get("path") or "").strip()
        if not repo_id:
            raise ModelHubError("model has no upstream Hugging Face repo_id")
        if not Path(path).is_dir():
            raise ModelHubError(f"model path not found: {path}")
        upload_hf_model(repo_id, path, str(entry.get("revision") or ""))
        entry["updated_utc"] = _utc_now()
        return self._decorate(self.store.put_model(entry))

    def _find_registered(self, parsed: ModelRemote) -> dict[str, Any] | None:
        path_key = _path_key(parsed.path) if parsed.path else ""
        for entry in self.store.models():
            if parsed.repo_id and str(entry.get("repo_id") or "") == parsed.repo_id:
                return entry
            stored_path = str(entry.get("path") or "")
            if path_key and stored_path and _path_key(stored_path) == path_key:
                return entry
        return None

    def _unique_id(self, parsed: ModelRemote) -> str:
        taken = {str(entry.get("id") or "") for entry in self.store.models()}
        base = _slug(parsed.repo_id or parsed.path or parsed.remote)
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = dict(entry)
        row["managed"] = True
        path = str(row.get("path") or "")
        facts = (
            describe_path(path)
            if path
            else {"playable": False, "policy_type": "", "mtime": 0, "missing": True}
        )
        row["playable"] = facts["playable"]
        row["policy_type"] = row.get("policy_type") or facts["policy_type"]
        row["mtime"] = facts["mtime"]
        row["missing"] = facts["missing"]
        return row


def merge_models(scanned: list[dict[str, Any]], registered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registered models first; scan rows that resolve to the same weights are dropped."""
    rows: list[dict[str, Any]] = []
    claimed_paths: set[str] = set()
    claimed_repos: set[str] = set()
    for entry in registered:
        row = dict(entry)
        row["managed"] = True
        path = str(row.get("path") or "")
        if path:
            claimed_paths.add(_path_key(path))
            facts = describe_path(path)
            row["playable"] = facts["playable"]
            row["policy_type"] = row.get("policy_type") or facts["policy_type"]
            row["mtime"] = facts["mtime"]
            row["missing"] = facts["missing"]
        else:
            row.setdefault("playable", False)
            row.setdefault("policy_type", "")
            row["missing"] = True
            row["mtime"] = 0
        repo_id = str(row.get("repo_id") or "")
        if repo_id:
            claimed_repos.add(repo_id)
        rows.append(row)
    for entry in scanned:
        path = str(entry.get("path") or "")
        repo_id = str(entry.get("repo_id") or "")
        if path and _path_key(path) in claimed_paths:
            continue
        if repo_id and repo_id in claimed_repos:
            continue
        row = dict(entry)
        row["managed"] = False
        row["playable"] = True
        rows.append(row)
    return rows
