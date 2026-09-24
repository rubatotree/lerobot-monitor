"""Hugging Face dataset search/download and empty LeRobot dataset creation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .library import dataset_entry, hub_cache_repo_dir, huggingface_hub_cache, lerobot_home, list_hf_datasets, slugify
from .store import JsonStore
from .types import JOINT_ORDER

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SAFE_CAMERA = re.compile(r"[^A-Za-z0-9._-]+")
# Sort rank of scanned folders in the dataset list; registered datasets use 0 so
# the Library always shows them in the order the user added them.
_SCAN_RANK = 1


class DatasetHubError(RuntimeError):
    """Raised when a dataset remote cannot be searched, downloaded, or created."""


def _hub_module() -> Any:
    try:
        import huggingface_hub  # noqa: PLC0415 - optional, LeRobot brings it in
    except ImportError as exc:
        raise DatasetHubError(
            "huggingface_hub is not installed in this environment; local dataset scanning still works"
        ) from exc
    return huggingface_hub


_QUIET_PROGRESS: type[Any] | None = None


def _quiet_progress() -> type[Any]:
    """Return a tqdm replacement that renders nothing.

    The monitor draws download progress on its own dataset cards, so
    huggingface_hub's console bars would only duplicate and spam the log.
    """
    global _QUIET_PROGRESS
    if _QUIET_PROGRESS is not None:
        return _QUIET_PROGRESS
    try:
        from tqdm import tqdm  # noqa: PLC0415 - ships with huggingface_hub
    except ImportError:  # pragma: no cover - huggingface_hub depends on tqdm

        class _NoBar:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.disable = True

            def __enter__(self) -> "_NoBar":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def __getattr__(self, name: str) -> Callable[..., None]:
                return lambda *args, **kwargs: None

        _QUIET_PROGRESS = _NoBar
        return _NoBar

    class _QuietTqdm(tqdm):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("name", None)  # huggingface_hub's own marker, rejected by tqdm
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

    _QUIET_PROGRESS = _QuietTqdm
    return _QuietTqdm


def _repo_cache_bytes(repo_id: str) -> int:
    """Bytes already written to the Hub cache for one dataset repo.

    The cache stages payloads in `blobs` (including `*.incomplete`) and links
    them into `snapshots`; without symlink support the finished file is moved
    there instead. Counting both trees without following links tracks an
    in-flight `snapshot_download` in either layout.
    """
    root = huggingface_hub_cache() / f"datasets--{repo_id.replace('/', '--')}"
    total = 0
    for folder in (root / "blobs", root / "snapshots"):
        if not folder.is_dir():
            continue
        try:
            for path in folder.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _hub_dataset_bytes(repo_id: str, revision: str = "") -> int:
    """Total size of the files the Hub will send, or 0 when it cannot be read."""
    hub = _hub_module()
    try:
        info = hub.HfApi().dataset_info(repo_id, revision=revision or None, files_metadata=True)
    except Exception:  # noqa: BLE001 - progress is best effort; the download reports real errors
        return 0
    total = 0
    for sibling in getattr(info, "siblings", None) or []:
        size = getattr(sibling, "size", None)
        if isinstance(size, int) and size > 0:
            total += size
    return total


def _parse_remote(remote: str, revision: str = "") -> tuple[str, str]:
    text = str(remote or "").strip()
    if not text:
        raise DatasetHubError("dataset address is empty")
    if text.lower().startswith("hf:"):
        text = text[len("hf:") :].strip()
    found_revision = str(revision or "").strip()
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if parsed.netloc.lower() not in HF_HOSTS:
            raise DatasetHubError(f"only huggingface.co dataset URLs are supported, got {parsed.netloc or text}")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0] == "datasets":
            parts = parts[1:]
        elif parts and parts[0] in {"models", "spaces"}:
            raise DatasetHubError(f"not a dataset URL: {text}")
        if len(parts) < 2:
            raise DatasetHubError(f"not a dataset URL: {text}")
        repo_id = "/".join(parts[:2])
        if not found_revision and len(parts) >= 4 and parts[2] in {"tree", "resolve"}:
            found_revision = parts[3]
    else:
        repo_id = text
    if not _REPO_ID.fullmatch(repo_id):
        raise DatasetHubError(f"not a valid Hugging Face dataset id: {repo_id}")
    return repo_id, found_revision


def search_hf_datasets(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    text = str(query or "").strip()
    if not text:
        raise DatasetHubError("search query is empty")
    api = _hub_module().HfApi()

    def fetch(tag: str | None) -> list[Any]:
        # huggingface_hub 1.x dropped the `direction` argument; the Hub API
        # already returns `sort="downloads"` in descending order.
        return list(api.list_datasets(search=text, filter=tag, limit=limit, sort="downloads"))

    try:
        rows = fetch("lerobot")
        if not rows:
            rows = fetch(None)
    except Exception as exc:  # noqa: BLE001 - surface Hub/transport failures to the UI
        raise DatasetHubError(f"Hugging Face dataset search failed: {exc}") from exc
    return [
        {
            "repo_id": str(row.id),
            "downloads": int(getattr(row, "downloads", 0) or 0),
            "likes": int(getattr(row, "likes", 0) or 0),
            "last_modified": str(getattr(row, "last_modified", "") or ""),
            "tags": [tag for tag in (getattr(row, "tags", None) or []) if not tag.startswith("license:")][:6],
        }
        for row in rows
    ]


def download_hf_dataset(remote: str, *, revision: str = "") -> str:
    repo_id, parsed_revision = _parse_remote(remote, revision)
    hub = _hub_module()
    kwargs: dict[str, Any] = {
        "repo_id": repo_id,
        "repo_type": "dataset",
        "tqdm_class": _quiet_progress(),
        # Sync is an explicit overwrite operation. Revalidate cached files
        # against the Hub rather than silently returning a locally edited blob.
        "force_download": True,
    }
    if parsed_revision:
        kwargs["revision"] = parsed_revision
    try:
        path = Path(hub.snapshot_download(**kwargs))
    except Exception as exc:  # noqa: BLE001 - Hub errors are many and all user-facing
        raise DatasetHubError(f"could not download {repo_id}: {exc}") from exc
    if not (path / "meta" / "info.json").is_file():
        raise DatasetHubError(f"{repo_id} is not a LeRobot dataset: meta/info.json is missing")
    return str(path)


def _replace_local_dataset(snapshot: Path, destination: Path) -> str:
    """Install a complete Hub snapshot at an existing local dataset address."""
    source = Path(snapshot)
    target = Path(destination).expanduser().absolute()
    if target == Path(target.anchor) or target.is_symlink():
        raise DatasetHubError(f"unsafe local dataset destination: {target}")
    if not (source / "meta" / "info.json").is_file():
        raise DatasetHubError(f"downloaded dataset is missing meta/info.json: {source}")
    if target.exists() and not (target / "meta" / "info.json").is_file():
        raise DatasetHubError(f"local destination is not a dataset: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.sync-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    moved_original = False
    try:
        shutil.copytree(source, staging)
        if target.exists():
            target.rename(backup)
            moved_original = True
        try:
            staging.rename(target)
        except OSError:
            if moved_original:
                backup.rename(target)
                moved_original = False
            raise
    except Exception as exc:  # noqa: BLE001 - preserve the original on any copy/swap failure
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        recovery = f"; original data is at {backup}" if backup.exists() else ""
        raise DatasetHubError(f"could not replace local dataset {target}: {exc}{recovery}") from exc
    if moved_original:
        def retry_readonly(function: Callable[[str], None], path: str, excinfo: Any) -> None:
            del excinfo
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)

        try:
            shutil.rmtree(backup, onexc=retry_readonly)
        except OSError:
            # The new dataset is already installed. Keep the backup for manual
            # recovery rather than misreporting a successful sync as failed.
            pass
    return str(target)


_ACTIVE_TRANSFER_STATES = {"pending", "transferring", "finalizing"}
# Terminal states stay visible long enough for every open page to see the
# transition even when its status stream stalled while the tab was hidden.
_TRANSFER_RETENTION_S = 120.0
# Hub folders that must never be uploaded, mirroring upload_folder defaults.
_UPLOAD_SKIP_PARTS = {".git"}
_UPLOAD_SKIP_PREFIXES = (".cache/huggingface",)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upload_folder_files(root: Path) -> list[tuple[str, int]]:
    """Relative paths and sizes of the files an upload will send."""
    root = Path(root)
    if not (root / "meta" / "info.json").is_file():
        raise DatasetHubError(f"local dataset is missing meta/info.json: {root}")
    cache_repo = hub_cache_repo_dir(root)
    files: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        try:
            # Hub snapshots link into their own `blobs` directory. A link in a
            # regular dataset may point anywhere, so never follow that one.
            if path.is_symlink() and (
                cache_repo is None or not path.resolve().is_relative_to(cache_repo)
            ):
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
        except OSError:
            continue
        parts = set(relative.split("/"))
        if parts & _UPLOAD_SKIP_PARTS or relative.startswith(_UPLOAD_SKIP_PREFIXES):
            continue
        try:
            files.append((relative, int(path.stat().st_size)))
        except OSError:
            continue
    return files


def _upload_batch_size(file_count: int) -> int:
    """Aim for roughly eight progress steps, capped per commit."""
    return max(1, min(50, -(-file_count // 8)))


def upload_dataset_folder(
    repo_id: str,
    folder: Path,
    *,
    revision: str = "",
    private: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Mirror a dataset folder to the Hub, reporting bytes and files done.

    ``upload_folder`` offers no progress hook, so the monitor commits batches
    itself; each completed batch is one progress step on the dataset card.
    """
    repo_id, parsed_revision = _parse_remote(repo_id, revision)
    revision = parsed_revision
    hub = _hub_module()
    root = Path(folder)
    files = upload_folder_files(root)
    if not files:
        raise DatasetHubError(f"nothing to upload from {root}")
    total_files = len(files)
    batch_size = _upload_batch_size(total_files)
    try:
        operation_add = hub.CommitOperationAdd
        operation_delete = hub.CommitOperationDelete
    except AttributeError as exc:  # pragma: no cover - older huggingface_hub
        raise DatasetHubError("huggingface_hub does not expose commit operations") from exc
    commit_kwargs: dict[str, Any] = {"repo_id": repo_id, "repo_type": "dataset"}
    if revision:
        commit_kwargs["revision"] = revision
    done_bytes = 0
    done_files = 0
    try:
        hub.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)
        remote_files = set(hub.list_repo_files(repo_id, repo_type="dataset", revision=revision or None))
        local_files = {relative for relative, _size in files}
        # Hub creates this file for LFS routing. Keep it unless the local
        # dataset explicitly supplies its own copy.
        obsolete = sorted(remote_files - local_files - {".gitattributes"})
        for start in range(0, total_files, batch_size):
            batch = files[start : start + batch_size]
            operations = [
                operation_add(path_in_repo=relative, path_or_fileobj=str(root / relative))
                for relative, _size in batch
            ]
            hub.create_commit(
                operations=operations,
                commit_message=f"Upload {len(operations)} file(s) via lerobot-monitor",
                **commit_kwargs,
            )
            done_bytes += sum(size for _relative, size in batch)
            done_files += len(batch)
            if on_progress is not None:
                on_progress(done_bytes, done_files)
        for start in range(0, len(obsolete), 50):
            batch = obsolete[start : start + 50]
            hub.create_commit(
                operations=[operation_delete(path_in_repo=relative) for relative in batch],
                commit_message=f"Remove {len(batch)} obsolete dataset file(s) via lerobot-monitor",
                **commit_kwargs,
            )
    except DatasetHubError:
        raise
    except Exception as exc:  # noqa: BLE001 - Hub/auth errors are user-facing
        raise DatasetHubError(f"could not upload {repo_id}: {exc}") from exc
    return str(root)


