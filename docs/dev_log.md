# Dev log

## 2026-09-22（Monitor 工作区标签化）

- Library 的 Videos、Datasets、Snapshots、Models 改为单内容区标签页；右侧 Joints、
  Tasks、Hardware、Model Debug 改为编辑器式任务标签页，移除了侧栏纵向堆叠。
- 标签状态由通用 `initTabList` 管理，支持鼠标、触摸、左右方向键、Home、End，并分别
  通过 `lerobot-monitor-library-tab` / `lerobot-monitor-side-tab` 恢复上次选择。
- 每个任务面板拥有独立滚动容器；Library 原有折叠轨道、异步刷新、表单绑定和
  `data-panel` 契约均保持不变。
- 右侧主标签固定为 Joints / Record / Rollout / Debug / Hardware；Teleop 命令预览
  放到 Record 页下方；E-STOP 与新增的 Resume torque 统一放在顶栏，任务脚本折叠项
  由 `Info` 改名为 `Command preview`。
- 浏览器验证覆盖 1440×900 与 1024×900：标签切换、刷新持久化、任务面板滚动和
  Hardware 长标签完整显示正常，窄屏页面横向溢出为 0。
- 验证：非仿真测试 `133 passed, 2 skipped`；完整测试中 5 个 `test_sim` 项因当前
  venv 缺少 `scservo_sdk` 未通过，与本轮 UI 改动无关。`node --check`、HTML 标签栈
  检查和 `git diff --check` 通过。

## 2026-09-22（Rollout 连续推理回归修复）

- 修复 rollout 预测图引入的实时推理回归：控制循环不再每 0.5 s 额外调用一次
  `predict_action_chunk()`。SmolVLA 等 chunking policy 的 `select_action()` 本来就会
  填充 action queue，现在 overlay 直接读取该队列中尚未执行的同批动作。
- 预测 chunk 仍以单调 ID、真实推理完成时间和 policy FPS 发布，但只在主推理刷新 action
  queue 时更新；模型调用次数与未启用 overlay 时相同。
- 删除 `rollout.prediction_interval_s` 与 `rollout.prediction_chunk_size`：前者会制造额外
  VLA 推理并挤占控制线程，后者会截短真实队列、在两次刷新之间误报预测断档。
- 删除上一轮手写的异步 producer，rollout 改为通过 LeRobot 的
  `create_inference_engine()` 创建 `SyncInferenceEngine` 或 `RTCInferenceEngine`。
- `inference.type`、`inference.rtc.*` 和 `inference.queue_threshold` 现在会解析为
  `RTCInferenceConfig`。RTC 路径会把 `rtc_config` 安装到 policy，并调用
  `init_rtc_processor()`，因此 UI 参数不再被静默忽略。
- 已用缓存中的 `rubatotree/so101_classify_the_blocks_smolvla_512` 在 CUDA 环境验证
  `load_policy → create_monitor_inference_engine → reset/start/resume/stop`，实际得到
  `RTCInferenceEngine`。engine setup 异常现在同时记录完整 traceback。
- 修复 `_rollout_hw_features` 实例字典遮蔽同名方法导致的
  `TypeError: 'dict' object is not callable`；缓存字段更名为
  `_rollout_hw_feature_spec`，并增加真实覆盖 `_start_inference_engine` 的回归测试。
- 恢复非 RTC 的虚线预测：同步 engine 没有 `ActionQueue`，现在回退读取 policy 自身的
  `select_action()` action queue；该路径只做 telemetry 映射，不执行额外推理。
- rollout 加载 repo id 前先用 `snapshot_download(local_files_only=True)` 解析本地 HF
  snapshot；cached repo 和本地目录都不再进入网络 fallback。Policy path 增加 cached
  policy picker，Models 点击仍写入具体 snapshot 路径。picker 显示模型名、类型、来源
  和本地路径，支持过滤、方向键、Enter、Escape 与点击外部关闭。
- cached 解析进一步改为直接扫描 `models--org--name/snapshots`，不再调用
  `snapshot_download`。SmolVLA 的 `vlm_model_name` 和 preprocessor
  `tokenizer_processor.tokenizer_name` 也替换为本地 VLM snapshot，避免 Transformers
  再次解析 backbone repo。
- E-STOP 和 Disconnect 调整为先关闭力矩/释放串口，再收后台 producer，避免线程 join
  延迟安全动作。
