# Cloud models: service and Monitor integration

The cloud service can be operated from its standalone manager or registered in LeRobot Monitor as a normal model entry. Monitor keeps SSH credentials and service tokens in its backend, while Debug, synchronous Rollout and RTC Rollout use authenticated remote sessions.

## Entry points and state

- `lerobot-cloud-manager`: loopback HTTP on 127.0.0.1:8095; independent local state in `~/.lerobot-cloud-manager`.
- `lerobot-monitor-cloud`: loopback HTTP on 127.0.0.1:8091; authenticated management and inference endpoints.
- Default roots: `/data/zhuyutian/lerobot-monitor` on `8x4090-server`; `/data2/zhuyutian/lerobot-monitor` on `8A6000-server`.
- Both apps serve `cloud/web/index.html` and `/static/*`. `GET /api/ui-config` returns `{mode: "manager" | "cloud"}`.

## Cloud HTTP API

All `/api/v1/*` endpoints require `Authorization: Bearer <token>` (including health); browser requests in cloud mode supply a manually entered token held only in session memory. Collection endpoints return arrays. JSON errors use FastAPI `detail`.

- `GET /api/v1/health`: version, status and capabilities.
- `GET /api/v1/gpus`: GPU rows including uuid, index, name, memory_total_mb, memory_used_mb, healthy.
- `GET /api/v1/deployments`: deployment rows including id, name, source_kind, source, revision, status, gpu_uuid, error.
- `POST /api/v1/deployments`: `{name, source_kind: "huggingface" | "path", source, revision?: string}`. Returns deployment row (possibly queued) and associated job id if asynchronous.
- `POST /api/v1/deployments/{id}/load`: `{gpu_uuid: string, device?: string}`; returns operation status/job.
- `POST /api/v1/deployments/{id}/unload`: `{}`.
- `DELETE /api/v1/deployments/{id}?delete_files=false`: never deletes externally registered source directories.
- `GET /api/v1/deployments/{id}/logs`: `{text: string}`.
- `GET /api/v1/jobs`: jobs including id, kind, status, message/error, created_at, updated_at.

Inference uses authenticated HTTP sessions owned by cloud workers. Monitor reaches those sessions through its backend SSH tunnel; the browser receives only model, deployment and GPU metadata.

## Local manager HTTP API

- `GET /api/hosts`: array with id, alias, root, port, status, error (no tokens).
- `POST /api/hosts`: `{alias, root, port?: 8091, python?: "python3.12", runtime_python?: string}`. Alias uses existing OpenSSH config; credentials are never accepted.
- `POST /api/hosts/{id}/probe`, `/bootstrap`, `/connect`, `/disconnect`: action; bootstrap returns a local job immediately.
- `GET /api/jobs`: local initialization/upload task array.
- `/api/hosts/{id}/cloud/api/v1/{path}`: authenticated proxy for supported cloud endpoints; token injected only on backend.
- `POST /api/hosts/{id}/upload`: `{path: string, name: string}`; local checkpoint directory packed with a file-size/SHA256 manifest, transferred to isolated remote staging, verified and then registered as a path deployment. Symlinks and special files rejected. Returns local job immediately.

Mutating manager requests require loopback Host and same-origin Origin when present. SSH uses argument lists, batch mode, strict host-key checking, configured jumps, keepalives and bounded timeouts. Failed tunnels are cleaned before reconnect; manager shutdown stops its tunnels only.

## Installation and operations

Bootstrap builds the current package into a local temporary wheel, transfers only that artifact and a fresh service token over SSH stdin, installs an isolated uv environment under the dedicated root, and invokes the cloud service's owned daemon lifecycle. Existing projects and environments are left intact. An explicitly configured compatible `runtime_python` may run workers using its own dependencies. The worker pins only the `lerobot_monitor` package to the active service release through an importlib package bootstrap; it does not add the service environment to dependency search paths or modify the external environment.

Bootstrap is version-aware and must not replace an active service with a different build. Stop/switch requires no active inference sessions. Data persists across service restarts. Runtime/model dependencies are a separate profile from management dependencies; missing policy dependencies must produce an actionable error.

No actual remote initialization is performed just by opening the manager. Hosts start disconnected. SSH aliases may be probed without bootstrapping. Remote files are staged under the dedicated service root, and removal is restricted to owned files.