@dataclass
class DatasetTransfer:
    """One Hub dataset download or upload plus the counters a card renders."""

    repo_id: str
    remote: str
    direction: str = "download"
    revision: str = ""
    name: str = ""
    private: bool = False
    status: str = "pending"
    transferred_bytes: int = 0
    total_bytes: int = 0
    files_done: int = 0
    files_total: int = 0
    path: str = ""
    target_path: str = ""
    error: str = ""
    started_utc: str = field(default_factory=_utc_now)
    finished_utc: str = ""

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_TRANSFER_STATES

    def to_dict(self) -> dict[str, Any]:
        total = max(0, int(self.total_bytes))
        done = max(0, int(self.transferred_bytes))
        if self.status == "done":
            percent = 100.0
        elif total > 0:
            percent = round(min(done, total) * 100.0 / total, 1)
            if self.active:
                percent = min(percent, 99.0)
        else:
            percent = 0.0
        return {
            "repo_id": self.repo_id,
            "remote": self.remote,
            "direction": self.direction,
            "revision": self.revision,
            "name": self.name,
            "private": self.private,
            "status": self.status,
            "active": self.active,
            "percent": percent,
            "indeterminate": bool(self.active and total <= 0),
            "transferred_bytes": done,
            "total_bytes": total,
            "files_done": max(0, int(self.files_done)),
            "files_total": max(0, int(self.files_total)),
            "path": self.path,
            "target_path": self.target_path,
            "error": self.error,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
        }