- 验证：`tests/test_policy.py` 与 `tests/test_loop.py` 共 41 项通过；排除当前 venv
  缺少 `scservo_sdk` 的 `test_sim.py` 后，全套为 `129 passed, 2 skipped`。尚未执行
  SmolVLA + 真实 SO-101 的长时 rollout，需在硬件侧确认动作连续性。

## 2026-09-21（Monitor 页面 GPU 降载）

- 相机 MJPEG 改为可视区懒加载：主相机与 Hardware 小图只有进入视口且页面可见时才设置
  `src`；面板折叠、滚出视口、进入 Replay 或页面隐藏时立即移除 `src`，关闭浏览器端持续
  解码和 MJPEG 连接。
- 主相机的 IntersectionObserver 改为观察卡片容器，而不是初始 `display:none` 的图片
  元素，避免懒加载形成“未进入视口所以永不设置 src”的 no signal 死锁。
- 页面隐藏时暂停 Replay 视频与时钟，停止接收状态后的重绘；恢复可见时只应用最新一帧
  状态，并按原播放状态恢复视频。
- 实时 Chart.js 更新合并为 150 ms 一次，WebSocket 突发消息不再逐条触发两张 canvas
  重绘；Replay 拖动仍保留即时更新。
- Chrome smoke `6/6` 通过：小图懒加载、折叠断开、页面隐藏断开全部流、恢复重连、图表
  节流且无 console error。

## 2026-09-21（Disconnect 安全降级）

- 新增 `_can_control_follower()`：只有 follower 已连接、非 E-STOP、无 pending 任务，且
  bus owner 属于 hold/monitor/teleop/record/rollout/jog 时才允许执行 relax-release。
- `disconnect_robot` 在不可控状态下跳过 relax，直接释放串口；relax 过程中若读取或写入
  失败，也会立即降级为直接断开，避免控制线程卡在 `jogging` 且 disconnect 请求不返回。
- `_release_follower` 统一收口断开后的 slew 状态、mode 与等待中的 API reply，确保总线
  已断连或发送失败时仍能返回 `ok`。

## 2026-09-21（Rollout 预测对照、模型库与 chunk 评分）

- 修复 Snapshot 与 Replay 标题同时显示：`replay-badge` / `snapshot-badge` 都由
  `snapshotActive` 派生，并补上 `.replay-tag.hidden` 的实际样式所有权。Chart.js 图例过滤
  改为读取 `legendItem.datasetIndex`，模型预测虚线不再进入图例。
- Model Debug 的每台相机新增输入开关；没有勾选相机时 Run 禁用，运行时只上传所选相机。
  有 replay command reference 时返回并展示 chunk score、MAE、RMSE、DTW 与参考覆盖率；
  snapshot 或 reference 不足时明确显示不评分。
- Models 支持 Hugging Face 搜索、remote/revision 编辑、显式更新和本地路径/model card
  拖拽；同一 repo 或本地路径采用 upsert，不再重复追加。拖拽入口同时提供文件选择器，
  `.json` 可读取 `repo_id`、`remote`、`path` 或 `_name_or_path`，`.txt` 可读取纯文本地址。
- Rollout 预测 chunk 增加单调 ID 和实际推理完成时间；重叠的新 chunk 覆盖旧预测，预测
  可用时间晚于上一段时形成红色断点。两张图共用相同 overlay，新预测不会因时间戳相同而
  被错误去重。
- 验证：排除 `test_sim` 为 `127 passed`；新增 metrics、model registry/API、prediction
  timing 测试。Chrome 浏览器 smoke 通过 `7/7`，覆盖模式标题互斥、相机开关、图例过滤、
  虚线保留/红色断点和 model card 地址解析；`node --check` 与目标文件 `git diff --check`
  通过。`test_sim` 仍有当前 venv/仿真环境下的 1 个既有断言失败，与本次改动路径无关。

## 2026-09-21（Blender 多相机发现与远程 MJPEG 接入）

- 修复“Blender 已上线但 Monitor 搜索不到相机”：Monitor 现在会从虚拟机械臂注册表读取
  `cameras[]`，旧协议只有单数 `camera` 时自动回退；每路 Blender 相机注册为独立的
  `RemoteMjpegCamera`，并保留用户已有的启用、主视图与策略输入设置。
- 远程相机使用增量 JPEG SOI/EOI 分帧、读取超时、单帧大小上限和指数退避重连；对外
  与本地 `DeviceCamera` 提供相同的 `latest_jpeg/latest_bgr/latest_rgb/wait_for_frame`
  接口，因此显示、录制与策略输入链路无需分支。
