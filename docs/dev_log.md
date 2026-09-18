# Dev log

## 2026-09-18 (rollout no-freeze)

- `/api/rollout/start` returns immediately; policy loads on a background thread so the UI WS and cameras keep running.
- Other command endpoints use `asyncio.to_thread` so they cannot stall the event loop.
- Policies are cached in-process and loaded with `HF_HUB_OFFLINE` / `local_files_only` first to avoid re-downloading.

## 2026-09-18 (logs / ports / datasets)

- Third-party stdout and logs during policy load go to the UI log and `run.log` in the video session.
- Scrollbars use the panel palette. Auto-record uses `.auto-on` highlight. Arm/leader ports persist on connect and reload as the select default.
- HF dataset scan dedupes by repo_id, prefers a playable local/lerobot copy, and resolves v2 episode mp4 plus v3 chunk files with timestamp windows.

## 2026-09-18 (dataset viz + stop-anytime)

- Left library preview matches visualize_dataset: episode prev/next, synced multi-cam video, seek bar, state/action chart. Works for local videos and HF/LeRobot cache datasets.
- Stop is immediate (`request_stop`) even while rollout/teleop/record is still loading; extra start clicks stay ignored.

## 2026-09-18 (focus + stop label)

- Camera cards have Autofocus + Focus slider; applied on the capture thread via DirectShow `CAP_PROP_FOCUS`.
- Header task buttons only change the label to `stop` while running; icon and color stay the same.

## 2026-09-18 (pending + HF datasets + videos)

- Header teleop/record/rollout/capture/relax log immediately (`note_pending` + frontend `runAction`) and ignore extra clicks while busy. Pill shows `loading` until the control thread finishes.
- Local recordings live under `data/videos` (per-camera MP4 + merged + CSV), not as Hugging Face datasets.
- Library Datasets scans HF hub cache, `HF_LEROBOT_HOME`, and `dataset_roots` (e.g. `D:/datasets/lerobot`). Click fills Record repo/root.
- Library Videos plays merged or original camera files.

## 2026-09-18 (library / capture / joints)

- Preset Duplicate copies a named preset.
- Network MJPEG serves `/video`, starts capture if needed, and the camera card shows the URL.
- From follower / From leader / Load only fill slider targets; Apply or dragging moves, Apply slews in 2.5s.
- Sliders dim unless they match follower pose; teleop/record/rollout follow the follower live.
- Cameras sit in Hardware at the bottom of the sidebar.
- Header: scan, relax, teleop/record/rollout (icon becomes stop while active), rec, auto, stop, E-STOP.
- Local dataset/model library on the left; record creates/selects a dataset; episodes can be previewed, reordered, deleted.
- Record: num_episodes, episode+reset on one row, fps/format/path, resume, streaming encoding.

## 2026-09-18 (header / env / estop)

- Rollout/teleop share the monitor process. Sibling lerobot venv had CPU torch; CUDA request now errors instead of falling back.
- Header icons: scan, teleop, record, rollout, stop. Sidebar start/stop removed.
- E-STOP bypasses the command queue. Pose can be copied from follower or leader.
- Logs are selectable; Copy button; WS does not wipe an active selection.

## 2026-09-18 (tasks UI)

- Preset Save/Load/Delete stay on one row (`preset-btns`).
- Hardware arm/leader pick likely serial adapters; status shows connected port, adapter name, and device id.
- Record has episode_time_s and reset_time_s; Next ep cancels an in-progress reset.
- Deploy/rollout renamed to Rollout. Duration no longer uses `.split` (gutter hover). Camera width/height uses `.pair`.
- Rollout extra key/value table is stored, shown in Info as CLI flags, and applied onto policy config when the field exists.
- Each task Info block lists the equivalent CLI, one flag per line.

## 2026-09-18

- 建立 `lerobot-monitor` 仓库：FastAPI + 独立控制线程 + 相机线程。
- 空闲时持续读关节、推 MJPEG；任务（jog / teleop / record / rollout）互斥。
- Session 写 CSV + MP4，不依赖 LeRobotDataset。
- 实机验证：COM6 follower 已连上，idle 30 Hz 读关节；front MJPEG 29.7 fps；side 流未开所以显示 no signal。服务不依赖 record/teleop 进程。
- 下一步：side 相机在 win_cam_server 里单独开流；可选把 session 导出为 LeRobotDataset。