class DatasetTransferManager:
    """Move Hub datasets off the request thread and report progress per card."""

    def __init__(
        self,
        *,
        poll_seconds: float = 0.5,
        on_finish: Callable[[dict[str, Any]], None] | None = None,
        progress_probe: Callable[[str], int] | None = None,
        total_probe: Callable[[str, str], int] | None = None,
        downloader: Callable[..., str] | None = None,
        uploader: Callable[..., str] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._transfers: dict[str, DatasetTransfer] = {}
        self._poll_seconds = max(0.05, float(poll_seconds))
        self._on_finish = on_finish
        self._progress_probe = progress_probe or _repo_cache_bytes
        self._total_probe = total_probe or _hub_dataset_bytes
        self._download = downloader or download_hf_dataset
        self._upload = uploader or upload_dataset_folder

    def start(
        self, remote: str, *, revision: str = "", name: str = "", target_path: str = ""
    ) -> dict[str, Any]:
        """Queue one download and return its initial state."""
        repo_id, parsed_revision = _parse_remote(remote, revision)
        state = self._reserve(
            repo_id,
            remote=str(remote or "").strip() or repo_id,
            direction="download",
            revision=parsed_revision,
            name=name,
        )
        state.target_path = str(target_path or "")
        self._spawn(self._run_download, state, f"dataset-download::{repo_id}")
        return state.to_dict()

    def start_upload(
        self,
        repo_id: str,
        folder: str | Path,
        *,
        revision: str = "",
        name: str = "",
        private: bool = False,
    ) -> dict[str, Any]:
        """Queue one upload of a local dataset folder and return its state."""
        state = self._reserve(
            str(repo_id or "").strip(),
            remote=str(repo_id or "").strip(),
            direction="upload",
            revision=str(revision or ""),
            name=name,
            private=private,
        )
        state.path = str(folder)
        self._spawn(self._run_upload, state, f"dataset-upload::{state.repo_id}")
        return state.to_dict()

    def states(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            return [state.to_dict() for state in self._transfers.values()]

    def get(self, repo_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._transfers.get(str(repo_id))
            return state.to_dict() if state is not None else None

    def _reserve(
        self,
        repo_id: str,
        *,
        remote: str,
        direction: str,
        revision: str,
        name: str,
        private: bool = False,
    ) -> DatasetTransfer:
        if not repo_id:
            raise DatasetHubError("dataset repo_id is empty")
        with self._lock:
            current = self._transfers.get(repo_id)
            if current is not None and current.active:
                verb = "uploading" if current.direction == "upload" else "downloading"
                raise DatasetHubError(f"{repo_id} is already {verb}")
            state = DatasetTransfer(
                repo_id=repo_id,
                remote=remote,
                direction=direction,
                revision=revision,
                name=str(name or "").strip() or repo_id,
                private=bool(private),
            )
            self._transfers[repo_id] = state
        return state

    def _spawn(self, target: Callable[[DatasetTransfer], None], state: DatasetTransfer, label: str) -> None:
        threading.Thread(target=target, args=(state,), name=label, daemon=True).start()

    def _prune_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for key, state in list(self._transfers.items()):
            if state.active or not state.finished_utc:
                continue
            try:
                finished = datetime.fromisoformat(state.finished_utc)
            except ValueError:
                continue
            if (now - finished).total_seconds() > _TRANSFER_RETENTION_S:
                self._transfers.pop(key, None)

    def _run_download(self, state: DatasetTransfer) -> None:
        self._update(state, status="transferring")
        self._update(state, total_bytes=self._probe_total(state))
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_download,
            args=(state, stop),
            name=f"dataset-progress::{state.repo_id}",
            daemon=True,
        )
        watcher.start()
        try:
            path = self._download(state.repo_id, revision=state.revision)
            if state.target_path:
                path = _replace_local_dataset(Path(path), Path(state.target_path))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI through the state
            self._update(state, error=str(exc))
            terminal = "error"
        else:
            self._update(state, path=str(path))
            terminal = "done"
        finally:
            stop.set()
            watcher.join(timeout=2.0)
            self._update(state, transferred_bytes=self._probe_downloaded(state))
            self._finish(state, terminal)

    def _run_upload(self, state: DatasetTransfer) -> None:
        folder = Path(state.path)
        try:
            files = upload_folder_files(folder)
        except Exception as exc:  # noqa: BLE001 - report any local scan failure
            self._update(state, error=f"could not read {folder}: {exc}")
            self._finish(state, "error")
            return
        self._update(
            state,
            status="transferring",
            total_bytes=sum(size for _relative, size in files),
            files_total=len(files),
        )

        def on_progress(done_bytes: int, done_files: int) -> None:
            self._update(state, transferred_bytes=done_bytes, files_done=done_files)

        try:
            self._upload(
                state.repo_id,
                folder,
                revision=state.revision,
                private=state.private,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI through the state
            self._update(state, error=str(exc))
            terminal = "error"
        else:
            self._update(
                state,
                transferred_bytes=sum(size for _relative, size in files),
                files_done=len(files),
            )
            terminal = "done"
        finally:
            self._finish(state, terminal)

    def _finish(self, state: DatasetTransfer, terminal: str) -> None:
        # The card must not report completion before the Library has saved the
        # new path; otherwise its one refresh can observe the old entry forever.
        self._update(state, status="finalizing", finished_utc=_utc_now())
        finished = self.get(state.repo_id)
        if finished is not None and self._on_finish is not None:
            finished.update(status=terminal, active=False, indeterminate=False)
            if terminal == "done":
                finished["percent"] = 100.0
            try:
                self._on_finish(finished)
            except Exception as exc:  # noqa: BLE001 - show persistence failure
                terminal = "error"
                self._update(state, error=f"transfer completed but Library update failed: {exc}")
        self._update(state, status=terminal)

    def _watch_download(self, state: DatasetTransfer, stop: threading.Event) -> None:
        while not stop.wait(self._poll_seconds):
            self._update(state, transferred_bytes=self._probe_downloaded(state))

    def _probe_total(self, state: DatasetTransfer) -> int:
        try:
            return max(0, int(self._total_probe(state.repo_id, state.revision) or 0))
        except Exception:  # noqa: BLE001 - progress is best effort
            return 0

    def _probe_downloaded(self, state: DatasetTransfer) -> int:
        try:
            return max(0, int(self._progress_probe(state.repo_id) or 0))
        except Exception:  # noqa: BLE001 - progress is best effort
            return state.transferred_bytes

    def _update(self, state: DatasetTransfer, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(state, key, value)



def _normalize_camera_key(value: str, fallback: str) -> str:
    key = _SAFE_CAMERA.sub("_", str(value or "").strip()).strip("._-")
    return key or fallback


def _feature_cameras(cameras: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            continue
        base = _normalize_camera_key(str(camera.get("key") or camera.get("name") or ""), f"camera{index + 1}")
        key = base
        suffix = 2
        while key.casefold() in used:
            key = f"{base}_{suffix}"
            suffix += 1
        used.add(key.casefold())
        try:
            width = max(1, int(camera.get("width") or 640))
            height = max(1, int(camera.get("height") or 480))
        except (TypeError, ValueError):
            width, height = 640, 480
        features[f"observation.images.{key}"] = {
            "dtype": "video",
            "shape": [height, width, 3],
            "names": ["height", "width", "channels"],
        }
    return features


def create_empty_dataset(
    *,
    name: str,
    repo_id: str = "",
    fps: int = 15,
    robot_type: str = "",
    cameras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a minimal, readable LeRobot v3 dataset with zero episodes."""
    if int(fps) <= 0:
        raise DatasetHubError("fps must be positive")
    resolved_repo = str(repo_id or "").strip() or slugify(name, fallback="dataset")
    if not _REPO_ID.fullmatch(resolved_repo):
        raise DatasetHubError(f"invalid dataset repo id: {resolved_repo}")
    destination = lerobot_home() / resolved_repo
    if destination.exists():
        raise DatasetHubError(f"dataset already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    joint_names = [f"{joint}.pos" for joint in JOINT_ORDER]
    features: dict[str, dict[str, Any]] = {
        "action": {"dtype": "float32", "shape": [len(joint_names)], "names": joint_names},
        "observation.state": {"dtype": "float32", "shape": [len(joint_names)], "names": joint_names},
        **_feature_cameras(cameras or []),
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
    }
    info = {
        "codebase_version": "v3.0",
        "fps": int(fps),
        "features": features,
        "total_episodes": 0,
        "total_frames": 0,
        "total_tasks": 0,
        "chunks_size": 1000,
        "data_files_size_in_mb": 100,
        "video_files_size_in_mb": 200,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
        if _feature_cameras(cameras or [])
        else None,
        "robot_type": str(robot_type or "").strip() or None,
        "splits": {},
    }
    temp_path = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    )
    try:
        (temp_path / "meta").mkdir(parents=True)
        (temp_path / "data").mkdir()
        (temp_path / "videos").mkdir()
        (temp_path / "meta" / "info.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.rename(destination)
    except BaseException:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise
    return {
        "id": resolved_repo,
        "repo_id": resolved_repo,
        "name": resolved_repo,
        "title": str(name or resolved_repo),
        "path": str(destination),
        "source": "lerobot",
        "episodes": 0,
        "fps": int(fps),
        "lerobot": True,
        "has_video": bool(cameras),
        "previewable": False,
        "playable": False,
    }


def _describe_dataset_source(remote: str, revision: str = "") -> dict[str, Any]:
    """Classify a dataset address locally; never contacts the Hub or copies data."""
    text = str(remote or "").strip()
    candidate = Path(text).expanduser()
    if candidate.is_dir():
        if not (candidate / "meta" / "info.json").is_file():
            raise DatasetHubError(f"local dataset is missing meta/info.json: {candidate}")
        return {
            "source": "local",
            "remote": str(candidate),
            "repo_id": "",
            "path": str(candidate.resolve()),
            "revision": str(revision or ""),
        }
    repo_id, parsed_revision = _parse_remote(text, revision)
    return {
        "source": "hub",
        "remote": text,
        "repo_id": repo_id,
        "path": "",
        "revision": parsed_revision,
    }


def _resolve_dataset_source(remote: str, revision: str = "") -> dict[str, Any]:
    described = _describe_dataset_source(remote, revision)
    if described["source"] == "local":
        return described
    described["path"] = download_hf_dataset(described["repo_id"], revision=described["revision"])
    return described



class DatasetRegistry:
    """Scanned datasets plus editable local/Hub registrations."""

    def __init__(self, store: JsonStore, roots: list[Path]) -> None:
        self.store = store
        self.roots = [Path(root) for root in roots]
        self.transfers = DatasetTransferManager(on_finish=self._finish_transfer)

    def _scanned(self) -> list[dict[str, Any]]:
        return list_hf_datasets(self.roots)

    def _entry_for(self, dataset_id: str) -> dict[str, Any] | None:
        """Find a stored dataset by its id or by its upstream repo_id."""
        key = str(dataset_id or "")
        if not key:
            return None
        entry = self.store.dataset(key)
        if entry is not None:
            return entry
        for candidate in self.store.datasets():
            if str(candidate.get("repo_id") or "") == key:
                return candidate
        return None

    def assert_not_transferring(self, dataset_id: str) -> None:
        """Prevent deletion or a second sync while the same repo is in use."""
        entry = self._entry_for(dataset_id)
        repo_id = str((entry or {}).get("repo_id") or dataset_id or "")
        lookup = getattr(self.transfers, "get", None)
        state = lookup(repo_id) if callable(lookup) else None
        if state and state.get("active"):
            raise DatasetHubError(f"{repo_id} is still {state.get('direction') or 'transferring'}")

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = dict(entry)
        path = Path(str(row.get("path") or ""))
        repo_id = str(row.get("repo_id") or row.get("id") or "")
        if path.is_dir() and (path / "meta" / "info.json").is_file():
            scanned = dataset_entry(repo_id or path.name, path, str(row.get("source") or "local")) or {}
            scanned.update(row)
            row = scanned
        # The public id follows the upstream repo so every caller (cards,
        # overrides, delete) addresses a dataset the same way.
        row["id"] = str(row.get("repo_id") or entry.get("id") or row.get("id") or "")
        row["managed"] = True
        row["private"] = bool(row.get("private", False))
        row["path_is_cache"] = hub_cache_repo_dir(row.get("path")) is not None
        row["upstream"] = bool(row.get("repo_id"))
        row["editable"] = True
        return row

    @staticmethod
    def _added_key(row: dict[str, Any]) -> tuple[int, int, str]:
        """Stable join order: registered datasets keep their store position.

        Scanned folders have no record of when they were added, so they follow
        the registered ones in scan order (newest first) instead of being
        grouped ahead of them by source.
        """
        return (
            int(row.get("_added_rank", _SCAN_RANK)),
            int(row.get("_added_index", 0)),
            str(row.get("id") or ""),
        )

    def list(self) -> list[dict[str, Any]]:
        registered: list[dict[str, Any]] = []
        for index, entry in enumerate(self.store.datasets()):
            row = self._decorate(entry)
            row["_added_rank"] = 0
            row["_added_index"] = index
            registered.append(row)
        claimed_ids = {str(row.get("id") or "") for row in registered}
        claimed_repos = {str(row.get("repo_id") or "") for row in registered if row.get("repo_id")}
        claimed_paths = {
            str(Path(str(row.get("path"))).resolve())
            for row in registered
            if row.get("path")
        }
        rows = list(registered)
        for index, entry in enumerate(self._scanned()):
            row = dict(entry)
            row["id"] = str(row.get("repo_id") or row.get("id") or "")
            row["managed"] = False
            row["upstream"] = str(row.get("source") or "") == "hub"
            row["path_is_cache"] = hub_cache_repo_dir(row.get("path")) is not None
            row["editable"] = True
            path = str(Path(str(row.get("path"))).resolve()) if row.get("path") else ""
            if row["id"] in claimed_ids or row.get("repo_id") in claimed_repos or path in claimed_paths:
                continue
            row["_added_rank"] = _SCAN_RANK
            row["_added_index"] = index
            rows.append(row)
        # Registered Hub and local datasets interleave in the order they joined
        # the library; scanned folders trail them.
        rows.sort(key=self._added_key)
        return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]

    def get(self, dataset_id: str) -> dict[str, Any]:
        key = str(dataset_id or "")
        for row in self.list():
            if key and key in {str(row.get("id") or ""), str(row.get("repo_id") or "")}:
                return row
        entry = self._entry_for(key)
        if entry is not None:
            return self._decorate(entry)
        raise FileNotFoundError(dataset_id)


    def register(
        self,
        *,
        remote: str,
        name: str = "",
        revision: str = "",
        repo_id: str = "",
        private: bool = False,
        download: bool = True,
    ) -> dict[str, Any]:
        del download  # Dataset registration always resolves the source before saving.
        resolved = _resolve_dataset_source(remote, revision)
        repo_id = str(repo_id or resolved.get("repo_id") or "")
        path = str(resolved.get("path") or "")
        dataset_id = repo_id or Path(path).name or slugify(name, fallback="dataset")
        entry = self.store.dataset(dataset_id) or {
            "id": dataset_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(
            {
                "name": str(name or entry.get("name") or dataset_id),
                **resolved,
                "repo_id": repo_id,
                "private": bool(private),
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self._decorate(self.store.put_dataset(entry))

    def save(self, dataset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store edited fields only; syncing stays an explicit download action."""
        entry = self._entry_for(dataset_id)
        if entry is None:
            current = self.get(dataset_id)
            entry = {
                "id": str(current.get("id") or dataset_id),
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "name": current.get("display_name") or current.get("title") or dataset_id,
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") if current.get("upstream") else "",
                "source": current.get("source") or "",
            }
        if "name" in payload and payload["name"] is not None:
            entry["name"] = str(payload["name"]).strip() or entry.get("name") or dataset_id
        if "path" in payload or "repo_id" in payload:
            previous_repo = str(entry.get("repo_id") or "")
            previous_path = str(entry.get("path") or "")
            if "repo_id" in payload:
                raw_repo = str(payload["repo_id"] or "").strip()
                next_repo = _parse_remote(raw_repo)[0] if raw_repo else ""
                if next_repo and next_repo != previous_repo:
                    existing = self._entry_for(next_repo)
                    if existing is not None and str(existing.get("id") or "") != str(entry.get("id") or ""):
                        raise DatasetHubError(f"dataset {next_repo} is already in the Library")
                entry["repo_id"] = next_repo
                entry["remote"] = next_repo
                if next_repo != previous_repo and "path" not in payload and hub_cache_repo_dir(previous_path, previous_repo):
                    entry["path"] = ""
            if "path" in payload:
                raw_path = str(payload["path"] or "").strip()
                if raw_path:
                    path = Path(raw_path).expanduser().resolve()
                    if not (path / "meta" / "info.json").is_file():
                        raise DatasetHubError(f"local dataset is missing meta/info.json: {path}")
                    entry["path"] = str(path)
                else:
                    entry["path"] = ""
            if not entry.get("repo_id") and not entry.get("path"):
                raise DatasetHubError("dataset needs an upstream or a local directory")
            entry["source"] = (
                "local" if entry.get("path") and hub_cache_repo_dir(entry["path"]) is None else "hub"
            )
            if payload.get("revision") is not None:
                entry["revision"] = str(payload["revision"])
        else:
            source_value = payload.get("remote")
            if source_value is not None:
                described = _describe_dataset_source(str(source_value), str(payload.get("revision") or ""))
                previous_repo = str(entry.get("repo_id") or "")
                previous_path = str(entry.get("path") or "")
                next_repo = str(described.get("repo_id") or "")
                if next_repo and next_repo != previous_repo:
                    existing = self._entry_for(next_repo)
                    if existing is not None and str(existing.get("id") or "") != str(entry.get("id") or ""):
                        raise DatasetHubError(f"dataset {next_repo} is already in the Library")
                entry.update(described)
                if described["source"] == "local":
                    entry["path"] = str(described.get("path") or previous_path)
                else:
                    # A local dataset can acquire or change its upstream without
                    # losing the only writable copy. A cached Hub snapshot belongs
                    # to its old repo and must be dropped when the address changes.
                    keep_local = bool(previous_path and hub_cache_repo_dir(previous_path) is None)
                    entry["path"] = (
                        previous_path if previous_repo == described["repo_id"] or keep_local else ""
                    )
            elif payload.get("revision") is not None:
                entry["revision"] = str(payload["revision"])
        if "private" in payload:
            entry["private"] = bool(payload["private"])
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return self._decorate(self.store.put_dataset(entry))

    def stage(self, *, remote: str, name: str = "", revision: str = "") -> dict[str, Any]:
        """Record a Hub dataset before its bytes exist so a card can show progress."""
        repo_id, parsed_revision = _parse_remote(remote, revision)
        entry = self._entry_for(repo_id) or {
            "id": repo_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(
            {
                "name": str(name or entry.get("name") or repo_id),
                "source": "hub",
                "remote": str(remote or repo_id),
                "repo_id": repo_id,
                "revision": parsed_revision,
                "path": str(entry.get("path") or ""),
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self._decorate(self.store.put_dataset(entry))

    def start_download(
        self,
        *,
        remote: str = "",
        dataset_id: str = "",
        name: str = "",
        revision: str = "",
    ) -> dict[str, Any]:
        """Start a background download and return its progress state."""
        if not str(remote or "").strip():
            entry = self._entry_for(str(dataset_id or ""))
            if entry is None:
                scanned = self.get(str(dataset_id or ""))
                if not scanned.get("upstream"):
                    raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
                entry = self.store.put_dataset(
                    {
                        "id": str(scanned.get("id") or dataset_id),
                        "name": str(scanned.get("display_name") or scanned.get("title") or dataset_id),
                        "source": "hub",
                        "remote": str(scanned.get("repo_id") or ""),
                        "repo_id": str(scanned.get("repo_id") or ""),
                        "revision": str(scanned.get("revision") or ""),
                        "path": str(scanned.get("path") or ""),
                        "created_utc": _utc_now(),
                        "updated_utc": _utc_now(),
                    }
                )
            repo_id = str(entry.get("repo_id") or "")
            if not repo_id:
                raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
            return self.transfers.start(
                str(entry.get("remote") or repo_id),
                revision=str(entry.get("revision") or ""),
                name=str(entry.get("name") or repo_id),
                target_path=(
                    str(entry.get("path") or "")
                    if entry.get("path") and hub_cache_repo_dir(str(entry["path"])) is None
                    else ""
                ),
            )
        repo_id, _parsed_revision = _parse_remote(remote, revision)
        self.assert_not_transferring(repo_id)
        staged = self.stage(remote=remote, name=name, revision=revision)
        return self.transfers.start(
            remote,
            revision=revision,
            name=str(staged.get("name") or ""),
        )

    def start_upload(self, dataset_id: str) -> dict[str, Any]:
        """Start a background overwrite upload and return its progress state."""
        entry = self._entry_for(dataset_id)
        if entry is None:
            scanned = self.get(dataset_id)
            if not scanned.get("upstream"):
                raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
            entry = self.store.put_dataset(
                {
                    "id": str(scanned.get("id") or dataset_id),
                    "name": str(scanned.get("display_name") or scanned.get("title") or dataset_id),
                    "source": str(scanned.get("source") or "local"),
                    "remote": str(scanned.get("repo_id") or ""),
                    "repo_id": str(scanned.get("repo_id") or ""),
                    "revision": str(scanned.get("revision") or ""),
                    "path": str(scanned.get("path") or ""),
                    "created_utc": _utc_now(),
                    "updated_utc": _utc_now(),
                }
            )
        repo_id = str(entry.get("repo_id") or "").strip()
        raw_path = str(entry.get("path") or "").strip()
        if not repo_id:
            raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
        if not raw_path:
            raise DatasetHubError("dataset has no local path to upload")
        path = Path(raw_path)
        if not (path / "meta" / "info.json").is_file():
            raise DatasetHubError(f"local dataset is missing meta/info.json: {path}")
        return self.transfers.start_upload(
            repo_id,
            path,
            revision=str(entry.get("revision") or ""),
            name=str(entry.get("name") or repo_id),
            private=bool(entry.get("private", False)),
        )

    def _finish_transfer(self, state: dict[str, Any]) -> None:
        """Persist the outcome of a background transfer onto the dataset entry."""
        repo_id = str(state.get("repo_id") or "")
        if not repo_id:
            return
        # The user may have deleted or repointed the card while the worker ran.
        # Never recreate it or attach old bytes to a new source.
        entry = self._entry_for(repo_id)
        if entry is None or str(entry.get("repo_id") or "") != repo_id:
            return
        if str(entry.get("revision") or "") != str(state.get("revision") or ""):
            return
        if state.get("status") != "done":
            # Keep a staged card so an interrupted download can be retried.
            return
        if state.get("direction") == "upload":
            if str(entry.get("path") or "") != str(state.get("path") or ""):
                return
            entry["uploaded_utc"] = datetime.now(timezone.utc).isoformat()
        else:
            if str(entry.get("source") or "") != "hub":
                return
            target_path = str(state.get("target_path") or "")
            if target_path and str(entry.get("path") or "") != target_path:
                return
            entry.update(
                {
                    "name": str(entry.get("name") or state.get("name") or repo_id),
                    "source": "hub",
                    "remote": str(state.get("remote") or repo_id),
                    "repo_id": repo_id,
                    "revision": str(state.get("revision") or ""),
                    "path": str(state.get("path") or entry.get("path") or ""),
                }
            )
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        self.store.put_dataset(entry)

    def update(self, dataset_id: str) -> dict[str, Any]:
        entry = self._entry_for(dataset_id) or self.get(dataset_id)
        entry = {
            "id": str(entry.get("id") or dataset_id),
            "created_utc": entry.get("created_utc") or datetime.now(timezone.utc).isoformat(),
            "name": entry.get("display_name") or entry.get("title") or dataset_id,
            "path": entry.get("path") or "",
            "repo_id": entry.get("repo_id") or "",
            "source": entry.get("source") or "",
        }
        repo_id = str(entry.get("repo_id") or "")
        if not repo_id:
            raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
        resolved = _resolve_dataset_source(repo_id, str(entry.get("revision") or ""))
        entry.update(resolved)
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return self._decorate(self.store.put_dataset(entry))

    def delete(self, dataset_id: str, *, current: dict[str, Any] | None = None) -> dict[str, Any]:
        self.assert_not_transferring(dataset_id)
        entry = self._entry_for(dataset_id)
        if entry is None:
            current = current or self.get(dataset_id)
            entry = {
                "id": str(current.get("id") or dataset_id),
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") or "",
            }
        self.store.delete_dataset(str(entry.get("id") or dataset_id))
        return dict(entry)