- `CameraHub.rescan()` 只清理本机 DirectShow/V4L 设备，后台线程负责远程列表同步；
  `/api/scan` 与 `/api/cameras/rescan` 会立即同步远程相机。远程相机拒绝修改分辨率、
  焦点和网络流，API 返回稳定的 400。
- Web 端远程相机卡片显示 Blender URL 与连接状态，隐藏本地宽度、端口、对焦和网络流
  控件；策略配置直接使用远程 `url`，不再拼 `localhost:<port>`。
- 验证：新增解析、注册、移除、本地重扫保留、MJPEG 分帧和只读 API 测试；当前运行中的
  Blender 流实测可发现 `blender_sim_follower_camera_1` 并收到 `640×480` JPEG 帧。
  `test_sim` 中依赖 `scservo_sdk` 的 5 个既有用例在当前 venv 仍因缺少该可选依赖
  无法运行。

## 2026-09-21（Snapshot 库、可编辑备注与 VLA Model Debug 完成）

- Snapshot 采用“一个目录一条记录”：`snapshots_root/<id>/snapshot.json` 是唯一 manifest，目录名即权威 ID，扫描时忽略 JSON 内的旧 `id`、跳过缺失或损坏的目录，并校验 ID 与解析后的路径都落在 `snapshots_root` 内。相机 JPEG 与 `preview.jpg`（首张相机图缩放到宽 ≤ 320）通过独立路由以 `FileResponse` 返回。
- 新增 `GET/POST /api/snapshots`、`GET/PUT/DELETE /api/snapshots/{id}`、`POST /api/snapshots/{id}/duplicate`、`GET /api/snapshots/{id}/camera/{key}` 与 `GET /api/snapshots/{id}/preview`。创建与推理都使用 JSON + base64 JPEG，不引入 multipart 依赖；单张 ≤ 8 MiB、相机数 ≤ 12、解码总量 ≤ 48 MiB，超限返回 413。相机 key 归一化为 `[A-Za-z0-9._-]`，冲突时追加 `-2`。
- `JsonStore.library_overrides[kind][source_id]` 保存 video/dataset 的 note 与 description，`/api/videos`、`/api/datasets`、`/api/episodes` 合并返回；episode 的 `source.description` 以 override 优先于数据集自带描述，删除 video/dataset 时同步清理 override。Library 行内可编辑 note，episode 标题区的 description 就地编辑。
- `ControlLoop` 增加 debug lease：`debug_lease_acquire` 在 `mode == "idle"`（含 hold）或 `offline`（未连接 follower）且无 pending 任务时授予 token，不要求 follower 在线；`display_mode()` 与 `bus_owner()` 返回 `debug`，lease 生效时不再发送 hold 位姿。teleop、record、rollout、jog、resume、capture 入口被拒绝，E-STOP、Stop 与任务切换会清除 lease，follower 断连不会取消只读推理。
- `policy.predict_action_chunk` 优先调用 `policy.predict_action_chunk` 并按 `(B, T, A)` 截断到 `chunk_size`，不支持或返回非法形状时回退逐帧 `select_action` 并标记 `degraded`；两条路径共用 preprocess/postprocess、`observation_to_pose` 关节映射、输入关节补齐与截断逻辑，推理前统一 `reset()`。
- `POST /api/debug/infer` 复用 `_get_or_load_policy` 的策略缓存，在 `asyncio.to_thread` 中执行推理，返回 `strategy`、`degraded`、`latency_ms`、`fps`、`actions[{t_s, joints}]` 与 `warnings`；lease 冲突返回 409，策略加载或推理失败返回 400，`finally` 释放 lease。推理默认不向机械臂发送任何动作。
- Web 侧：顶部新增快照按钮（回放/快照视图从 `<video>`/`<img>` 抓帧并按 `obs.*` 插值采样关节，硬件视图取 `/api/state` 关节与相机卡片抓帧，无可用来源时禁用）；Library 新增 Snapshots 分组，支持编辑、复制、删除与 Refresh 重扫；快照视图复用主屏，隐藏 transport，静态相机图 + 两点扁平状态线 + chunk 图。
- Model Debug 面板支持 preset 保存/加载/复制/删除、模型下拉、task、device、extra 参数、chunk_size、fps 与 `source key → observation.images.*` 相机映射。Run 需要同时具备关节来源与至少一帧相机图像；Send first step 复用 `/api/joints`（`duration_s: 0, live: true`）。
- 预测 action chunk 以同色虚线叠加在 command action 图上，回放从当前 elapsed 向右展开，快照从 0 起整段显示；切换 episode、打开 snapshot、拖动时间轴、离开回放或重新 Run 都会清空 chunk。debug 的 extra 参数行不再触发 rollout UI 持久化。
- 验证结果：排除 `test_sim` 后为 `105 passed, 2 skipped`；包含 `test_sim` 时为 `128 passed, 2 skipped, 5 failed/errored`，非通过项全部来自当前 venv 缺少 `scservo_sdk`。`node --check src/lerobot_monitor/web/static/app.js` 与 `git diff --check` 通过。
- 验证边界：本轮未在真实机械臂与相机环境执行快照抓帧、note/description 行内编辑、chunk overlay、Send first step 与多视口视觉 QA；`test_sim` 因当前 venv 缺少 `scservo_sdk` 无法运行（与本次改动无关）。硬件直连推理不在本期范围内，需先保存快照再调试。