## Running the independent local manager

From this checkout, install `lerobot-monitor[cloud]` into its own environment, then run:

```powershell
lerobot-cloud-manager --port 8095 --project C:\path\to\lerobot-monitor
```

Open `http://127.0.0.1:8095`. Choose a server, inspect it, then initialize and connect. The manager does not contact either default host until an action is requested. This implementation round permits remote tests only on `8x4090-server`; `8A6000-server` remains configured but must not be connected, installed, started or tested.

The package must be built from a source checkout during initialization; `--project` disambiguates it when the manager itself was installed from a wheel. Package builds use a clean temporary copy and stable timestamps. They do not modify the source checkout or include local configuration, datasets or credentials.

## Runtime profiles and upgrades

`POST /api/hosts/{id}/runtime` accepts `{wheel_path, profile: "act" | "smolvla" | "pi", huggingface_home?: string}` and immediately returns a local background job. `wheel_path` is an absolute local path to a pinned LeRobot wheel (metadata name must be `lerobot`). Runtime installation is isolated under `ROOT/runtimes/<wheel-sha>-<profile>-<service-build>`, installs the Monitor worker package too, and records `requirements.lock` and `installed.txt`.

The initial supported CUDA stack is torch `2.11.0+cu128`, torchvision `0.26.0+cu128` from the official PyTorch CUDA 12.8 index, and transformers `5.5.4` for SmolVLA/PI. Management uses httpx `0.28.1` and huggingface-hub `1.30.0`. Every profile includes the LeRobot dataset extra because the native policy and rollout imports depend on it: ACT uses `[dataset]`, SmolVLA uses `[dataset,smolvla]`, and PI uses `[dataset,pi]`. The runtime resolves once to a hashed lock before installation, then validates CUDA availability and imports the native policy factory, processors, rollout configurations and the selected policy class before marking the environment ready. This does not guarantee a particular checkpoint fits GPU memory.

On success, an atomic `ROOT/runtime.json` contains `python`, `profile`, `lerobot_wheel_sha256`, `huggingface_home`, and the lock path. New model workers use that configuration; loaded workers keep their original process/runtime. The existing `/data/zhuyutian/cache/huggingface` cache may be selected explicitly. Existing third-party Python environments are not modified.

`POST /api/hosts/{id}/upgrade` stages the new environment first, asks the existing owned daemon to stop, and starts the new version. The cloud daemon refuses to stop while sessions or jobs are active. If the new start fails, the manager attempts to restore the previous daemon and reports whether rollback succeeded. Ordinary bootstrap does not replace an already-running different version.

Installation diagnostics are saved with user-only permissions on Linux: `ROOT/releases/<digest>/bootstrap.log` and `ROOT/runtimes/<digest>-<profile>-<build>/install.log`. Jobs report the relevant log path on installation failure. SSH keys, tokens and arbitrary subprocess output are never returned through job errors.

## Sessions (HTTP v1)

- `POST /api/v1/deployments/{id}/sessions` opens an exclusive session with `{mode: "select_action" | "debug_chunk" | "rtc_chunk", task?, state_keys?, overrides?}`.
- `POST /api/v1/sessions/{id}/heartbeat` and `/reset` take `{epoch}`.
- `POST /api/v1/sessions/{id}/infer` takes `{epoch, request_id, state, images?, task?, chunk_size?, prefix_raw?, prefix_absolute?, inference_delay?}`. Images must be base64-encoded PNG files; other image formats are rejected. Prefix tensors have shape `[T,A]` and must match each other.
- `DELETE /api/v1/sessions/{id}` closes the session.

These APIs are exposed through the local manager's authenticated backend proxy and through Monitor's backend-only cloud client. Monitor uses `debug_chunk` for Debug, `select_action` for synchronous Rollout and `rtc_chunk` for RTC Rollout.

## Monitor integration

Monitor stores a cloud model as `cloud://<host>/<deployment>?gpu=<uuid>`. The model remains selectable in the existing Debug and Rollout panels and has the same load, unload and residency controls as a local model. The Models add dialog can connect an SSH host, select a deployment, and bind it to a currently available GPU.

The cloud catalog API is backend-only:

- `GET /api/cloud/hosts`: configured SSH hosts without credentials or tokens.
- `POST /api/cloud/hosts/{id}/connect`: opens or reuses the SSH tunnel and returns deployments plus current GPU ownership.
- `GET /api/cloud/hosts/{id}/catalog`: refreshes the connected catalog.
- `POST /api/models/cloud`: registers `{host_id, deployment_id, gpu_uuid, name?}` in the normal model library.

GPU rows include the compute processes reported by `nvidia-smi`, their Linux users resolved through `/proc/<pid>/status`, executable names and per-process GPU memory. The UI shows the leading owner/program and disables a busy GPU unless it already belongs to the selected deployment. A single faulty GPU query does not hide healthy rows.

## Validation evidence

2026-09-28 final results:

| Check | Result |
| --- | --- |
| 4090 service initialization and upgrades | Bootstrap passed; two subsequent upgrades passed, about 14 seconds each |
| Runtime installation | Dataset dependency correction installed; native policy imports and GPU checks passed |
| ACT real inference | `select_action [1,6]` and `debug_chunk [8,6]` passed |
| SmolVLA real inference | `select_action [1,6]`, `debug_chunk [8,6]`, `rtc_chunk [50,6]` passed, including guided prefix inference |
| Lifecycle and transport | Exclusive leases, unload rejection while active, heartbeat, duplicate requests, reset/stale epochs passed; both models unloaded finally |
| Real local upload | Manifest/hash verification, managed registration and `delete_files=true` passed; stage and asset confirmed absent afterward |
| Linux service/manager/harness tests | 79 passed |
| Existing local full regression | 357 passed, 4 skipped, using a complete dependency environment and pinned LeRobot archive |
| UI | 12 tests and four viewport checks passed |
| GPU ownership | Real 4090 probe resolved Blender/Python processes to their Linux users and retained seven healthy GPU rows while one device query failed |
| Monitor direct client | ACT Debug returned 4×6 actions; ACT Sync returned 1×6; SmolVLA RTC returned a 50-action queue |
| Monitor control loop | Cloud registration, Sync ACT and RTC SmolVLA passed through the normal Monitor API, ControlLoop and virtual follower |
| Monitor browser | 4090 deployments and ownership labels rendered; busy GPUs were disabled; ACT was registered on an idle GPU |

Evidence files in the isolated checkout are `.tmp_cloud_results/act.json` and `.tmp_cloud_results/smolvla.json`; both report `status: passed`. Existing ACT/SmolVLA external cache weights were retained. The earlier lightweight-environment test failures and missing `datasets` import were resolved by the complete dependency environment and runtime profile correction; they are not the final acceptance result.

These inference tests used zero state and black PNG observations, with no robot attached. They validate the management and inference path, not task accuracy or physical robot performance. PI runtime support is implemented but no cached PI checkpoint was available for actual inference testing. A6000 was not contacted. Physical robot validation remains pending.

### Profile selection and transport status

`GET /api/hosts` keeps `status` equal to `connected` or `disconnected` even while a management job runs; `operation_status` separately reports `busy` or `idle`. Model/GPU panels therefore remain available during upload or runtime preparation.

Runtime configuration is version 1 with a `profiles` mapping keyed by `act`, `smolvla`, and `pi`. Preparing a profile preserves existing profiles and migrates an earlier single-profile file. Each profile stores its own interpreter, LeRobot wheel hash, cache directory and dependency lock. `default_profile` records the last prepared profile; top-level profile fields remain for older service compatibility. Worker selection uses the model policy type: SmolVLA requires `smolvla`; PI/PI0/PI05 requires `pi`; ACT can use `act`, then another installed profile that includes ACT dependencies. Existing workers remain pinned to their current interpreter.

### Upload recovery

A failed extraction, checksum verification or other preparation step triggers cleanup of exactly the manager-generated `ROOT/uploads/<32-hex-id>` directory. Cleanup validates the resolved parent and refuses symlinks or paths outside that upload root. A definitive HTTP 4xx registration rejection also removes staging. If registration times out, returns 5xx or returns an unreadable success body, its outcome is ambiguous: staging is preserved and the job error names its exact recovery path. Inspect deployments for that `source` before retrying or removing it; the service may already have moved the files into managed assets. If cleanup itself fails, the error likewise reports the recovery directory.