## 2026-09-21（回放时间轴与 Windows 断连稳定性完成）

- 全局顶栏移除了进度条与回放 seek；Replay 改为紧凑 transport，播放/暂停、回到开头、唯一主时间轴、时间读数和 Exit 集中在顶部。底部 Replay footer 与重复操作入口已删除，主视频区不再为第二套控制栏预留高度。
- Joint state / Action 的白色当前时间线改为 DOM overlay，由统一 episode clock 更新，避免每帧重绘完整 Chart。canvas 支持鼠标和触摸直接拖动 seek，同时保留 Chart.js hover tooltip。
- Scrub 生命周期以 pointer id 和交互来源为所有权边界；多指针输入、`pointercancel`、`lostpointercapture` 与窗口级释放只结束匹配的拖动，不会错误恢复播放或留下粘滞状态。
- Windows 资源收尾覆盖应用 lifespan、日志 handler、MJPEG 流、WebSocket 与 `RuntimeHub.stop()`：客户端断开和应用关闭均能撤销运行时所有权、停止后台循环并幂等释放资源，迟到工作不能复活旧状态。
- 验证结果：排除 `test_sim` 后为 `74 passed, 2 skipped`；其余可运行测试及差异检查通过。
- 验证边界：完整 `test_sim` 因当前环境缺少 `scservo_sdk` 未运行；按用户要求，本轮未进行浏览器检查或多视口视觉 smoke。上述两项仍是后续环境验证事项，不能视为本轮已覆盖。

## 2026-09-21（第二阶段：回放体验与双采样率完成）

- 回放交互收敛为明确的关闭、浏览和回放状态：Episode 标题栏新增关闭入口，Exit 与其共享完整清理路径；Library 折叠为窄轨道。episode 主行默认进入查看，编辑只由独立编辑按钮触发，避免查看与修改误操作。
- 顶栏和主回放区提供同步 timeline；joint state/action 图移除底部范围条，改用白色当前时间游标，hover 显示对应时间和值。回放区、Episode 元数据和 Library rail 已完成响应式适配；上一轮多视口浏览器 smoke 通过。
- `/api/episodes` 与前端统一消费 source metadata，Episode 面板可稳定展示 title、subtitle 与 description，并保留缺失字段的兼容回退。
- 录制将 `action_fps`、`video_fps` 与 rollout 的 `policy_fps` 解耦；旧 `fps` 仍可兼容解析，新建与 resume 均校验并延续已有采样率语义。writer 使用 action/video 独立时钟和计数，视频补帧不增加 action 样本。
- 多相机晚接入记录真实时间 offset，并按 episode-relative 时间对齐；session/episode/preview 元数据报告真实帧数、速率、duration 与晚接入信息，不再用补齐后的表象计数覆盖实际采样事实。
- Episodes/Preview 使用 safety generation：切换来源、关闭 Episode 或退出回放会使旧请求失效，迟到响应无法重新打开面板或覆盖当前预览；机械臂重放的后续调度同样受关闭状态约束。
- 最终验证：`65 passed, 2 skipped`；JavaScript `node --check`、Python compile 检查和 `git diff --check` 均通过。多视口 smoke 采用上一轮已通过结果。
- 剩余验证边界：尚未进行真实机械臂与多相机的长时间 soak test。硬件时钟漂移、相机驱动抖动、持续编码负载、磁盘吞吐与长录制后的 resume 仍需实机验证。

## 2026-09-21（录制、数据集与回放交互收敛）