### Reusable GPU smoke test

Run the harness only against a deliberately selected deployment and healthy, unused GPU after connection and runtime installation:

```powershell
python scripts/smoke-cloud-models.py --deployment-id MODEL_ID --gpu GPU_UUID --output smoke-result.json
```

The default target is the independent manager at `http://127.0.0.1:8095`, host `8x4090-server`. `--model` can select a unique exact deployment name/source instead of its ID. The harness waits for load completion, discovers metadata, and tests every advertised native inference mode with zero joint state and black RGB PNG images of shape `[480,640,3]`. It validates finite raw/absolute action shapes `[T,A]`, exclusive sessions, rejection of unload while in use, duplicate request rejection, heartbeat, epoch reset, stale epoch rejection and RTC prefix inference when supported. Finally it closes its session and requests model unloading; cleanup failures are recorded in the JSON report. This proves API/inference plumbing, not task success or safe physical robot actions.

## Verified checkout and repeatable launch

The completed implementation is isolated in branch `codex/cloud-model-manager` at:

```text
C:\Users\Admin\.codex\worktrees\cloud-model-manager\lerobot-monitor
```

The current local page is `http://127.0.0.1:8095`. It runs the independent manager from this checkout via `PYTHONPATH`, using the existing lightweight Python executable only as a runtime. It does not load or change the existing Monitor application. If the manager has stopped, the following PowerShell commands reproduce that setup:

```powershell
$cloudCheckout = 'C:\Users\Admin\.codex\worktrees\cloud-model-manager\lerobot-monitor'
$cloudPython = 'D:\repos\lerobot\lerobot-monitor\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $cloudCheckout 'src'
& $cloudPython -m lerobot_monitor.cloud_manager --port 8095 --project $cloudCheckout
```

Leave this terminal running and open the page in a browser. If port 8095 already serves the manager, reuse the running page. The default local state remains `%USERPROFILE%\.lerobot-cloud-manager`; tokens are stored separately from host configuration. In the page select `8x4090-server` and connect. After a server restart, initialize the same build to restart its owned daemon; after code changes use Upgrade. Neither action modifies another project's service or Python environment.

The remote deployment root is `/data/zhuyutian/lerobot-monitor`. The Monitor integration runtimes use a LeRobot wheel built from commit `e5315df2`, version `0.6.2`, with SHA256:

```text
ae5a3dbb2f67298ee1953319b7805603047c4ee680817245bd23cef413b29803
```

The wheel was installed into the dedicated ACT and SmolVLA runtime profiles under the cloud service root. The installed runtimes' resolved dependencies and hashes remain on the server in their `requirements.lock` and `installed.txt`. Runtime profiles reuse the explicitly selected `/data/zhuyutian/cache/huggingface` cache without deleting externally owned checkpoints.

For repeat inference checks, first refresh the GPU list and select a currently unused healthy GPU UUID. Run in a second PowerShell terminal:

```powershell
$cloudCheckout = 'C:\Users\Admin\.codex\worktrees\cloud-model-manager\lerobot-monitor'
$cloudPython = 'D:\repos\lerobot\lerobot-monitor\.venv\Scripts\python.exe'
$gpuUuid = 'REPLACE_WITH_CURRENTLY_FREE_GPU_UUID'
& $cloudPython (Join-Path $cloudCheckout 'scripts\smoke-cloud-models.py') `
  --deployment-id '3f82e397b7cc4639ae29cb7b379fea2b' --gpu $gpuUuid `
  --output (Join-Path $cloudCheckout '.tmp_cloud_results\act-repeat.json')
& $cloudPython (Join-Path $cloudCheckout 'scripts\smoke-cloud-models.py') `
  --deployment-id 'cf72c3318f1241758eab843dba11fa12' --gpu $gpuUuid `
  --output (Join-Path $cloudCheckout '.tmp_cloud_results\smolvla-repeat.json')
```

These deployment IDs refer to the retained ACT and SmolVLA registrations from acceptance testing. If a registration has subsequently been removed, select the new ID from the management page. Each harness run closes its sessions and unloads its selected model. The independent manager and remote management service can remain running without keeping model weights in GPU memory.