- 录制入口统一为显式的新建/续录语义：续录必须绑定用户选择的本地 Video；界面在提交前显示最终目标目录，Record、Capture 与 Teleop auto-record 共用 `root`、`repo_id`、视频开关和编码参数契约，后端命令错误不再以成功状态静默返回。
- 录制媒体链路补齐 `video=false`；每路相机和 merged 视频维护独立帧计数，相机晚接入时以首帧回填、短时掉线时重复末帧，确保多路视频与 episode 时间轴等长。元数据采用同目录唯一临时文件原子替换，新 session 目录以原子占位避免同秒并发冲突；续录会结合磁盘残留 episode 选择下一个编号，避免覆盖崩溃前数据。
- Library 固化为 `Library → Episodes → Replay`：选择 Video/Dataset 只加载 Episode 列，只有 episode 的播放按钮进入主屏 REPLAY；退出回放后保留当前来源与 episode 列。Episode 名称、任务和备注保存在 Monitor store，删除或重排后按旧、新索引映射 overrides。
- 回放主屏过滤 merged 视频，提供播放/暂停、回到开头、三处同步 seek、前后 episode 与默认关闭的机械臂动作重放。机械臂重放仅允许 idle/hold 所有权，并以 generation 丢弃退出回放、Relax 或任务切换后的在途请求。
- LeRobot 数据适配支持 v2 episode 文件与 v3 分片 parquet：统一 Pandas/Arrow/list series，按 metadata 边界抽取 episode，将非零或异常时间戳归一为从零开始的单调时间轴；纯 series 数据集也可进入回放。API 边界把 `NaN`/`Inf` 转成 JSON `null`，缺失关节值不再导致 Preview 500。
- Videos、Datasets、Models 独立异步刷新并带 loading/error/empty 状态；请求使用 generation 防止旧响应覆盖新选择。Chart.js 不可用时降级为非阻断占位，扫描、解析与文件探测移入工作线程，避免阻塞 WebSocket、视频和控制请求。
- 修复任务生命周期竞态：record/teleop/rollout/capture 使用 start generation 校验 Stop 后的所有权；旧 policy loader 不能激活或覆盖新 rollout，policy load 的全局 stdout 捕获串行化。E-STOP 先设置急停状态并立即关闭力矩，不等待 policy 生命周期锁。
- 录制写入与 Video 删除/episode 删除或重排共用 mutation ownership；活动 writer 持有 lease，管理接口无法在检查后与 recorder 创建发生 TOCTOU 竞争。
- 浏览器 smoke（Chrome headless，1440×900，8092）通过：Video 选择只展开 Episode，episode play 进入 `REPLAY`，Exit 退出回放但保留 Episode 列；Capture payload 正确保留自定义 root、`video=false` 与 encoder threads；未出现页面异常或 5xx。
- 验证：项目 venv `49 passed, 2 skipped`，跳过项来自未安装的 pandas/pyarrow；完整可选依赖环境 `51 passed`。同时通过 `node --check src/lerobot_monitor/web/static/app.js` 与 `git diff --check`。

## 2026-09-18 (episode column + replay controls)

- 布局改为 `Library | Episodes | Cameras | Side`；点击 Videos / Datasets 只加载 Episode 列，点击 episode 行的 play 图标才回放。
- Episode 行右侧图标：play、edit；仅 Video 额外有 delete、拖拽排序。单击行只展开编辑框。
- Episode 名称/任务/备注存进 Monitor 的 `store.json`（`episode_overrides`），不改原始数据；`/api/episodes` 合并，`/api/preview` 返回 `episode_name` / `episode_note`。
- 回放主屏过滤 `merged`，顶部为 Play/Pause、回到最开始、机械臂重放开关（默认否）；下方 Joint state 与 Control state 各带时间条，三处 seek 同步。
- 底部按钮：SCAN、RELAX、PLAY/PAUSE、REWIND TO START、CONNECT ARM、EXIT；Stop 退出回放并调用 `/api/task/stop`。
- Models 扫描本机 policy（hub cache / `HF_LEROBOT_HOME` / `models_roots`），点击填入 Rollout policy path。
- Library 三列独立请求并立即渲染，policy 扫描不再阻塞首屏；轮询 in-flight 合并；重载后按持久化选择恢复 Episode 列。
- 验证：`node --check app.js`、`uv run pytest -q`（31 passed）。8091 无 pandas/torch，图表与机械臂回放需在带完整依赖的实例上验证。

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
