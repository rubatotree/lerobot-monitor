# Dev log

## 2026-09-30：Hugging Face 上传鉴权：镜像重定向吞掉 Authorization

- 诉求：往 `rubatotree/pick-the-block-hd` 上传数据集时报 `401 Unauthorized ... https://huggingface.co/api/repos/create` + `Invalid username or password`；要求修好 HF 验证相关问题、优先按环境变量 `HF_ENDPOINT`/`HF_HOME`/`HF_TOKEN` 工作，并直到数据集真正上传成功。
- 根因（实测，不是猜）：本机 `HF_ENDPOINT=https://hf-mirror.com`，而镜像只服务下载——`POST https://hf-mirror.com/api/repos/create` 实测返回 **308 Permanent Redirect → `https://huggingface.co/api/repos/create`**。httpx 在跨源重定向时按规范丢弃 `Authorization`，于是写入请求到达 Hugging Face 时已是匿名，服务端回 401 `Invalid username or password`（报错里的 URL 因此显示 huggingface.co 而不是镜像）。三个本机 token（注册表 `HF_TOKEN`=MainToken2、`stored_tokens[MainToken]`、`HF_HOME/token` 的 `hf_oauth_*`）用 `whoami-v2` 全部 200、用户都是 rubatotree：**凭据没问题，是写入走错了 endpoint**。之所以旧代码必踩：`upload_dataset_folder`/`upload_hf_model` 调的是 huggingface_hub 的模块级 `create_repo/create_commit/upload_folder`，这些函数的 endpoint 只能来自进程级的 `constants.ENDPOINT`（即 `HF_ENDPOINT`），无法逐次指定。
- 修法（`library.py` 一处入口，读写分离）：`huggingface_endpoint()` 读 `HF_ENDPOINT`（下载/搜索照旧走镜像）；`huggingface_upload_endpoint()` 供写入——`HF_UPLOAD_ENDPOINT` 显式覆盖优先，否则当 `HF_ENDPOINT` 是已知只读镜像（`hf-mirror.com` 及其子域）时回落到 `https://huggingface.co`，自建 Hub 不受影响；`huggingface_token()` 按 `HF_TOKEN` → `HUGGING_FACE_HUB_TOKEN` → `huggingface_hub.get_token()`（登录存储）解析；`huggingface_write_api()` 返回 `HfApi(endpoint=写入 endpoint, token=已解析 token)`。`dataset_hub.upload_dataset_folder`（create_repo/list_repo_files/create_commit）与 `model_hub.upload_hf_model`（create_repo/upload_folder）全部改走该 api，并在传输前调用 `assert_huggingface_write_access()`（`whoami`）：凭据缺失或被拒立即报错并点名 `HF_TOKEN`/`HF_UPLOAD_ENDPOINT`，不再等 1.8 GB 传完才 401。`__main__` 改成 `os.environ.setdefault("HF_HOME", …)`（环境变量优先于配置里的 `huggingface_home`），并在启动横幅加一行 `hf: cache=… endpoint=… upload=… token=env HF_TOKEN|login store|missing`（只报来源，不打印令牌值）。
- 端到端实测：重启 Monitor（`D:\repos\lerobot\.venv`，`HF_TOKEN` 取自用户环境变量）后横幅为 `hf: cache=D:\Cache\huggingface endpoint=https://hf-mirror.com upload=https://huggingface.co token=env HF_TOKEN`；`POST /api/datasets/rubatotree/pick-the-block-hd/upload` 于 17:14:40 开始、17:37:51 结束：`status=done 1767.4/1767.4 MB files=254/254 pct=100`，远端 `repo_info` 核对 **255 文件 / 1,853,224,685 字节**（254 个本地文件 + Hub 自动加的 2504 字节 `.gitattributes`，差值恰好相等），`sha=69f420030801`、public、lastModified 与完成时刻一致。反向路径同样实测：把 `HF_TOKEN` 换成无效值后 `upload_dataset_folder` 在任何 commit 之前抛出指向 `HF_TOKEN`/`HF_UPLOAD_ENDPOINT` 的 `DatasetHubError`（huggingface_hub 自身也确认 `HF_TOKEN` 优先于 `hf auth login`）。
- 验证：`tests/test_library.py` 更新两处上传桩为 `Hub.HfApi` 形状（并补 `HF_HOME`/`HF_TOKEN` 隔离），新增「只读镜像不接写入且写入用 env token」「写入前 whoami 被拒则零 commit 并给出变量名」「读写 endpoint 分流」「token 环境变量优先于登录存储」「write api 固定 endpoint+token」；`tests/test_model_hub.py` 新增模型上传的 env token/写入 endpoint 与拒绝路径两项。同级完整 venv（hub 1.30.0）全套 **569 passed / 1 skipped**；Monitor 自身 `.venv`（hub 2.0.0）`test_library+test_model_hub+test_hub+test_config+test_app` 为 106 passed / 3 skipped / 1 failed，唯一失败是本机既有的环境缺口（`.venv` 无 `torch`，`lerobot.utils.device_utils` 导入失败），与本轮改动无关。ruff（用户全局配置）在这 6 个改动文件上从 43 项变为 45 项：新增 2 项 RUF100（`# noqa: BLE001`/`PLC0415` 在该配置下未启用），与这些文件里既有的 11 处同类写法一致；顺带把两个 hub 模块的 import 改成排序后的多行（少 1 项 I001）。
- 边界：已知只读镜像只列了 `hf-mirror.com`（含子域）；其他只读镜像会照 `HF_ENDPOINT` 写入并在预检处报 401，需要 `HF_UPLOAD_ENDPOINT` 指路——预检错误信息里写了这一条。自建/企业 Hub 语义不变（读写同 endpoint）。Monitor 进程需要重启才能拿到新代码；本次重启以 `Start-Process` 后台方式拉起，stdout/stderr 落在 `.tmp_monitor/`（gitignored）。模型上传路径只做了单测，未在真实模型仓库上跑。

## 2026-09-30：云端推理 other 开销拆解：全链路分相位计时 + 一行耗时日志

- 诉求：Debug 面板里云端一次推理的 `other` 达到 1 s 量级，需要知道它由什么构成，并把各相位（尤其 `other` 里的杂项）打成详细耗时日志。
- 根因（读代码 + 本机实测）：`other = inference − encode − upload − compute − download`，而 `upload/compute/download` 是 `RemoteSession.infer` 对 `[sent_at, received_at]` 的**估计**（`compute_s` 取 worker 的 `compute_seconds`，其余按请求/响应字节比例劈开）。因此 `other` 恰好等于区间外的全部：两次 `debug_lease` 控制循环提交、`_poses`、字节统计用的第二次 `json.dumps(payload)`、以及每次点击都会发生的 session 开关（`open_session` 记进 LOAD 行，`session.close()` 的 DELETE + 心跳 join 落在 `other`）。载荷处理本身不是主因：本机实测 3×640×480 帧的 PNG 编码 70 ms、`json.dumps` 9 ms、服务端 `json.loads` 2.3 ms、pydantic 校验 ≈0 ms、worker 再序列化 4.3 ms、PNG 解码 20 ms，合计约 0.11 s。
- 后端（三段进程各自量自己拥有的相位，全部可选、零口径改动）：新增 `src/lerobot_monitor/timing.py`（stdlib-only 的 `Timing`：`add/span/merge/as_dict`，ms 三位小数）；worker 子进程在 `NativePolicyBackend.infer` 内记 `decode/prepare/policy/emit`（`compute_seconds` 语义不变），父进程 `SubprocessWorker.call` 在 **infer** 时回写 `ipc`（json+管道，其余操作绝不加键）；云服务 `ApiBoundary` 把 body 读完的耗时与时刻塞进共享 scope，`infer` 路由据此产出 `read/parse/service` 并合并 worker 的 `timings`（`worker` 取 `compute_seconds`）后作为响应顶层 `timings` 返回；Monitor 侧 `request()` 新增可选 `timings/sizes`（不传时默认路径一字未改），显式序列化一次（顺带删掉尾部第二次 `json.dumps`）、`httpx.Client` 构造计 `client_transport`、`send(stream=True)` 计 `client_ttfb`、`read()` 计 `client_read`、`json.loads` 计 `client_parse`，并把服务端 `timings` 以 `server_` 前缀并入；`RemoteSession.infer` 记 `client_build/client_encode`，`close(timing=)` 拆出 `client_close_http` 与 `client_close_join`，`debug_infer` 记 `client_open/client_poses/client_close` 并写入 `ActionChunk.timing_ms`；`/api/debug/infer` 追加 `client_lease`（acquire+release 两次控制循环提交）、响应新键 `timing_ms`，并在 UI 日志里输出一行 `cloud infer timing: …`（`_cloud_timing_line` 按墙钟顺序，`hub.loop.log`）。
- 前端：`debug-timing.js` 新增 `DEBUG_PHASES` 表（key/label/parent/bucket/detail）与 `phaseRows`（按墙钟顺序、沿 parent 求缩进、根 `session` 行按整段延迟取占比，其余按 inference），`other` 只由 `lease/build/parse/poses/close` 对账，差额 >0.5 ms 时补 `unmeasured` 行；`app.js` 新增默认收起的 **Phases** 分区（`debugProfileRow` 支持 `kind/depth` → `--indent`）并纳入 Copy 文本；`styles.css` 用 `--indent` 变量缩进、`is-phase` 行的 detail 可省略号收缩；资源版本升到 `20260930-debug-phases`。条形图、`stage_ms`、Cloud legs 与 rollout 泳道口径**一律未动**。
- 端到端实测（本机临时云服务 + 假 worker，真实 HTTP）：`cloud infer timing: client_lease=41ms client_open=955ms client_build=0ms client_encode=0ms client_serialize=0ms client_transport=427ms client_ttfb=24ms server_read=0ms server_parse=1ms server_service=0ms server_ipc=1ms server_worker=21ms server_policy=18ms client_read=0ms client_parse=0ms client_poses=0ms client_close=421ms`。据此得到两条可直接行动的结论：**(1) `httpx.Client(...)` 构造在本机恒定约 360-430 ms**（`verify=False` 时 0.4 ms、单独 `ssl.create_default_context()` 35 ms ⇒ 成本主要来自 CA 包/SSL 上下文），而同一连接池上的请求只要 1-2 ms —— `MonitorCloudClient` 每个请求都新建客户端，一次 Debug 点击（`ensure_loaded` 的 GET/load/轮询 + sessions + infer + DELETE）要付 5-8 次；其中 infer 这次因落在 `[sent_at, received_at]` 内被旧的字节比例估计藏进了 `upload`，close 这次则整段落进 `other`（实测 420 ms）。**(2) `ensure_loaded` 对已加载部署仍提交 load 并每 250 ms 轮询 `/jobs`**，实测 `client_open` 750-955 ms 全部来自轮询粒度（服务端本身 open 只要 3-22 ms）。
- 验证：`tests/test_cloud_worker.py` 补 worker 相位断言、`tests/test_cloud.py` 补 infer 响应 `timings` 断言、`tests/test_monitor_cloud.py` 新增「测量路径记录相位与字节/线上字节不变」与「debug_infer 汇总 client+server 相位且 stage_ms 口径不变」、`tests/test_app.py` 新增「本地路径 timing_ms 为空且不写日志」与「云路径透出 timing_ms 且只写一行日志」；同级完整 venv 全套 **561 passed / 1 skipped**，剩余 1 项 `test_virtual_record_api_publishes_to_library_dataset` 为本机既有环境问题（`D:\Cache\huggingface\lerobot\pick-the-block-hd\meta\info.json` WinError 5 拒绝访问，同一原因让 `/api/datasets` 在精简 venv 下 500）；`scripts/test-debug-timing.mjs` 14/14、`scripts/verify-debug-timing.cjs` 两视口 44/44（新增 Phases 分区默认收起/行序/缩进/配色/`other` 对账/无溢出），截图人工核对 1440×900 与 390×844 的相位树缩进与配色；改动 Python 文件 ruff 无新增（仅修掉自己引入的 F401/F841/PYI034/PYI036）。
- 边界：服务端相位需要把本次构建 Upgrade 到云端才有；未升级时 `timings` 缺失，面板只显示客户端相位、条形图与 Cloud legs 走老口径。`client_encode` 与 `stage_ms.cloud_encode` 是同一段测量的两份副本，不要各自变化。连接复用（一个 client/一次 session 开关）与 `ensure_loaded` 的冗余 load 属于**下一步的修法**，本次只负责量出来并写进日志与面板。


## 2026-09-30：Record 顶栏改为占位网格行，并随 record 模式退出

- 现象一：Stop 退出 record 后顶栏仍在。根因：后端 `_stop_record` 结束后 `mode` 立即回到 idle，但 `_record_snapshot` 仍带着最后的 session（finalizing/completed/error）上报，而前端只在 `phase === "preparing"` 时清理 `liveRecord`，于是「可见」一直成立。修复：`renderRecordTransport` 以 `last.mode === "record"` 或 `task.pending === "record_start"` 为唯一判据，离开 record 模式即清空 `liveRecord` 并隐藏顶栏，重进 record 模式自动恢复。
- 现象二：顶栏绝对定位覆盖摄像头窗口。修复：`.record-transport` 取消 `position: absolute` / `z-index` / `top/left/right`，改为 `#cameras` 网格第 1 行——`#cameras.record-open` 时 `grid-template-rows: auto repeat(var(--cr, 1), minmax(0, 1fr))` 且顶栏 `grid-column: 1 / -1; grid-row: 1`，摄像头窗口在剩余等分行里收缩（`object-fit: contain` 自适应）；`.camera-empty` 在 record-open 时改为 `grid-row: 2 / -1` 避免与顶栏同格；700px media query 里的 top/left/right 一并删除。
- 验证：新增 `scripts/verify-record-transport.cjs` 12/12（合成状态流 + 路由 PNG：idle 顶栏隐藏且 `position: static`；record 帧顶栏可见、卡片 `top ≥ 顶栏 bottom`、顶栏不越出 `#cameras`、卡片高度较 idle 缩小 >20px；Stop 后仍上报 completed payload 时顶栏隐藏、`record-open` 移除、卡片高度恢复；重进 record 顶栏回来且仍不重叠）。回归：`verify-camera-rotation.cjs` 20/20、`verify-ui-fixes.cjs` 15/15、`tests/test_app.py -k "index_page or record"` 2 passed（唯一失败仍是 venv 缺 `huggingface_hub` 的环境用例）。截图 `.agent-progress/record-transport-shots/`。
- 边界：未在真机录制流程验证（合成状态流模拟真值）；数据集 finalize/save 期间顶栏现在会随 Stop 立即消失，保存结果仍记录在 Log / Sessions。

## 2026-09-30：Main view 摄像头窗口旋转（0/90/180/270）

- 主视图 `#cameras` 每张 `.cam-card` 头部新增旋转控件（`⟳ 0°` 循环 0→90→180→270，`aria-pressed` 标记是否旋转）；媒体区包进 `.cam-stage`，只旋转窗口显示。
- 90°/270° 用容器单位换向：`.cam-stage` 设 `container-type: size`，图片 `width: 100cqh; height: 100cqw` 后 `transform: rotate()`，所以竖装相机在横窗里按 `object-fit: contain` 满幅显示，不再被压扁或裁掉内容；180° 只做镜像。
- 选择按相机身份（`device_key`，如 `local:0`）存入 `lerobot-monitor-camera-rotation`，刷新/重启沿用；回到 0° 时删除该条目。视频流、快照抓帧与 `feed_robot` 保持原始方向（`mediaFrameDataUrl` 读原始帧），模型输入不受影响。
- 验证：新增 `scripts/verify-camera-rotation.cjs` 20/20（合成状态流 + 路由两色 PNG；像素断言未旋转红左/蓝右、90° 红上/蓝下、180° 左右互换、270° 红下/蓝上；图片布局盒换向与 transform 矩阵；刷新恢复旋转；390px 无溢出且控件可用），截图 `.agent-progress/camera-rotation-shots/`。既有 `verify-ui-fixes.cjs` 15/15、`test-rollout-lanes.mjs` 13/13、`tests/test_app.py` 41 passed / 1 skipped（唯一失败仍是 venv 缺 `huggingface_hub` 的虚拟录制用例，与本轮无关）。
- 边界：真机相机与多路/远程相机未验证；旋转目前只作用于主视图窗口，Hardware 迷你预览与 replay 视图仍按原始方向显示。

## 2026-09-30：六项 UI 修复（preset 资源、泳道带、arm 默认、页签、复制、白线拖动）

- Preset 载入兜底：`ensureSelectOption` 在 `applyRolloutFields` / `applyDebugFields` / `applyRecordFields` 中，为 preset 里已记录但当前列表缺失的 model path / dataset id 合成 option，`applyLibraryDrop` 也改用同一 helper；`setSelectValue` 不再把拖入过、后来不在列表里的资源静默清空（只做载入兜底，preset 仍手动 Save）。
- Commanded action 泳道带按需保留：`mkChart` 初始 `$laneHeight = 0`，新增 `setRolloutLaneBand`；`updateRolloutLanes` 在有 rollout timeline 时置 64px 并同步 `layout.padding.bottom`，`clearRolloutLanes` 收回（冻结视图保持）。空闲时图表填满整个下方。
- arm 默认值：`refreshPorts` 对 `#arm-port` 使用 `savedPortValue(uiHw, "arm_port", "")`，虚拟 follower 仍是可选设备但不再是默认；`robot-preview.js` 预览电源默认关闭（仅显式存过 `"1"` 才自动上电），本机 `config.yaml` 的 `virtual_follower.auto_connect` 改为 `false`，因此启动后 arm 留空或沿用硬件 preset 端口，不再连接 `virtual://preview`。
- 侧栏 Hardware 页签移到 Cloud 右侧（DOM 顺序即键盘导航顺序，面板顺序不变）。
- 元信息复制：新增 `copyTextToClipboard` / `makeCopyChip` / `markCopyChip`（`navigator.clipboard` + `execCommand` 回退，按钮内 1.2s ✓ 反馈）；Library 元信息行与描述、episode 摘要、debug timing/load progress/model residency、Cloud 面板主机/GPU/模型/任务卡片均加入复制按钮，块级 Copy all 复制 `Label: value` 全量行。
- rollout 白线可拖动：`rolloutWindowSplit` 以「过去占比」拆分窗口（默认比例保持历史行为），拖动只改变过去/未来占比、总跨度不变，比例存 `lerobot-monitor-rollout-now-fraction`，双击复位；`renderLiveFrame` 抽出供动画与拖动共用，泳道窗口跟随同一拆分。
- 验证：`node --check` 三个脚本通过；`tests/test_app.py` 40 passed / 1 skipped（`test_virtual_record_api_publishes_to_library_dataset` 因 venv 缺 `huggingface_hub` 失败，与本轮前端改动无关）；`scripts/test-rollout-lanes.mjs` 13/13；扩展后的 `scripts/verify-rollout-lanes.cjs` 98/98（含空闲收回泳道带、拖动改变窗口比例、跨度不变、比例落盘、双击复位，1440×900 与 390×844）；新增 `scripts/verify-ui-fixes.cjs` 15/15（页签顺序、arm 留空、虚拟预览默认不上电、三类 preset 载入兜底、元信息行/整块与 Cloud 主机复制）。
- 边界：未在真实机械臂/相机环境验证；本机 `config.yaml` 的 `virtual_follower.auto_connect: false` 属本地配置（gitignore），如需恢复「无硬件时自动连接虚拟从臂」改回 `true` 并点开 Arm preview 电源即可。

## 2026-09-30：8A6000-server 初始化云服务

- 诉求：在 `8A6000-server` 上初始化 LeRobot Monitor 的云管理器，数据根目录 `/data2/zhuyutian/lerobot-monitor`（与 4090 同构）。执行面是已在运行的 Monitor（`http://127.0.0.1:8090/lerobot/`，即本 checkout，`/lerobot/static/app.js` 与本机文件 SHA256 一致）；`8A6000-server` 这一行本来就是 `CloudManager` 在 `hosts.json` 缺失时注入的默认主机（root 已是目标路径、端口 8091、`python3.12`），因此不需要、也不能再 `POST /api/cloud/hosts`（重复 alias 会 400）。
- 前置探测（只读）：`python3.12 = 3.12.3`、`uv 0.11.32` 在 `~/.local/bin/uv`（非登录 PATH 之外，`BOOTSTRAP` 有该回退路径）、root 不存在、`/data2` 空闲 159.9 GB、TCP 8091 空闲、8×`RTX A6000`（0-6 忙，7 空闲）。
- 执行：`POST /api/cloud/hosts/8A6000-server/bootstrap` → 任务 `f98b455e37a84a0bae1be4c270af4d42`，20 秒后 succeeded（本地构建 wheel + SSH stdin 传输 + 远端 `uv venv`/`uv pip install '<wheel>[cloud]'` + 启动 daemon），随后 `connect` 返回 catalog。
- 结果证据：release `2b96ab47…`，`installation.json` 指向 `releases/<digest>/venv/bin/python`；root 700、`token` 600；`daemon.json` pid 4154561、`127.0.0.1:8091` LISTEN；服务 `/api/v1/health` `code_hash 472ff1d3…`、`instance_id dd935ab9…`、`active_sessions 0`、`loaded_models 0`、capabilities 四条齐全；GPU 列表 8 行（7 忙 1 空闲），部署 0。Cloud 面板实测切到 `8A6000-server` 显示 CONNECTED、`/data2/zhuyutian/lerobot-monitor · 8091`、`1 / 8 available`、Recent jobs 里该任务 SUCCEEDED。
- 边界：未安装任何 runtime profile（ACT／SmolVLA／PI），未部署模型，因此该机的加载与推理链路仍未验证；4090 未做任何操作（其 `build_outdated` 依旧为 true，面板仍会提示 Upgrade）。

## 2026-09-30：Debug 时间条按真实次序铺满推理各段，明细改为 LOAD → INFERENCE → CHUNK

- 诉求：占比条此前只画 compute/chunk/ghost，上传/下载/杂务没有对应的段；明细表要严格按时间顺序（load → inference → chunk），inference 之下再按 encode → upload → compute → download → other 细分。
- 语义（仍全部在前端派生，后端零改动）：条的分母换成 `barMs = inference + chunk + would-be`，段集合与顺序由 `DEBUG_TIMING_SEGMENTS` 固定为 `encode/upload/compute/download/other/chunk/ghost`；`load/wait` 依旧不进条，`inference = latency − session` 不变，`sessionMs` 为新增字段（`load/wait` 继续按模型加载与租约等待拆分导出）。明细行改为 `load(session)` → `inference(excludes load)` → 缩进的 `encode/upload/compute(gpu)/download/other` → `chunk` → `ghost`，每行占比统一以 `barMs` 为分母（子行直接复用条段的占比，与条严格对齐，子行占比之和恒等于 inference 行）。
- 样式：段配色新增 `is-encode/#91c9e8`、`is-upload/#e0a35c`、`is-download/#c98bd4`、`is-other/var(--faint)@0.5`；chip 圆点同步补齐并删掉 `is-transfer`；明细行 `[data-kind="wait"]` 规则删除（不再产出 wait 行）；资源版本升到 `20260930-debug-timeline`。`CLOUD LEGS`、`REFERENCE` 分区与后端口径未动。
- 验证：`scripts/test-debug-timing.mjs` 重写 5 项（条序与占比、chips 逐段、明细行序/层级/分母、本地无传输段、钳制用例行序）后 11/11 通过；`scripts/verify-debug-timing.cjs` 两视口 38/38 通过（新增「条内逐段次序与占比」「session 不进条」「chips 列全腿」），截图滚动改为把卡片顶部 160px 定格在视口内；人工核对 1440×900 与 390×844 两张图：条序 enc→up→gpu→down→other→chunk→ghost、chips 换行不溢出、明细表 load→inference→子段→chunk→ghost。
- 边界：旧服务器不带 `stage_ms` 时只剩 compute/other 两段（本地模型同样没有传输段，`transfer` 恒为 0）；`download` 这类 10-20 ms 的段在 ~300px 的条上只有 2px（CSS `min-width`），是真实比例，不做放大。

## 2026-09-30：chore 整理：死文件与两个「一直失败」的用例

- 删除 `src/lerobot_monitor/cloud/transfer.py`：0 字节、2026-09-28 写入后从未被任何代码 import（全仓库 grep `cloud.transfer` / `from .transfer` 无命中），是遗留空文件；顺手清掉了先前被句柄占住的空目录 `.pytest-tmp/cloud_rtc_lanes`。
- 那两个「一直测试不通」的用例没有删除，而是修好了（保住覆盖）：`tests/test_monitor_cloud.py::test_cloud_model_registry_migrates_legacy_gpu_binding` 与 `::test_monitor_api_registers_cloud_deployment_as_library_model` 的失败根因不在代码——它们断言 `len(registry.list()) == 1` / `GET /api/models == []` 时把**本机真实 HF 缓存**里已存在的模型也算进去了（本机 `HF_HOME=D:/Cache/huggingface`，扫描出 7 个模型）。两条用例开头按仓库既有约定隔离缓存（`monkeypatch.setenv("HF_HOME", tmp_path / "hf")` + `monkeypatch.delenv("HUGGINGFACE_HUB_CACHE")`），于是任何机器上都确定通过，不再依赖运行环境是否干净。
- `tests/test_native_rollout_profiling.py` 原来裸 `import torch`，在精简 venv 下会让 `uv run pytest tests` 直接停在收集阶段（整轮中断）；改为 `torch = pytest.importorskip("torch")`，与同目录 `test_native_rtc.py` 同一约定——缺依赖时跳过而不是炸掉整轮。
- 验证：同级完整 venv 全套 **558 passed / 1 skipped / 0 failed**（此前固定 2 failed，现在全绿）；monitor 精简 venv `uv run pytest tests` 能跑完（526 passed / 18 skipped / 8 failed，这 8 项是既有的「缺 huggingface_hub/torch」环境失败，在完整 venv 中全部通过），收集阶段不再报错；改动文件 ruff 0 新增。

## 2026-09-30：Debug 时间条只留 GPU 计算，LOAD 与传输移出

- 诉求：LOAD 不该算进 inference 时间、也不该出现在时间条里（只在下方 timing 表）；时间条的 compute 只含 GPU 时间，上传/下载与其它杂务不得混入。
- 语义（全部在前端派生，后端无改动）：`inference = latency − (load + wait)`——会话/部署等待在请求之前发生，先被减去，不再计入 inference；`compute`（条内）= 云端 `stage_ms.cloud_compute`（无 stage 时退化为本地 `compute_ms`），并按 `inference − 传输` 上限钳制；`transfer = enc + up + down`；`other = inference − transfer − compute`，三者在表里对得上 `inference`。时间条现在只画 `compute / chunk / ghost`，`load`、`wait`、`upload`、`download`、`other` 一律不进条。
- 明细表改为层级占比：`inference`（占整段延迟，标注 `excludes load`）打头，其下缩进列出 `compute (gpu) / encode / upload / download / other`（各自占 inference），随后是 `chunk`、`ghost`，最后是条外的 `load (session)`、`wait`；chips 改为 `inference · gpu · transfer · other · chunk`（不再出现 load）。`CLOUD LEGS` 分区与参考对比分区保持不变。
- 样式：条内删掉 `is-wait/is-load/is-other` 配色（已无对应段），刷新 chip 圆点为 inference/gpu/transfer/other/chunk，明细行补齐 `encode/upload/download/load/wait` 的底纹与圆点、子行缩进 8px；资源版本升到 `20260930-debug-gpu-only`。
- 验证：`scripts/test-debug-timing.mjs` 重写为 11 项（条内只有 compute/chunk/ghost、inference 减掉 load、compute 只取 GPU、inflated compute 被钳制、层级占比与子行和等于 100%、本地无传输段、缺 fps/未截断/空输入）全部通过；`scripts/verify-debug-timing.cjs` 两视口 34/34 通过（含「load 与传输不在条内」「chips 拆成 gpu/transfer/other」「timing 子行序与 load 值 800 ms」），并输出截图人工核对层级与底纹；本轮无 Python 改动。
- 边界：云端没有 stage 分段（旧服务器）时 `compute` 退化为 `compute_ms` 并按 `inference` 上限钳制，此时上传/下载无法单列（显示为 `other` 的一部分）；本地模型本来就没有传输段，`transfer` 恒为 0。

## 2026-09-30：Debug 时间条下方的可折叠 profile 明细

- 诉求：时间条下方显示详细 profile 信息；范围与形态定为「时序分段 + 云端网络分段 + 逐关节误差」，且可折叠、默认收起。
- 后端：`ActionChunk` 追加 `stage_ms`——云调试把 `RemoteSession.stages` 的 `cloud_encode/upload/compute/download` 折算成毫秒，本地路径没有分段 profiler 恒为 `None`；`/api/debug/infer` 新增 `stage_ms`（无分段时 `{}`）。逐关节误差其实早已在响应里（`evaluation.joints` 的 `mae/rmse/nrmse/scale`），此前只是没有渲染。
- 前端：`debug-timing.js` 的 `buildDebugTiming` 追加 `detailRows`（wait/load/compute/other + inference 合计 + chunk/幽灵行，占比统一以整段墙钟为分母，与条形一致）、`stageRows`（云端往返内部占比）、`jointRows`（按 nrmse 由差到好排序）与 `evaluationSummary`；`app.js` 用原生 `<details>` 渲染 Timing / Cloud legs / Reference 三个分区，行内以 `--pct` 半透明底纹表达占比，折叠状态记在 `debugProfileOpen` 中、跨重渲染保留但初始均收起；`styles.css` 新增 `.debug-profile*`（分区标题 + 箭头 + 右侧汇总值，行内 10px 等宽数字，沿用 `.debug-eval`/`.task-info summary` 的语言）；`app.js`/`styles.css`/`debug-timing.js` 资源版本升到 `20260930-debug-profile`。
- 测试：`scripts/test-debug-timing.mjs` 新增 4 项（明细行分母与顺序、缺 fps 时 chunk 行为 `—` 且无幽灵、云端占比和为 100、逐关节排序与空数据降级）共 11/11；`scripts/verify-debug-timing.cjs` 追加 10 项检查（默认收起、展开后三张表的行序/数值/label、云端占比、逐关节首行、折叠状态跨重渲染保留、窄面板不溢出），两视口合计 32/32 通过并输出展开态截图。pytest 补 `stage_ms` 断言（本地 `None`、云端 250 ms、路由透出）；既有 `test_static_rate_panel_contract` 里「全文不得出现 `details.open`」的近似断言放宽为只在 rate 面板函数体内断言同一意图（新面板合法使用 `<details>`）；同级 venv 全套 556 passed / 1 skipped（2 项 HF 缓存扫描失败为本机环境既有）；改动 Python 文件 ruff 0 新增。
- 边界：`Cloud legs` 占比以四段之和为分母，与上一行 `compute` 的墙钟值存在毫秒级差异（stage 时间戳不含 `session.infer` 内部少量组装开销），不要对齐两者；`Reference` 分区只在请求带 reference（打开了 episode/snapshot）时出现；`Cloud legs` 需服务器为本构建，旧构建该分区不出现。

## 2026-09-30：Debug 面板推理用时条（含未截取长度）

- 诉求：Debug 页签的推理阶段要有一条条形用时统计，横跨「发起推理 → 动作块结束」，并标出动作块未被 `chunk_size` 截取时本应有的长度。
- 后端：`ActionChunk` 追加字段 `generated_steps`（截断前策略实际生成的步数；追加到 dataclass 末尾，避免破坏位置参数调用）。本地 `predict_action_chunk` 先保留完整张量再切 `chunk_size`；云 worker 的 `debug_chunk` 在切片前记长度，且只在 debug 模式回传 `generated_steps`（`select_action`/`rtc_chunk` 响应不变）；`MonitorCloudClient.debug_infer` 映射该字段（旧服务器无此键则降级为 `None`）；`/api/debug/infer` 返回体透出。
- 前端：新增纯计算模块 `web/static/debug-timing.js`（`buildDebugTiming`/`formatTimingMs`）——wait/load/compute/other 以 `latency_ms` 为上限做钳制（四段之和恒等于整段延迟），chunk 段与幽灵段按请求里的 Chunk FPS 折算；`app.js` 新增 `renderChunkTiming`/`clearChunkTiming`（函数声明，验证脚本直接调用），在 Run inference 开始/成功/失败三处接上；`styles.css` 新增 `.debug-timing*`（全主题变量配色：wait 暗紫、load 紫、compute 蓝、other 灰、chunk 绿、would-be 绿斜纹；10px 条高、3px 圆角、chips 自动换行、`min-width: 2px` 保证极小段可见）；`app.js`/`styles.css` 资源版本升到 `20260930-debug-timing`，并新增 `debug-timing.js` 模块脚本。
- 验证：`scripts/test-debug-timing.mjs` 7/7 通过（冷加载分段、常驻模型 wait/compute 分离、噪声钳制、缺 fps、未截断无幽灵段、空输入、时长格式化）；新增 `scripts/verify-debug-timing.cjs`（真实服务 + Playwright + 合成结果直调渲染），1440×900 与 390×844 共 22/22 通过：段序与占比（load 38.3% / chunk 16.3% / would-be 34.7%）、chips 数值、note 文案、卡片不溢出、清空后隐藏，并输出截图人工核对；同级 venv 全套 556 passed / 1 skipped（2 项 HF 缓存扫描失败为本机环境既有）；改动 Python 文件 ruff 0 新增。
- 边界：云端幽灵段需服务器部署本构建，旧构建只显示到实际 chunk 结尾；`sequential_select_action` 回退路径不写 `generated_steps`（该路径本就只按 `chunk_size` 逐步生成，不存在「未截取长度」）。

## 2026-09-30：泳道内拆分云端上传/下载/计算耗时

- 诉求：斜线泳道的时间 profiling 要把数据上传、下载单独标出来，不能全部算作 inference 时间。
- 现状：云端 RTC 的 block 自上一提交起已带 `cloud_encode/cloud_upload/cloud_compute/cloud_download` 四个阶段，颜色映射也已存在，但阶段子带被图例开关 `inferenceStages`（默认 false）挡住，斜带上只有一个总时长标签。
- 前端：`inferenceStages` 默认改为开启（含图例开关），并在斜带的 inference 行新增逐阶段时长标签（`enc/up/gpu/down + 时长`，只在该阶段自身像素宽度放得下时绘制，行 y=+40，与总时长/+chunk 行错开 12px 防重叠）；阶段配色/简称映射从渲染函数内提到模块级常量（`STAGE_COLORS`/`STAGE_LABELS`/`stageColor`/`stageLabel`/`isCloudStage`），供画布与 tooltip 共用。tooltip 对含云端阶段的 block 把总时长改称 `Round trip`，并新增 `Cloud legs: enc … · up … · gpu … · down …` 汇总行（阶段明细行保留）。
- 图例持久化加了 schema 版本（`CHART_LEGEND_VERSION=2`）：v1 存下的 `inferenceStages=false` 会被丢弃并回到新默认，避免老会话永久看不到阶段带；此后开关照常持久化。`app.js` 资源版本升到 `20260930-cloud-legs`。
- 验证：`scripts/verify-rollout-lanes.cjs` 新增云端 chunk 场景（502 号 block 带四个 cloud 阶段，upload 跨度 1.4s）与 4 项断言/视口：阶段各自成段（upload/compute/download 可见 + block 保留 encode，被 2s 窗口滚出属预期）、阶段标签非空、upload 标签落在自身窗口、悬停 tooltip 同时含 `cloud_upload`/`Round trip`/`Cloud legs`。1440×900 与 390×844 共 84/84 通过；`node scripts/test-rollout-lanes.mjs` 13/13；`node --check` 通过。
- 边界：`up 1.40 s` 这类标签需要该阶段在屏上有足够像素（默认 20s 窗口下云阶段只有几像素宽，此时看颜色分段与 tooltip 数值；缩放到 2s 或阶段较长时显示标签）。窄面板（验证用 1440 布局下 action 图仅 205px）也能画出至少一个阶段标签。

## 2026-09-30：云端 RTC rollout 补齐 chunk 生命周期（泳道恢复）

- 现象：`Model = cloud SmolVLA + inference.type=rtc` 的 rollout 中 command action 图底部泳道带空白（无推理带、chunk 带、重叠与 tooltip）；云端 ACT、本地模型均正常。实测主机目录确认差异来源：`8x4090-server` 的 ACT 部署 `capabilities.rtc_chunk=False`，只能走 `select_action`（云 sync 的 PolicyWorker 早已上报事件），SmolVLA 部署支持 `rtc_chunk`。
- 根因：`RolloutTimeline` 的 block 只由 `_bind_rtc_events` 创建，而它只被本地路径调用；`RemoteRTCInferenceEngine` 既不发出 chunk 生命周期事件也没有 `chunk_observer`，`_tick_rollout` 因此走「只激活已有 block」的队列轮询分支，`snapshot()` 返回 null → 前端 `buildRolloutLanes` 直接不画泳道。
- 修复（客户端合成；不改会话协议、不要求升级服务器，旧构建照常工作）：`RemoteRTCInferenceEngine` 新增 `RemoteChunkEvent`（`kind`/`actions`(关节空间 pose)/merge receipt/`action_index`/`stage`）与 `chunk_observer`，在 `_run` 内按 native 语义发出 `started`、`ready`（steps=完整 horizon）、`accepted`（steps=入队步数、`prefix_trimmed`=delay、`replaced`=合并锁内旧 chunk 未消费步数）、`session.stages → stage`，推理异常发 `failed`，`get_action` 发 `consumed`。loop 侧 `_bind_rtc_events` 成为两种引擎共用适配器（新增 Mapping 行投影与 `stage` 分支），云端 RTC 分支改为调用它，标志 `_rtc_native_events` 更名 `_engine_chunk_events`。于是 `_tick_rollout` 的 preview 排空、goal chunk/index 与 `note_dispatched` 通路对云端 RTC 一并生效。
- 前端零改动：快照字段与本地 RTC 相同，`rollout-lanes.js` 无需修改。
- 验证：新增引擎事件用例（事件序与 `trim`/`replaced`/消费索引/前缀行、失败路径）与 loop 用例（`_begin_rollout` 云端 RTC → 真实 timeline：kind/steps/active/status/consumed/stages + preview 载荷）；跨层校验把该快照喂给真实 `rollout-lanes.js`，产出 inference/action/planned/wait 相位与 cloud_encode/cloud_compute 阶段带。同级 venv 全套 554 passed / 1 skipped（2 项 HF 缓存扫描失败为本机环境既有，置空 `HF_HOME` 即通过）；monitor 精简 venv 除缺 torch/huggingface_hub 的既有失败外全绿；Node `test-rollout-lanes.mjs` 13/13；改动文件 ruff 0 新增。
- 边界：未在真实 4090 上加载模型跑浏览器 rollout（本机无机器人/相机；部署保持 unloaded，GPU1 正被 Blender 占用），硬件端验收留给用户；云端 RTC 与本地 RTC 一致地不画 planned 尾迹（native RTC 不写 `predicted_steps`）；不发射 `discarded`（云端无 epoch/reset 失效守卫）；推理带含 PNG 编码与往返，宽度大于纯 GPU 计算（与云 sync 口径一致）。

## 2026-09-30：云模型 Load 自动选择空闲 GPU

- 左侧模型库对云模型点 Load 不再先弹 GPU 选择窗：自动拉取主机目录，把负载提交到编号最小的空闲（健康且非 busy）GPU，并直接进入侧边进度跟踪（"Loading … on GPU N"）。弹窗仅保留给"所有 GPU 都被占用、需要选择共享哪张卡"的场景；主机无健康 GPU 时不弹窗，直接在卡片与日志报错。点击期间沿用请求守卫（按钮显示 Requesting…），提交前同步移交守卫给加载动作，双击不会产生两次加载。
- 验证：新增 `scripts/verify-cloud-load-autopick.cjs`（真实 Monitor 服务 + Playwright 路由桩 + 合成 WebSocket）8/8 通过——有空闲卡时不弹窗且请求体 `device` 指向最低编号空闲卡、全忙时弹窗且所有选项禁用/零提交、无健康卡时报错且不弹窗不提交、无浏览器异常；截图确认卡片红字错误与 "Loading Cloud SmolVLA on GPU 1" 进度条。`node --check` 通过。
- 边界：云面板（cloud-panel.js）自带的 Load 对话框是另一条路径，未改动；本机模型 Load 本就不弹窗。

## 2026-09-30：云端构建版本漂移可见化

- 现象：云端 4090 的 `classify-blocks-2-1-smolvla` rollout 仍无虚线与泳道。排查确认本机包哈希 `cf9db7c8…`（含两轮修复）与服务器 `daemon.json` 记录的运行构建 `79de4867…` 不一致——服务器从未升级，旧 worker 不回传 `queued_actions`，云端预览与 `predicted_steps` 数据链全部断在源头。ACT 本机运行不受影响。
- 修复：`code_hash` 从 `cloud/__main__.py` 上移到 `cloud/__init__.py`（包根与相对路径口径不变，跨机器可比）；manager 在 connect 时记录服务器健康报告的 `code_hash`，`hosts()` 新增 `build_outdated`（仅已连接且两端哈希都已知且不同为真）；云面板主机行显示 "outdated build" 徽标与升级提示（cloud-panel.js 属未提交工作，改动随其保留在工作区）。
- 运维路径：云面板点 Upgrade 部署当前构建 → 重新加载模型（worker 按加载启动）→ rollout。有活动会话时服务器拒绝升级，需先停止。
- 验证：`test_cloud_manager + test_cloud_api + test_cloud` 73 passed；新增漂移用例覆盖一致/失配/断开三态；`node --check` 通过；ruff 0 新增（`__main__` 的 I001 与测试桩 PYI 均为既有）。

## 2026-09-29：Rollout 底部泳道斜带适配完整预测计划

- 现象：SmolVLA rollout 时 command action 图底部的泳道只有蓝色推理斜带和约 2.7px 的绿色碎段，没有长斜线计划尾迹；ACT 正常。用真实检查点驱动引擎+PolicyWorker+时间线复现数据：`n_action_steps=1` 时每个 block 的 `steps=accepted_steps=1`（引擎只上报**入队执行**的步数），前端 `chunkSpans` 按 `steps × step_s` 只能画出 66ms 的段。
- 修复：`_RolloutBlock` 新增 `predicted_steps`（完整预测 chunk 长度），由 `note_inference_end` / `note_chunk_accepted` 记录并随快照发布。本地 sync 由 PolicyWorker 的新回调在引擎 `ready` 事件时读取 `predicted_chunk_total`（chunk 捕获）；无原生事件的路径（云 sync 等）用 `1 + len(preview)` 推出同一值。`steps`/`accepted_steps`/`remaining_steps` 语义不变（执行事实）。
- 前端：`buildRolloutLanes` 的斜带 action 段在 `predicted_steps` 更大时扩展到完整计划长度，活动中的 chunk 因此显示淡色 planned 尾迹，被替换的 chunk 显示 45° replaced 尾迹；底部 chunk 两行带与重叠计数保持执行步数不变。悬停提示在行尾追加 `predicted N`（仅当与执行步数不同）。无 `predicted_steps` 的旧快照渲染完全不变。
- 通用性：任何 policy 只要 chunk 生成交付被捕获（现行全部 chunking policy 均如此），泳道即显示完整计划；与策略类型、n_action_steps 无关。
- 验证：真实 SmolVLA 复现脚本（已删除）显示本地与云 sync 两条路径的 block 均获得 `predicted=50`；前端几何（Node 直跑 rollout-lanes.js）产出 inference/action/planned/replaced 全相位。新增回归：timeline 字段透传 2 项、worker 原生/手动路径 2 项、前端几何 1 项。Monitor venv 相关套件 175 passed / 1 skipped；torch 依赖套件在同级 venv 9 passed；Node `test-rollout-lanes.mjs` 13/13；ruff 改动文件 0 新增告警。
- 边界：云 sync 的 `preview=engine.leftover_poses` 接线与 monitor_cloud 引擎侧改动属于既有未提交工作，未纳入本次提交；云 RTC 引擎仍无 chunk 生命周期事件（泳道缺失为已知空白）。未重启 Monitor 服务，未跑浏览器 smoke。

## 2026-09-29：Rollout 虚线预测适配队列耗尽型策略

- 现象：SmolVLA rollout 的动作图始终没有虚线预测（本机与云端 sync 一致），ACT 正常。根因：两个 SmolVLA 检查点均为 `n_action_steps=1 / chunk_size=50`，`select_action` 入队 1 步随即弹出，预览读取时队列恒为空；既有预览链（RTC 队列 → ACT temporal ensembler → policy 队列）无法看到被丢弃的其余 49 步。
- 修复：新增 `PredictedChunkCapture`，在 `load_policy` 时包装策略的 chunk 生产者（`_get_action_chunk` 优先，否则 `predict_action_chunk`），记录最近一次原始 chunk；`reset` / `drop_queued_actions` 同步清空，超过 10 s 的记录视为过期。消费量由策略自身队列推出（`min(n_action_steps, T) - len(queue)`），`inference_leftover_poses` 将其作为队列之后的最终回退；队列可用时行为完全不变。
- 通用性：现有全部 chunking policy 的生成都路由到这两个函数之一（仅 SmolVLA/XVLA 绕过公开 API 走 `_get_action_chunk`），未来遵循任一约定的策略自动获得虚线预览；无 chunk 生产者的策略保持原行为。遥测失败一律静默降级为空预览，不影响控制。
- 云端：`NativePolicyBackend._queued_actions` 在队列空时回退到同一捕获尾部；**需要重新准备 4090 的推理运行时**（worker 代码哈希已变）才会生效。
- 验证：本机 RTX 4060 真实加载 `rubatotree/classify-blocks-2-smolvla`，sync 引擎每 tick `preview_steps` 由 0 变为 49；`rubatotree/so101_classify_the_blocks_act_512` 回归 99→98→97 逐步收缩（队列路径不变）。新增 7 项回归测试（捕获安装/幂等/reset/drop 清空/过期拒绝/队列优先/worker 回退与过期）。`test_loop + test_monitor_cloud + test_cloud_worker + test_policy` 合计 151 passed / 1 skipped；剩余 2 项失败是本机 HF 缓存污染的既有问题（`registry.list()` 扫到 8 个真实模型）。Ruff 与基线一致（新增 0 项）。
- 边界：渲染链路未改动（虚线前端早已就绪），本轮未跑浏览器 smoke，未在真实 4090 云服务上验证。

## 2026-09-28：模型加载取消与占用释放

- 在功能实现前补充 ROADMAP。目标为模型卡片增加取消加载和释放占用实例，统一复用当前常驻管理器、实例身份及 Rollout / Debug 停止协议。
- 加载取消采用协作式边界：排队项立即失效，正在执行的阻塞调用显示取消中，返回后清理且不得晚发布 ready。不会强行终止 Python 线程。
- 活动实例先停止相关任务，再等待推理线程退出与租约交还，恢复为保留缓存权重的 ready；另行点击 Unload 才卸载。等待期间保留占用状态，执行未退出时不会提前向其他请求交出同一模型。
- 后端增加按模型或可选 `instance_id` 取消加载、释放占用的接口，返回操作数量和最新常驻状态。取消会立即使排队项失效、唤醒等待者；阻塞加载显示 cancelling，完成清理后转为 cancelled。阶段取消异常不会触发网络回退，晚完成结果不会发布为 ready。Hub 模型在加载途中出现缓存目录后仍可按原路径或缓存别名定位；取消清理期间重新加载也不能绕过同一实例的状态。
- Release 先记录具体租约的取消意图，回调即使晚注册也会收到请求；停止命令只匹配该租约，不能误停后来取得实例的新任务。等待票据在状态锁内捕获释放版本，原有等待者不会在 Release 后重新取得实例。Rollout 加载结果同锁发布；Debug 从获取总线租约到计算和最终释放采用完整后台生命周期，HTTP 请求取消、推理锁等待及迟到结果均检查取消状态，租约保留至实际计算退出。
- 卡片增加 Cancel load / Release，提供取消中、停止中、已取消及重试状态；明确 Release 保留权重、Unload 释放内存。请求守卫独立于卡片 DOM，状态刷新重建卡片也不重复提交。状态版本检查防止较慢 HTTP 响应覆盖更新的 WebSocket 状态，按实例操作传递准确身份。
- 独立只读复核通过。门控回归覆盖队列取消、阻塞操作晚返回、阶段检查、取消后重试、回调迟注册、原有等待者和日志窗口、远端缓存别名、Debug 获取/计算期间请求取消、延迟停止命令、跨实例隔离及取消不触发网络重试。
- 首次全量 Monitor 为 417 passed / 1 failed / 1 skipped（170.8 s）。唯一失败是旧模型复用测试桩返回了 Debug token 却未设置真实租约状态；修正测试桩后全部 3 项模型复用测试通过（3.97 s），生产代码未改动。两次结果合计覆盖 418 个不同通过测试、1 个跳过项；首次运行未全绿。跳过仍是 Windows 符号链接权限限制。
- 前端浏览器检查 52/52 通过，覆盖 1440×900 与 390×844（DPR 2）的操作、状态转换、重复点击、接口错误、旧响应、键盘与窄屏布局。截图位于 `C:/Users/Admin/AppData/Local/Temp/lerobot-model-residency-shots/model-actions-*.png`，已人工查看。后端提交 `f8f420f`，前端提交 `91e43fb`。
- 取消采用协作式边界：正在进行的原生加载或推理必须返回后才能完成清理，无法保证立即停止。未重启当前 Monitor 服务、发送实体机械臂动作或通知；进度看板保持关闭通知，同级原生仓库用户文档修改保留。

## 2026-09-28：Rollout 执行速度倍率

- 在功能代码前更新 ROADMAP，随后实现独立 `execution_speed`（默认 1×）。基础 `policy_fps` 保留模型/来源频率含义；`effective_policy_fps = policy_fps × execution_speed` 用于动作消费、同步请求、RTC 延迟折算、插值和预测/斜带步长。机械臂输出、相机和录制频率继续各自配置，权重身份与模型训练配置不变。
- Rollout 新增类似频率面板的速度浮层，预设为 0.25×、0.5×、0.75×、1×、1.5×、2×、4×、8×，支持自定义倍率。显示基础频率、倍率与目标执行频率；运行中编辑明确标记为下一次 Rollout，当前启动参数保持固定。UI 和命名预设保存倍率，旧预设缺字段时恢复 1×；CLI 示例以有效频率生成 `--fps`。
- 倍率须为正且有限，有效频率 0.1–240 Hz。API、命令处理及冷加载完成后的启动均校验有效频率不得超过独立解析的 Arm 输出频率；运行中降低 Arm 输出也在状态变更前校验。错误提示提高 Arm 频率或降低执行倍率，不静默截断。基础频率、倍率与有效频率分别发布在状态中。
- RTC 在控制/读取节拍刷新关节观测，动作消费仍按有效频率的固定时间点调度，避免低速时观测过期和累计时间漂移。同步路径仅在下一执行截止时间采集观测，最多保留一个请求/结果；已返回的结果不会覆盖尚未成功下发的目标。工作线程结果事件可提前唤醒等待，Windows/macOS 短截止时间保留既有控制调度精度。慢推理保持当前动作，后续缓存动作不会集中补发。
- 定向测试 94 项通过，覆盖倍率边界、拒绝操作不改变状态、0.5×/1×/2×/0.75× 的动作顺序与插值、有效频率传递、预测步长、低速新观测、同步迟到结果及结果通知竞态。全量 Monitor 402 passed / 1 skipped（162.70 s，2 条上游警告）；94 项属于全量子集。全量收集后新增的“启动校验拒绝时释放模型租约”测试单独通过。唯一跳过项为 `tests/test_library.py:630` 的 Windows 符号链接权限限制。
- 前端计算测试 5/5、浏览器检查 48/48，使用隔离服务/配置和合成状态覆盖 1440×900 与 390×844（DPR 2）、低速/高速/自定义、非法输入、旧预设与持久化、当前运行和下一次选择分离、Arm 独立性以及启动请求。截图位于 `C:/Users/Admin/AppData/Local/Temp/lerobot-rollout-speed-shots/rollout-speed-1440x900.png` 和 `rollout-speed-390x844.png`。完成独立后端/前端复核，未发现阻塞问题。
- 提交：后端 `fe5a04a`，前端 `8d25b94`。本功能未改动原生 LeRobot，保留同级仓库两项用户文档修改；未重启当前 Monitor 服务、发送实体机械臂动作或通知。倍率表示目标节拍，模型吞吐量与总线性能仍限制实际速度；真实硬件的执行频率和关节跟踪需在下次运行中测量。

## 2026-09-28：Rollout 连续斜带与冻结检查

- 将 action chart 底部改为固定 64 px 区域：同一 chunk 连接蓝色推理、虚线等待、绿色动作与淡色未来计划；实际 `action_end` 决定截断尾部，队列 merge 时间不会提前结束已经取出的目标。重叠使用精确覆盖计数，所有 chunk 固定一行。
- 每次 rollout 固定 `run_id` / `epoch_ts`，旧协议只锚定一次；两图曲线、预测和斜带由同一帧时钟更新。保留原始区间，窗口仅裁剪几何，避免快照频率改变坐标或耗时。缺失快照和同运行重连不重建原点，新运行清理旧预测。
- 线段距离命中覆盖推理、等待、动作及替换尾部，整根高亮并展示完整阶段/GPU/交接/RTC 步数和计划重叠。修复原生 pointermove 对底部区域的排除；详情卡按视口定位，长记录可在斜带上滚轮查看，并提供滚动提示。
- 图例新增持久化 `Inference stages`（默认关闭）与雪花冻结按钮。冻结捕获已绘制时刻的两图、预测、标记与时间轴，后台仍接收有界实时历史；缩放和悬停使用捕获数据，停止后保留，解冻恢复实时，进入 Replay / Snapshot 解除冻结。
- Monitor 遥测提交 `bfffe02` / 原生 `e5315df2` 记录实际队列裁剪/替换、逐阶段主机与可选 GPU 耗时，并区分队列取出与成功下发；发布阶段计入整体推理结束时间。同步 ACT / SmolVLA / Diffusion 的缓存动作归入同次生成，其他策略和 ACT temporal ensemble 保留逐步记录，缓存命中不重复记录原始推理阶段。
- 加载日志已补充 Monitor/原生加载器和 SmolVLA 内部阶段；前端保留既有加载阶段并显示 VLM 缓存解析。加载日志提交为 Monitor `b0bf4f5` / LeRobot `99bb5ec0`；原生 queue/rollout/interactive_rollout 测试 172 passed；Monitor 全量 382 passed / 1 skipped（138.05 s），另有 2 条上游警告。跳过项为 `tests/test_library.py:630` 的 Windows 符号链接权限限制（WinError 1314）；同步分组/保留定向 96 项属于 Monitor 验证子集，不重复计入总数。修改文件 Ruff/diff 检查通过。
- 验证：Node 几何/生命周期分析 12/12；隔离配置/store 和合成 WebSocket 的 Playwright 76/76，包含 1440×900 与 390×844（DPR 2）、完整详情卡视口边界、长阶段滚动、<1 px 时间对齐、缺失/重复快照、运行隔离、冻结后台接收、阶段展开/缩放、停止保留及 Replay 解冻。`node --check`、`git diff --check` 通过。
- UI 截图：`C:/Users/Admin/AppData/Local/Temp/lerobot-ribbons-shots/rollout-ribbon-phases-1440x900.png`、`rollout-ribbon-phases-390x844.png`，对应 `rollout-ribbon-details-*.png` 显示整根斜带详情。前端提交 `0d0fd3f`，静态资源缓存版本更新 `f5b9ec3`。
- 未重启现有 Monitor 服务，未发送实体机械臂动作；保留同级 LeRobot 的用户文档修改。GPU 异步事件使用假事件验证无主动等待；真实硬件 Rollout 的 profiling、控制节拍和长期运行开销需在下次服务重启后的实际运行中观察。

## 2026-09-28：模型导入缓慢排查与诊断补充

- 在线 SmolVLA512 最终耗时 605.817 秒：imports 492.819 秒、构造/权重 111.208 秒，其余约 1.8 秒；加载锁等待可以排除为主要原因。后续 Rollout 日志确认复用模型。
- 同环境独立导入约 10.1 秒；后台常驻加载 53.887 秒、隔离 RuntimeHub 加载 62.297 秒。线程采样确认 VLM/expert 构造时存在随机初始化开销；异常导入未复现，原进程线程栈读取被 Windows 权限拒绝，不能声称已确定具体根因。
- 新增 torch/config/factory/policy 导入子阶段，以及按需 GET `/lerobot/api/models/load-diagnostics`；返回线程名、ID 和最多 48 帧调用位置，不返回局部变量，不保留 frame 或影响状态推送。
- 验证：78 项缓存/常驻/复用/policy/API 回归，加新增诊断 API 1 项通过；JS 语法和 diff 检查通过。真实加载使用原缓存权重，未发送实体机械臂动作。
- 检查恢复条件时发现在线服务已经进入 Rollout，未执行重启。补丁需下次重启生效；后续在异常冷导入期间抓栈，确定具体等待后再做性能修复。详细数据与边界见 `docs/model_load_diagnosis_2026-09-28.md`。

## 2026-09-26：移除加载参数并恢复跨入口模型复用

- Library 的 Load 直接发起加载，不再打开 Settings / Device / JSON 参数弹窗；服务端使用配置设备，运行参数留给 Rollout / Debug。加载 API 保留可选 device，忽略旧客户端的 extra，避免预加载改变后续会话的模型配置。
- 根因证据：`SmolVLA_Test` Debug 预设指向已经不存在的 `C:\Users\Admin\.cache\huggingface`，Rollout 指向 `D:\Cache\huggingface`，两者仓库与提交 `e0202ea0976391e7b7575a897aa575fd5455bef5` 相同。旧路径不能命中当前实例。
- 在现有 policy 路径解析器内恢复迁移后的标准 Hub 快照，复用已有缓存查找与模型校验。仅对不存在的目录处理；严格按 repo + 40 位 commit 查找，不回退到最新版本，已有本地目录继续保留原义。不改写用户预设。
- Debug 完成信息和日志显示 `resident model reused` 或模型加载耗时，直接使用后端 cache_hit / model_load_ms。
- 验证：`test_model_reuse.py`、`test_policy_residency.py`、`test_policy.py`、`test_app.py` 共 71 项通过。新增跨入口回归走真实 Rollout 加载 worker、驻留管理器、Debug API，仅替换模型计算和硬件；验证加载器只调用一次，旧路径命中同一对象。覆盖提交/仓库隔离、已有目录和缺失权重。新测试 Ruff 检查通过；Node 验证一键加载仅发送空参数，JS 语法通过。
- 使用实际 Debug 旧路径做只读解析，确认解析到 Rollout 当前快照且缓存身份相同。未执行真实模型推理或硬件动作，未重启现有服务；后端修复需要重启 Monitor 生效。
- 提交建议：`fix(models): simplify loading and reuse relocated snapshots`。工作区同时存在另一项模型加载诊断改动，未将其混入本任务提交。

## 2026-09-26：SmolVLA512 加载缓存与阶段诊断

- 用户反馈模型加载停在 0/4 数分钟。通过运行服务 `/lerobot/api/models` 确认实际模型为 `rubatotree/so101_classify_the_blocks_smolvla_512`，加载记录 529.162 秒，常驻张量 1,197,716,576 字节。权重文件约 1.20 GB，单文件，配置 `load_vlm_weights=false`。
- Monitor 的 0/4 是四阶段计数，不是四个 checkpoint 分片。旧实现把等待全局加载锁、首次依赖导入等时间也显示成 Reading configuration。单独测量：torch 导入 2.281 秒，configs 2.362 秒，factory 5.669 秒，本地配置解析 0.036 秒；这些是新诊断进程的结果，原始 529 秒没有阶段明细。
- 确认缓存缺陷：VLM 缓存复用了 `resolve_cached_policy_path`，其 `is_policy_dir` 同时要求完整权重和 LeRobot input/output features，导致普通 SmolVLM 配置/processor/tokenizer 缓存被拒绝。嵌套的 AutoConfig/AutoProcessor 未收到外层的 local_files_only。运行期设置 `HF_HUB_OFFLINE=1` 时，已经导入的 huggingface_hub 1.30.0 的 `constants.is_offline_mode()` 仍为 False。
- 独立进程将 httpx.Client.send 替换成记录后拒绝请求的函数，确认原加载访问 config.json、processor_config.json 和 additional_chat_templates 列表；后者的调用栈来自 AutoProcessor。通过显式本地 VLM 路径对照，完整 CUDA 加载 41.286 秒，无 HTTP；其中 VLM 构造 20.033 秒、expert 构造 7.915 秒、safetensors 装载 2.427 秒。
- 修复：新增 VLM 专用缓存解析，使用 Hub 官方 `snapshot_download(local_files_only=True)` 跟随缓存 refs；配置-only backbone 不要求第二套权重，需要 backbone 权重时仍验证文件/分片齐全。向 AutoConfig/AutoProcessor 和 tokenizer 传本地 snapshot 路径；缓存缺失的本地 SmolVLA 加载在构造前报明确错误。没有修改全进程 Hub 常量，也没有跳过模型初始化或改变精度。
- 可观测性：区分 waiting、imports、cache、config、weights、device、processors；API 提供 elapsed_ms、phase_elapsed_ms、stage_durations_ms，前端显示当前阶段与耗时，后端记录阶段完成和失败时的阶段。修正离线环境变量 helper 的说明，避免将其误认为对已导入 Hub 的联网屏障。其他策略的嵌套联网行为未在本轮全面审计。
- 验证：42 项针对缓存、策略覆盖和常驻生命周期的测试，以及 1 项加载/复用/卸载 API 回归通过（共 43 项）；Node JS 语法与 git diff 空白检查通过。修复后使用原模型 ID（无 VLM 路径覆盖）的独立进程 CUDA 加载为 39.857 秒：imports 9.058 秒、cache 0.019 秒、config 0.022 秒、模型构造/权重/首次设备迁移 30.511 秒、device 0.005 秒、processors 0.242 秒。HTTP_ATTEMPTS=[]，常驻 acquire_ready 复用同一对象；测试进程退出后释放自身显存。
- 限制：新测量是文件缓存已存在时的进程冷启动，不是重启操作系统后的冷磁盘基准；没有原始慢加载的逐阶段追踪，不能把全部差值精确归因于网络。运行服务未重启，现有已加载模型继续可用；下次重启 Monitor 后生效。进一步提速可评估避免完整 backbone/expert 的随机初始化，但须先验证 checkpoint 覆盖率、共享参数和非持久 buffer。

## 2026-09-26：复用 LeRobot 的异步边界修复

- 按用户新约束调整设计：继续使用 RTCInferenceEngine / SyncInferenceEngine / ActionQueue 及原处理器，不新增 IPC、独立动作调度器或另一套模型缓存。依赖 LeRobot 提交 `79f1e10d`。
- 相机 `show_main` 与 `feed_robot` 解耦；原子读取帧与接收时刻，图像复制移出相机锁，转换在推理线程完成。新增配置 `observation_max_age_s=1.0`、`camera_max_skew_s=0.25`、`inference_timeout_s=30.0`，示例配置和 README 已说明其为可调整的任务参数。
- RTC 生产者完成后处理、CPU 拷贝与有限值校验后才发布，raw/absolute prefix 使用同索引快照；控制侧取动作/发布关节不等待生产者锁。reset 撤销 epoch 后在原线程串行清模型状态。首块没有旧动作执行，延迟裁剪为零。
- Monitor 图表消费原生 chunk 事件，动作真实发送后才标 active；预测姿态在生产线程展开，不再在控制线程读整块张量。每次 rollout 新建独立时间轴，迟到回调不能污染新运行。同步模式的图像准备、frame 构建和动作转换移入既有 PolicyWorker。
- 验证：Monitor 全量阶段 346 passed / 1 skipped；新增真实引擎集成及最终配置/控制改动定向 76 passed。LeRobot 队列、RTC、interactive rollout、相对动作共 197 passed；含慢观测不阻塞已有动作、reset 不并发改模型、首块完整、非有限动作立即失败。前端 CI 4 项命令通过，LeRobot 修改文件 Ruff 通过；Monitor 历史严格 lint 诊断相较基线无新增，新测试文件 lint 通过。
- 后续：A06 的 deadline 跳步/限幅与逻辑时间完整对齐、GPU/真机 SLO 与模型热切换验收、H06 跨重启暂存恢复。独立进程只保留为候选；未重启现有服务或移动真实机械臂。

## 2026-09-26：常驻策略的运行配置事务

- 每次运行从 checkpoint 运行参数基线构建候选配置，先类型转换、正值检查和 LeRobot 配置校验，成功后再应用；失败不会污染常驻模型。清空覆盖恢复 n_action_steps、SmolVLA num_steps、Pi0/Pi05 num_inference_steps 等原值。
- ACT temporal ensemble 直接用 LeRobot 的 `ACTTemporalEnsembler` 重建；结构字段不能在已有权重上原位修改，明确报错。配置对象本身保持身份，避免模型持有旧配置引用。
- 验证：新增覆盖恢复、失败回滚、ACT 融合器开关/系数、SmolVLA 步数测试；Monitor 全量回归阶段 346 passed / 1 skipped，最终配置及 rollout 定向 76 passed。

## 2026-09-26：Bug 审核与异步 Rollout 设计

- 审计 monitor `c75aa41` / LeRobot `8f1d64cd`。当前 RTC 已在后台线程推理，端到端控制隔离尚不完整：总线线程还有相机复制/颜色转换、动作转换、预测展开及共享张量队列访问。
- 从 ROADMAP/dev_log 核对既有修复与实机待验项。仓库未找到独立 bug list，GitHub Issues 查询为空，未声称覆盖尚未提供位置的外部清单。
- 内存假对象复现：隐藏主画面使 `feed_robot=True` 相机不再进入输入；旧队列消费可把尚未 merge 的新 chunk 标为 active；清空运行覆盖后 n_action_steps 仍保留上次覆盖值。另登记观测新鲜度、时间对齐和 reset 所有权风险。
- 新增 `docs/bug_audit_2026-09-26.md` 与 `docs/async_rollout_design.md`，更新 ROADMAP。目标方案为 spawn 推理服务、CPU 动作时间线、完整观测版本、有界 IPC、明确的 RTC 前缀/延迟契约及模型常驻缓存代理。
- 验证：父目录既有 `.venv` 执行 loop/policy/worker/residency/overrides/timeline/cameras 共 120 passed；一条既有 pytest cache 写权限警告。未运行真实模型或移动机械臂，功能代码未修改。
- 下一步：先修复相机路由和配置事务；再用 fake worker 验证 CPU 时间线/缺货/epoch 失效，随后迁移真实 RTC 与缓存。完整实施与验收顺序见审核文档 M1–M6。

## 2026-09-25（Rollout 推理时间轴可视化）

- 新增线程安全 `RolloutTimeline`：记录 RTC/sync 推理 `[start, end]`、chunk steps/step_s、执行交接 `active` 与失败状态；20 s 输出窗口、最多 256 块，超限优先淘汰未激活旧块。
- `policy.py` 只包装 `predict_action_chunk` 并对共享策略实例幂等绑定，保留 `__signature__`；RTC 的 chunk 交接由引擎队列 `qsize` 下降或 `index` 归零确认。`PolicyWorker` 对 sync 调用逐次打点，loop 在 worker 结果被消费时记录绿线；嵌套的内部 chunk refill 不重复计数。
- 快照新增 `rollout_timeline`，非 rollout 为 `null`。`rollout_inference_ms` 优先使用 timeline 的真实 chunk 推理耗时，失败或钩子不可用时回退原实测值，遥测异常不影响控制节拍。
- 新增 `rollout-lanes.js` 纯函数模块与 action chart 插件：绿线只由有 steps 的有效交接生成；chunk 长度按 `steps × step_s` 绘制为两行带，重叠区叠加 45° 阴影，失败块使用红色描边；推理带独立成行，缺失 steps 只回退 prediction 长度并标记 `chunk:false`。
- 新增 `chunk input`、`chunk span`、`chunk overlap`、`inference` 四个默认开启图例开关并持久化；action chart 预留固定 26 px 泳道，tooltip 在泳道 hover 时显示 chunk steps、时长、起点、推理耗时和重叠时长。

- 验证：后端遥测定向回归 `93 passed`；Node `scripts/test-rollout-lanes.mjs` 为 `6 passed`；Playwright 临时配置/store 与合成快照在 1440×900、390×844 为 `38/38`，覆盖画布绿/蓝像素、四项图例、开关刷新持久化、lane tooltip、移动端无横向溢出并输出截图。完整 LeRobot 可选依赖环境 `317 passed, 1 skipped`；monitor 自带精简 `.venv` 缺少 `huggingface_hub`，其中 4 项数据集/HF 测试按环境依赖预期失败，其余 `305 passed, 9 skipped`。`node --check`、Python compile、`git diff --check` 与现有 CI 静态脚本通过。

## 2026-09-25（ACT rollout 预测曲线与配置重载）

- 根因一：ACT 启用 `temporal_ensemble_coeff` 后由 `ACTTemporalEnsembler` 直接输出融合动作，不再维护 `_action_queue`；monitor 的同步预测读取只支持队列，因此曲线始终为空。现改为直接读取 ensembler 中尚未消费的融合动作，不增加第二次 `predict_action_chunk` 推理。
- 根因二：旧 checkpoint 的 `config.json` 若省略 `n_action_steps`、`temporal_ensemble_coeff` 等 dataclass 默认字段，模型身份计算会丢弃这些显式覆盖，导致既不复用正确配置也不触发重载。现把配置中不存在的覆盖项视为有效差异；覆盖应用后重新执行 `__post_init__`，保证 ACT 的两项耦合约束不会被热覆盖绕过。
- 验证：ACT/策略/缓存/rollout 定向回归 `85 passed`；完整 monitor 回归 `296 passed, 1 skipped, 3 failed`。3 项失败均为 `D:\Cache\huggingface\lerobot` 写权限导致的既有数据集录制测试，与本次 ACT 推理和模型身份改动无关。

## 2026-09-25：GPU 模型常驻缓存

- `RuntimeHub` 统一持有模型驻留管理器：相同实例合并冷加载，不同实例排队；Rollout、Debug Inference 和手动载入共用模型与处理器，任务结束仍保留缓存。使用租约覆盖推理和 RTC 停止收尾；加载中取消只取消任务启动，内存清理使迟到结果失效。
- 模型实例身份依据权重来源、revision、设备和有效模型覆盖参数。接口与 WebSocket 提供实例状态、真实加载阶段、错误、张量显存和进程显存；Library 卡片可选择设置载入、按实例或整卡卸载，局部更新避免刷新焦点。
- 已修复含 `/` 的 Hub 模型 ID 在载入接口中的 404；浏览器在桌面与 390 px 视口下检查了卡片、配置面板、阶段进度、键盘 Escape 和重连后的状态恢复。真实 RTX 4060 上 ACT 与 SmolVLA 同时常驻，张量显存分别约 197.1/1142.2 MiB；连续 Debug Inference 复用模型。SmolVLA 加载期间 ACT 命中推理耗时约 1.3 秒。ACT 卸载后进程已分配显存从 1399.8 降至 1198.0 MiB，再载入约 10.6 秒，验证重新读入权重。
- 虚拟从臂 Rollout 可立即复用已载入 ACT 并启动同步引擎，但当前服务没有 `observation.images.front` 相机源，因此首个动作计算报缺图；完整带图像虚拟 Rollout 待接入相机再验。ACT 首次载入约 375 秒，SmolVLA 约 268.5 秒；此处包含本机首次依赖初始化，不代表纯权重 I/O。全量 monitor 回归 295 项通过、1 项跳过；末次针对补充的模型 ID、删除失效和运行设置测试另行复核。

## 2026-09-25：Replay 倍速播放

- Video/Dataset replay 的进度条右侧新增倍速菜单：常用倍率直接选择，Custom 在菜单内展开数值输入（任意非负有限倍速）。0× 固定当前帧；浏览器不支持的极端媒体速率由统一时间轴驱动视频定位。切换倍速时先结算原速下的播放位置，再按新倍率继续；关节预览和动作控制继续按时间轴采样。
- 验证：Node 语法检查、倍速切换的时间轴/视频速率行为检查，以及 `test_status_without_hardware` 通过。实体机械臂控制效果尚未实机验证。

## 2026-09-24（模型删除）

- 已登记的 `rubatotree/classify-blocks-2-smolvla` 在 Library 的实际 ID 是 `rubatotree-classify-blocks-2-smolvla`。前端此前优先发送 `repo_id`，后端返回 404，日志只显示仓库名。
- 模型卡片改用实际 ID。删除 Hugging Face 模型时删除整个缓存仓库目录（含 snapshots、blobs、refs）；磁盘删除成功后才清理登记与描述，短暂 Windows 占用做有界重试。
- API 回归覆盖登记 ID 与 repo_id 不同、完整缓存清理、删除失败保留登记；前端脚本验证请求 ID。`tests/test_app.py` 为 `33 passed, 1 skipped`，前端脚本、JS 语法与 diff 检查通过。当前机器的原缓存路径已不存在，未在运行中的服务中执行真实模型删除。

## 2026-09-24（仿真 rollout 偶发失败）

- 复现 `test_sim.py::test_rollout_style_action_moves_the_emulated_scene` 的偶发旧姿态断言：修复前连续运行第 5 次读到 `shoulder_pan ≈ 0.04°`，目标为 `30°`。
- 根因是 Feetech `sync_write` 广播没有回执，`FollowerArm.send_pose()` 返回后 TCP 服务线程仍可能尚未写入目标寄存器。测试现以有界等待确认目标已进入 `MotorBank`，再调用 `tick()` 推进场景；生产控制路径不变。
- 使用仓库根目录带 Feetech SDK 的虚拟环境，专项连续 12 次通过；完整 monitor 测试 `239 passed, 1 skipped`。monitor 自身 `.venv` 缺少 `scservo_sdk`，不适合运行该集成测试。

## 2026-09-23（观看中删除与数据集上游设置）

- 删除正在观看的数据集时，确认后先退出回放、卸载视频，等待媒体释放和界面绘制，再发送删除请求；Windows 临时共享/拒绝访问（WinError 32/5）的后端重试延长至约 5 秒。前端顺序脚本验证“确认 → 退出 → 绘制 → 删除”，取消确认时不退出。
- 数据集编辑窗口把本地目录、Hugging Face upstream、revision 和新仓库 public/private 拆开。Hub 缓存快照不预填为用户指定的本地目录；更新 upstream 时保留真实本地目录，切换到新仓库时不沿用旧仓库缓存。
- 扫描发现的本地目录不再把路径推导出的卡片 ID 当作已设置的 upstream；设置 upstream 后才启用下载，上传还需有效本地数据。新建空数据集可选择可见性，留空 Repo ID 时保持纯本地。
- 上传任务保存点击时的 private 选项，新建 Hub 仓库时传给 `create_repo`；已存在的 Hub 仓库可见性由 Hub 保持。模拟 Hub 测试覆盖 private/public，API 回归覆盖独立路径和上游。
- 验证：数据集相关测试 `30 passed`，后补 WinError 5/32 专项测试 `2 passed`；前端删除顺序脚本与 Chrome 元数据窗口检查通过，JS/Python 语法与 diff 检查通过。完整测试 `237 passed, 1 skipped, 1 failed`（专项测试增例前）；失败的 `test_sim.py::test_rollout_style_action_moves_the_emulated_scene` 单独复跑仍失败，未涉及本次数据集改动。

## 2026-09-23（本地数据集删除后清理）

- 定位扫描型本地数据集删除时的 404：文件夹已删除，`DatasetRegistry.delete()` 却再次扫描该路径，导致后续记录与界面清理中断。现改为使用删除前取得的条目完成清理。
- 删除目标正在回放的数据集前先关闭 episode 视图，暂停视频、移除 `src` 并调用 `load()` 释放浏览器媒体请求；后端仅对 Windows 临时共享冲突做有界重试。
- 删除成功时立即从前端缓存移除卡片并重新扫描；删除请求返回 404 时也刷新列表，以清理已不存在的旧卡片。
- 本地扫描型数据集删除及首次 WinError 32 后成功重试的 API 回归通过。完整测试 `236 passed, 1 skipped`；`node --check`、Python compile 与 `git diff --check` 通过。当前机器上指定的 `plus80` 目录已不存在，未删除旁边的 `_old` 目录；未在仍运行的浏览器会话中复测实际视频句柄释放。

## 2026-09-23（数据集、上传下载与同步故障修复）

- 核对工作区已有的异步 DatasetTransferManager、进度卡片、v3 task 读取、来源保存与 Hub 缓存删除改动；保留原有未提交成果。
- 修复 Hugging Face `/datasets/org/repo` URL 解析；显式下载强制刷新缓存。绑定普通本地目录的数据集下载后在同目录复制完整快照并替换原目录，失败时保留旧目录；上传按本地文件集合分批提交并清理云端旧文件，保留 Hub 的 `.gitattributes`。
- 拒绝空路径上传，限制普通目录符号链接；Hub 快照可读取指向同仓库 blob 的链接。数据集来源改名时保留本地目录与描述，阻止指向已注册 repo；传输结束不再恢复已删除或改源的条目。
- 删除数据集时先检查传输状态并删除文件，成功后才移除 Library 记录；传输最终状态在记录持久化完成后才对前端可见。前端避免旧状态回退，修复删除前丢失选中态，以及来源变化后选中 ID 未更新。
- 回归：完整 monitor 测试 `235 passed, 1 skipped`；唯一跳过项是 Windows 缺少创建文件符号链接的权限（WinError 1314）。`node --check`、Python compile 与 `git diff --check` 通过。未执行真实账号上传或大体积 Hub 同步。

## 2026-09-23（3D 虚拟从臂预览）

- 新增 `VirtualFollowerArm` 与 `virtual://preview`：无硬件时默认自动连接，真实从臂
  显式连接时优先接管；关闭 Arm preview 会停止渲染、释放 WebGL context 并断开虚拟
  follower。E-STOP 和 force disconnect 不自动恢复虚拟连接。
- 新增 `RobotModelRegistry` 与 `robot_model.json` 清单。内置 SO-101 URDF/STL 资产，
  支持本地目录和 Hugging Face 仓库安装；REST 覆盖列表、搜索、安装、更新、删除、
  激活、manifest 与安全文件服务，限制路径穿越、符号链接和包体积。
- 新增 `robot-preview.js`：本地 Three.js/URDFLoader 场景、OrbitControls、视图预设、
  双击聚焦、动作来源解析、Auto 关注跟随和虚拟腕部相机。`app.js` 只推送状态、replay
  时间与 hover 上下文，预览模块自行处理插值、回退和 GPU 生命周期。
- 底部布局新增可拖动宽度分隔条；宽度支持键盘调整、双击复位和 localStorage 持久化。
  预览关闭时移除 canvas；页面不可见或预览不可见时停止 RAF，腕部相机按 10 FPS 渲染。
- 验证：`211 passed`，`node --check`、Python compile、`git diff --check` 通过；
  Playwright 桌面与 900px 窄屏 smoke 通过，覆盖模型加载、Auto joints/prediction
  徽标、宽度持久化、腕部相机、关闭/重开虚拟 follower 和无页面横向溢出。

## 2026-09-23（Library 资源管理器重构）

- Library 标签顺序固定为 Models / Datasets / Videos / Snapshots，无保存偏好时默认
  Models；列表标题只显示资源名，结构化 metadata 独立显示在标题下方，每行两个字段。
- metadata 保存时间、来源、本地路径、上游 repo_id、revision、policy type、episode
  数量、fps、task、robot type 等字段；搜索栏同行提供设置按钮，可选择每个标签实际
  展示的字段并持久化。
- 多 note 模型收敛为单个 `description`，只在资源被选中后显示并可原地编辑；旧 note
  在加载时合并迁移到 description。Episode 管理器中的 description 入口已移除。
- Models、Datasets 改为 Add/Scan 图标工具栏。Add 使用搜索式弹层；Datasets 的 Add
  提供 Hub 下载与 New empty dataset。空数据集写入有效 LeRobot v3 骨架，并按当前
  JOINT_ORDER 与启用 Main view 相机生成 action/state/video features。
- Videos 每个 recording 只显示一行，点击直接进入主预览并使用预览栏切换 episode；
  Datasets 继续保留 Episode 列。预览栏 Snapshot EDIT 与 Debug 内隐藏编辑器已删除。
- 四类资源都可从资源管理器删除。Models、Datasets 统一提供编辑来源、上传覆盖云端和
  下载覆盖本地三个操作；无 repo_id 时上传按钮标暗，无上游关联时下载按钮标暗，本地
  cache 与上游资源使用同一套行外观。新增 DatasetRegistry，并让扫描资源和注册资源
  使用统一列表、编辑、删除、下载与上传语义。
- Videos 启动时只保留第一个 episode，后续录制结束也会自动裁剪；旧 episode 缺失的
  `duration_s` 会从 `joints.csv` 最后时间戳恢复，并回写 episode/root meta。
- Record / Rollout / Debug 的 Library 依赖改为下拉选择：Record 只列 Datasets，
  Rollout 与 Debug 只列 Models；路径保存在隐藏字段中，不再要求手动输入。Record 和
  Rollout 标题移除，Teleop 标题改为与 Task 一致的标签样式。
- Models/Datasets 卡片支持从 Library 拖到右侧对应选择器；接收下拉使用虚线高亮、
  左侧强调色和拖入态反馈，类型不匹配时拒绝。Record 的 Dataset 已移动到 Task 上方。
- Record 的 New dataset 选项会立即创建并选中空数据集。录制 task 会写入每个 episode
  meta；episode 展开详情新增 Task/Name/Note 元信息摘要。点击 episode 主行会同时
  播放并展开只读详情，编辑图标才展开编辑表单。
- Episode 查看器对所有来源提供编辑、删除和拖动排序；Video 使用原有物理 episode
  操作，Dataset 使用持久化 episode view override 调整顺序和隐藏项。
- 移除 Library 15 秒自动轮询，保留初始加载、操作后刷新和手动 Scan/Refresh。
- 新增 `GET /api/datasets/search`、`POST /api/datasets/download`、
  `POST /api/datasets/empty`；数据集下载后必须包含 `meta/info.json`，空数据集使用
  临时目录原子创建。
- 验证：排除 `test_sim.py` 为 `171 passed, 2 skipped`；完整测试为 `203 passed, 1 failed`，
  失败为既有 `test_sim` 模拟总线初始位姿断言。`node --check`、
  Python compile、`git diff --check` 通过。
- Chrome/IAB 浏览器检查通过：metadata 位于标题下方且每行两个字段、设置弹层、单个
  来源编辑按钮、上传/下载/删除按钮、选中后 description、Datasets Add 菜单、Videos
  直进预览且无 Episode 列与无 console error。空数据集由 `LeRobotDatasetMetadata`
  实测读取成功：0 episodes、20 fps、单路 front video key。
- 尚未在真实串口/相机环境验证新增弹层触控拖动、真实 Hub 大体积数据集下载和录制后
  元信息 note 的实际内容；这些边界需要在设备与网络环境确认。

## 2026-09-22（Joints 面板显式连接与同步源）

- Joints 面板新增同步源、Speed cap 和 Serial control。默认 `Follower / 180°/s /
  serial off`，加载页面不再因拖动滑条隐式连接 COM 口。
- `Follower` 为可编辑目标通道；串口关闭时只更新 UI，开启后通过 `/api/joints` 发送。
  `Leader`、`Joint state`、`Command`、`Predict` 均为只读源；leader relay 要求 leader
  已在 Hardware 面板手动连接。
- 控制线程新增 live target 与 `max_speed` 限制，manual 输出和 leader relay 都按真实
  `dt` 逐步逼近目标。Stop、E-STOP、Disconnect、relax-release、任务启动和 follower
  异常会清除 live target，防止迟到命令继续控制硬件。
- `/api/status` 增加 `leader_joints`；`/api/joints` 增加 `source` 与 `max_speed`，
  非法 source 和非正/非有限速度返回 `400`。`ui.joints` 持久化 source 与 speed cap。
- Prediction 源在没有新 rollout chunk 时保留最后值并显示 stale，不自动回退其他源。
- 移除 Apply targets / From follower / From leader 与 Hold pose 控件；Serial control
  改为带状态指示的主按钮，Speed cap 位于其下方并使用自定义轨道/滑块。Joints 配置只存
  `ui.joints`，pose preset 仍只保存关节目标。
- Serial control 开启且不在 teleop/record/rollout 等任务模式时，当前 Joints command
  会立即发送，Speed cap 变化也立即以新上限重发；无需额外 Apply。
- 回放 dataset/video 时，`Joint state` 与 `Command` 同步源改为按统一白线
  `vizState.elapsed` 从当前 episode 的 `obs.*` / `act.*` series 线性插值；拖动、播放、
  暂停和跳帧都会同步刷新 Joints 滑条，不再读取实时硬件状态。
- Serial control 开启后，Joints 当前同步到的 state / command / prediction pose 会统一
  下发 follower；回放游标处的数据因此可直接驱动机械臂。Joints 面板的滑条、同步源、
  Speed cap 和 Serial control 操作不再调用 `exitReplayForControl`。
- 关闭 Serial control 改为调用 `/api/hardware/force_disconnect`，立即释放 follower，
  不再执行 relax-then-release 过渡。
- Replay 顶栏移除 `arm off` 与 snapshot `edit`，EXIT 移入全局顶栏并在 replay 期间显示。
- 修复 Serial control 下发时后端短暂进入 `jog` 导致 replay 被强制关闭的问题；状态推送
  不再把 `jog` 视为任务模式，只有明确启动 loading / teleop / record / rollout 才关闭
  replay 或 snapshot 观看模式。
- Joints 右栏顺序改为 Serial control、Speed cap、无文字分隔线、Sync + 同步按钮、关节
  列表。新增同步按钮可把当前源立即填入 command，并修复仅切换到 Follower 源时只更新
  targets、不更新滑条的问题。
- Sync 改为与 send/control 类似的双控：`sync` 单次拉取，`auto` 灯持续跟随。所有源
  行为一致并允许 Follower 自动同步；手动编辑或 preset load 自动关闭 auto，None 不锁定
  auto。send/sync 与 control/auto 分别使用统一宽度，Sync 与 auto 同高对齐。
- Serial control 右侧新增 `send` 单次发送与 `control` 持续输出灯。默认 control 熄灭，
  滑条编辑、自动同步和 preset load 不再隐式下发；send 或 control 开启才发送，手动
  Sync 只填充面板值，由 send/control 决定是否写到串口。
- Serial control 缩窄并与右侧按钮同高；send 使用上传箭头、sync 使用下载箭头表示相反
  方向；serial control 与 sync 的持续输出灯统一显示 `auto`；移除 Sync 上方分割线。
- relax / preset slew 时后端状态报告 `motion_locked`，前端停止 Joints 自动输出和
  leader relay；竞态中的 live jog 由后端返回 `ignored`，不再刷 `cannot live jog` 日志。
- 验证：非仿真测试 `163 passed`；完整测试 `193 passed, 1 failed`，唯一失败仍是
  `test_sim` 的模拟总线初始位姿断言，与本轮 Joints/API 路径无关。`node --check`、
  Python compile 和 `git diff --check` 通过。Chrome smoke 10/10 + 7/7 + 3/3 + 5/5 + 1/1 + 4/4 + 7/7 + 3/3 + 1/1 + 3/3 + 1/1 + 6/6 + 6/6 + 1/1 通过，覆盖默认
  不连接串口、串口关闭拖动不发请求、五种同步源、设置持久化、prediction stale、
  串口开启即发送 command、Speed cap 重发、回放白线插值、桌面/390px 窄屏与浏览器
  console，以及回放 state 下发、开关 serial 不退出 replay、即时 force disconnect 和
  EXIT/arm/edit 布局、`jog` 状态不再关闭 replay，以及 Sync 按钮对 follower / command /
  replay state 的即时填充，以及 None、编辑暂停、全绿自动恢复、手动同步恢复、preset
  load 视为编辑、视频窗口 EXIT 对齐、全绿自动恢复、Follower 手动同步、preset 串口
  下发、motion lock、send 单次发送、control 默认关闭与持续输出、control 关闭时 preset
  不写串口、按钮几何、图标方向与 auto 文案。
- 验证边界：尚未连接真实 follower/leader 执行 relay、速度上限和反复连接/断开 soak；
  硬件动作连续性、COM 口释放和主动臂读数抖动仍需在设备环境确认。

## 2026-09-22（Hardware preset、固定工具栏与设备身份恢复）

- Library 的四个搜索框合并为标签栏下方的共享 sticky 搜索框；关键词按标签存入
  `lerobot-monitor-library-search`，Models 的 Hub 搜索仍独立。
- 右侧新增共享 preset 工具栏，五页通过 `PRESET_KIND_BY_TAB` 映射到 pose / record /
  rollout / debug / hardware。下拉切换只选择，Load 才应用；Save、Rename、Duplicate、
  Delete 改为图标按钮，新建与重命名使用内联弹层。标签选择、每页滚动位置和 preset
  选择分别持久化。
- 新增系统 preset `Disconnected`：store 自动补齐，首次选中但不执行；禁止覆盖、
  rename 和 delete，可 duplicate 为用户 preset。用户显式 Load 后才写入
  `ui.active_hardware_preset`。
- `/api/ports` 和相机 snapshot 增加 `identity` / `device_key`。Hardware preset 绑定串口
  hwid、虚拟 role+robot_id、远程 robot_id+camera_id 和本地 index/name；相机 label
  重命名不再影响 preset 匹配，旧 COM 被其他 hwid 占用时会 skipped 而不是误连。
- 新增 `POST /api/hardware/apply` 与 `hardware_apply` 控制命令。应用仅在 idle/offline、
  无 pending、writer、debug lease 或 release 时执行；逐项记录 success / skipped / failed
  到 UI 日志和 `run.log`。未列入 preset 的现存相机会被停用。
- RuntimeHub 启动时读取活动 preset 并排队自动恢复；首次没有活动 preset 时只记录
  `auto-restore skipped`，不连接任何设备。
- arm/leader 端口行移除 `Port` 文案与两枚连接按钮，改为设备下拉框加单个电源图标；
  空选项表示该 preset 不包含对应设备，图标只反映并切换实际连接状态。
- 修复 arm 电源按钮误调用 `/api/arm/*` 导致的 404；实际请求按 arm→`/api/robot/*`、
  leader→`/api/leader/*` 映射。图标改为网格居中，并新增 `Force disconnect` 直接
  释放两条总线，不执行 relax。
- Hardware preset 载入使用可中断状态：Load 图标在请求期间变黄；再次点击会设置
  `_hardware_apply_force`，使 relax 等待立即退出并继续强制载入。
- Arm/Leader 排版收敛为单行设备行：标题、端口选择、连接电源图标和各自的 force
  disconnect 图标同排，连接详情位于该行下方；`/api/hardware/force_disconnect`
  支持 `role=arm|leader|all` 单独释放。
- 移除控制命令中的隐式连接：`_require_follower_connected` /
  `_require_leader_connected` 只检查连接状态并抛出明确错误。jog/relax、resume、
  read pose、teleop、record、rollout 不再调用 `connect()`；实际 HTTP 验证中
  `POST /api/joints` 返回 400，`POST /api/rollout/start` 接受排队后由控制线程记录
  `start rollout requires a connected follower arm`。
- 顶栏任务按钮保持 teleop/record/rollout 文案，仅通过 `task-on` 点亮；点击已激活任务
  不再触发停止。Stop 对慢停止任务采用两阶段状态：第一次 `stop-requested` 变黄并调用
  `/api/task/stop`，第二次调用 `/api/task/force_stop`。
- `force_stop` 会立即脱离 inference engine，把 engine.stop 放到后台清理，同时关闭
  writer 并回到 idle/offline；follower 掉线或读失败时 `_abort_active_task` 会主动结束
  rollout，不再等待后续 tick。
- 顶栏 F/L 改为设备名电源按钮，bus/mode 文本移除；顶栏与 Hardware 面板共用
  `disconnectPending`，第一次软断开、第二次 force disconnect，独立 force 按钮删除。
- F/L 标签移出按钮，按钮宽度随设备名收缩且高度与 HOLD pill 对齐。`refreshPorts`
  使用 `savedPortValue` 区分“字段缺失”和显式空字符串，修复选择 `No device` 后刷新
  又被默认 COM6/COM5 覆盖的问题。
- `.brand` 固定为 260px 轨道（600px 以下为 220px），状态 pill 切换不再改变 F/L
  按钮的横向位置。
- 验证：排除 `test_sim.py` 后 `155 passed, 2 skipped`；`node --check`、Python compile
  通过。浏览器 smoke 覆盖系统 preset、显式 Load 日志、图标 Save/Rename、刷新持久化、
  服务重启自动恢复、1024/390px 无横向溢出；未使用真实串口和相机做换端口 soak。

## 2026-09-22（未完成 HF 缓存导致的 rollout 加载失败）

- `rubatotree/classify-blocks-2-smolvla` 首次加载时只下载了 config 与 processor，
  `model.safetensors` 仍在 `.incomplete` 阶段；旧逻辑只要发现 snapshot 目录就强制离线，
  因而报 `No such file or directory: ...\model.safetensors`。
- `is_policy_dir()` 现在只把 `model.safetensors`、完整的分片索引、`model.pt`、
  `pytorch_model.bin` 或 `adapter_model.safetensors` 视为策略权重，不再把
  `policy_preprocessor_*.safetensors` / `policy_postprocessor_*.safetensors` 当模型本体。
- `resolve_cached_policy_path()` 会校验 snapshot 的权重完整性；未完成缓存不再进入离线
  加载，而是走现有 Hugging Face 下载回退。
- 验证：`tests/test_policy.py` 与 `tests/test_library.py` 共 26 项通过；实际缓存下载完成后
  `model.safetensors` 正确解析。排除 `test_sim.py` 的全量测试为
  `134 passed, 2 skipped, 1 failed`，唯一失败是工作区既有前端 smoke 对
  `chart.$hoverOverlayVisible` 的断言，与本次策略加载改动无关。

## 2026-09-22（实时图表滚动与鼠标交互修正）

- 实时图表整帧显示上一动画帧的时间轴与数据，避免曲线已落后一帧但横轴仍按当前时间
  前进造成的右端闪烁。
- 实时图表按 30Hz 量化刷新，并在右端保留稳定空白；最后一个采样值延伸到当前时间
  槽，避免右端点在约 30ms 周期内漂移后突然跳回。
- 时间轴恢复原来的绝对时间刻度与滚动格式；中间刻度从右侧进入、从左侧离开时按边缘
  距离淡入淡出；透明区根据左右端点文字的实际宽度和中间刻度宽度计算，避免出现位置
  或消失位置与固定时间数字重叠。时间文本变化本身不做淡化。
- rollout 的未来窗口改为仅由当前时间尺度决定，不再随预测 chunk 长度伸缩；now 线
  因此在一次 rollout 中保持固定像素位置，同时仍与时间轴上的当前时刻对齐。
- mode 切换标注相对灰色竖线增加横向偏移，避免竖排模式名与标线相互贴近。
- 悬停提示增加 actual 截止时间：live 图以当前渲染时刻为界，replay 以最后一条
  actual 数据为界；未来时间只显示 prediction，不再回退显示最后一个 actual。
- 悬停提示卡开关改为鼠标中键，仅控制提示卡本身；十字虚线、插值圆点和时间读数始终
  显示。replay 与 snapshot 中这些圆点、以及图表左键拖动的白色游标都吸附到真实
  采样帧，不再做时间插值。
- 左侧图例增加逐项小灯选框，可分别控制 command、prediction、prediction gap、
  mode change、current time 与各关节曲线的显示，默认全部开启。
- 图例小灯缩小为 7px、上移并与图例线条中线对齐，选中状态使用蓝色。
- replay/snapshot 图表鼠标样式由左右箭头改为十字光标，保留左键拖动 seek。
- prediction 起点改用当前图表渲染帧作为基准，不再把状态快照晚于预测记录的负时间
  差带入坐标；预测曲线与 now 线保持同一帧时间基准。
- replay/snapshot 中，在 Commanded action 图上滚动鼠标滚轮可按固定采样帧前后浏览
  动作；向上滚前一帧，向下滚后一帧，播放中的滚动会先暂停。
- 鼠标拖动 seek 或滚轮浏览动作帧时，显式刷新当前鼠标坐标与悬停提示状态，使圆点和
  时间信息立即跟随鼠标，不再等待下一次 Chart.js mousemove。
- 动作图滚轮按标准 100px 刻度累积，每次达到一个刻度只前进或后退一帧，避免一个
  滚轮事件按倍数跳过多帧。
- 图例关闭的曲线不再绘制悬停圆点；提示表仍保留对应 actual/pred 数值，并用灰色
  表示该值对应的曲线已隐藏。
- 鼠标进入图表后显示横纵虚线，并在鼠标纵坐标处显示按相邻 Y 轴 tick 线性插值的读数；
  曲线上的圆点按指针 X 坐标连续插值绘制，不再吸附到原始采样点。
- 鼠标提示时间精确到毫秒；悬停提示卡每帧最多更新一次，避免高频 mousemove 触发 DOM
  重建。鼠标左键或右键切换整个悬停图层，关闭后只保留干净曲线与时间轴。
- 实时、预测和回放曲线统一为 1px、butt cap 与 bevel join，并吸附到设备像素网格，
  消除端点、折点和横轴滚动造成的视觉增粗或闪烁。
- 验证：浏览器中实测整帧延迟、中间刻度、毫秒提示、平滑插值圆点及左右键开关；
  `node --check` 通过；排除缺少可选仿真依赖的 `test_sim.py` 后测试为
  `134 passed, 2 skipped`。

## 2026-09-22（Library 搜索与实时图表时间语义）

- Library 的 `.lib-list` 不再拥有独立高度和滚动条，Videos、Datasets、Snapshots、
  Models 统一由左侧面板滚动；每个标签页新增本地搜索框，只匹配标题/名称与 note。
- 实时图表改用同一绝对时间轴保存 jog、relax、teleop、record、rollout 历史，模式切换
  不再清空曲线；replay 保持秒数刻度，live 保持本地 `HH:MM:SS` 刻度。
- 模式切换记录为灰色竖线，并在 Commanded action 的底部时间区域绘制竖排模式名；
  `current time` 白线仅在 rollout 显示，relax/teleop/record 与 jog 一样直接追加。
- 图例改为 `command` / `prediction`，保留 `prediction gap`，并新增 `mode change`；
  prediction gap 仍按时间区间绘制半透明红色背景。
- 图表悬停使用自定义提示卡，同一时间同时显示 actual 和 prediction；红色 gap 内没有
  预测点时仍显示最近的 actual。提示卡与底部刻度共用时间格式化，时间单位可切换为
  `wall`、`mode` 或 `start`。
- 左侧图例新增 `Time` 区，用两行紧凑选择器显示当前 `unit` 与监控 `scale`。
- `mode` 时间单位在模式切换线上标记 `0s`，后续刻度按该次切换重新计时。
- 监控时间新增 `2s / 10s / 30s / 1m / 10m` scale 选择并持久化；图例移除 `mode age`
  与 `total`。
- 实时图表按 60Hz `requestAnimationFrame` 每帧重绘画布；右侧新采样延迟一帧进入
  画布，曲线本身不再生成插值尾点。
- 原始历史最多保留 36k 点，显示前按窗口降采样到 1200 点以内，兼顾 10min 窗口与
  滚动流畅度；降采样使用固定绝对时间桶，并在窗口左端用真实相邻采样插值出边界点，
  避免左侧反复跳变。
- Tooltip 改为在光标时间前后两个真实采样之间线性插值；时间刻度文字使用 140ms
  交叉淡化；曲线改为零张力与 butt cap，修复两端视觉变粗。
- 验证：非仿真测试 `134 passed, 2 skipped`；完整测试 `160 passed, 2 skipped`，其余
  5 个失败/错误均来自缺少 `scservo_sdk`。Chrome smoke 14/14 + 7/7 + 5/5 通过，覆盖四个
  搜索框、单滚动容器、历史保留、模式标记、rollout now 线、红色 gap 像素、actual/
  prediction 提示、gap actual 回退、时间戳一致性、时间单位切换、scale 轴范围、
  长时间窗口降采样、左侧稳定边界、右侧一帧延迟、tooltip 插值与时间刻度淡化。

## 2026-09-22（Action 序列图可读性）

- Joint state 与 Commanded action 的 Y 轴统一固定为 ±180，曲线超出绘图区后由
  Chart.js 硬裁切；原始关节值和动作值不 clamp。
- 两张图共用底部最左侧一组图例，关节颜色按 Joints 面板顺序从 gripper 到
  shoulder_pan 排列；line style 使用简短的实线、虚线、预测断档背景和当前时间线文案。
- prediction gap 改为按时间区间绘制半透明红色背景带，不再叠加红色菱形数据点。
- 两张图标题固定展开，移除了 Joint state 与 Commanded action 的收起按钮。
- teleop、record、rollout 与 jog 使用线性时间轴并显示秒数，now 固定在窗口 88%
  位置；rollout 的未来窗口由最新 action chunk 的步数和长度估算，最大保留 8 秒。
- 时间轴改用快照 `ts` 的绝对时间戳，底部显示本地 `HH:MM:SS`；白色当前时间线不再
  绘制 `now` 文案，图例以 `current time` 表示。
- relax/jog 等非 rollout 行为不预留未来窗口；`loading → rollout` 沿用同一时间轴和
  实时缓存，历史点容量由 180 提升到 1800，避免 rollout 实线过早消失。
- 验证：非仿真测试 `133 passed, 2 skipped`；伪造 Chart.js 的浏览器测试覆盖
  ±110 范围、数据裁剪、固定 now 比例、4 秒 rollout 未来窗口、图例唯一性与窄屏布局。

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

## 2026-09-23：Arm Preview 调度修复

- 新增独立按需调度器：单一 rAF 所有者、主视角 60 Hz、姿态 30 Hz、腕部 10 Hz；独立脏标记与可见性，停止相机阻尼。
- 姿态使用时间插值并精确收敛；Auto 滑条保留窗口结束后主动恢复来源；尺寸不变时不重建画布。
- 模型等待子网格/纹理完成再挂载，关闭/切换后过期结果释放；释放场景共享几何/材质/纹理，腕部计算复用临时对象。
- 调度行为测试 7/7 通过，JS 语法检查和 diff 检查通过。浏览器和外观验证将在下一里程碑完成。

## 2026-09-23：Arm Preview 外观、交互与腕部相机

- 主/腕部 WebGL 开启原生抗锯齿；主视口 DPR 上限 1.5。场景改为页面背景色、中性半球光和单方向光，网格复用页面边框色。
- 内置 SO-101 在预览层使用共享冷灰/深灰 Lambert 材质及正面剔除；外部模型继续保留原材质、纹理和 sidedness。
- ISO/F/S/T/R 移到右下角 6px，缩为 20px 高、低不透明背景，保留 tooltip、键盘焦点和 `aria-label`。
- 位姿时间常数由约 101ms 缩短为 55ms，0.01° 内直接收敛；90° 测试姿态在约 0.5 秒内停止，减少无意义的尾部刷新。
- 腕部相机从 `gripper_frame_link` 原点沿“前端减安装点”的朝外方向观察，up 使用该坐标系的局部 +Y；SO-101 验证的视线/朝外点积与 up 点积均为 1.0，位置误差为 0。
- 浏览器回归覆盖：静置 5 秒停绘、相机不刷新腕部、姿态收敛、主/腕部独立可见性、电源重开、延迟模型失效、模型切换、DPR 1/2、280/480px 面板和 900px 窄屏。
- 可见 Chrome 同机测量：旧版相机交互产生超过 10k 次主/腕部绘制，新版主视角 59.8 FPS、P95 17.9ms、无重复提交，静态腕部零绘制；系统级 GPU 样本从 46–100% 降至 5–13%。GPU 样本包含其他桌面应用，仅作为同流程参考。
- 验证：调度测试 7/7；Arm Preview 相关 Python 测试 11 passed、1 skipped；完整测试 204 passed、2 skipped，另有 2 failures / 3 errors 均因当前 venv 缺少 `scservo_sdk`。
## 2026-09-24：公开仓库整理

- 将模型搜索与传输日志修正、数据集库管理和同步改动分成独立提交；模型库删除故障已在此前提交修复。
- 本地配置 `config.yaml` 改为忽略，提供不含本机路径与串口的 `config.example.yaml`；服务默认监听 `127.0.0.1`。
- 补充公开 README、Apache-2.0 与第三方资源许可说明、Windows 启动脚本和 CI；确认构建产物包含静态资源及许可文件。
- 验证：项目虚拟环境中 234 passed、9 skipped；Node 前端语法与模型/数据集删除行为脚本通过；离线构建和锁文件检查通过。
- 剩余限制：实体机械臂和策略推理仍需额外安装兼容的 LeRobot、硬件 SDK 与模型依赖；公开服务接口无内建认证，默认仅监听本机。

## 2026-09-24：Hugging Face 缓存迁移后的校准路径

- 监控配置支持 `huggingface_home`，启动时在导入 Hugging Face 依赖前设置 `HF_HOME`。
- 从臂与主臂支持独立的 `calibration_dir`，传给 LeRobot 配置，保留旧目录中的实体机械臂校准文件。
- 本机 `config.yaml` 将缓存指向 `D:/Cache/huggingface`，校准仍指向 `D:/datasets/lerobot/calibration`；已验证从臂六个电机的校准成功载入。重启监控服务后需用实机确认读取恢复。

## 2026-09-24：REC 流编码与遥操作背压修复

- Hardware 页可设置每路视频编码线程数，录制时实际传给 FFmpeg；流编码输出 H.264 MP4。Library 为旧 mp4v 文件生成浏览器可播放的缓存。
- 发现控制线程直接写 FFmpeg 管道，编码跟不上时写入阻塞遥操作。改由每路独立线程送帧，控制线程仅提交最多两项待处理画面；积压时合并旧画面，保持视频总帧数。Windows 编码进程降低调度优先级。
- 验证：模拟编码器写入卡住，控制侧仍可连续提交 10 帧且缓存不超过两项；真实 FFmpeg 双路相机及合成画面可解码；相关测试 143 passed、3 skipped。实体机械臂需复测高分辨率与所选线程数下的控制流畅度。

## 2026-09-24：满分辨率录制与延后编码

- 录制工作线程接管相机取帧、视频写盘和 episode 收尾；控制线程只提交动作及视频采样时间戳。视频请求在积压时合并，Windows 控制线程优先级提高，录制线程优先级降低。
- 新增可选“Save JPEGs, encode after recording”：录制时保存相机现有 JPEG，不在录制期间编码；停止后后台生成 H.264 MP4 和合成视频，Videos 列表显示进度与错误。JPEG 暂存保留以便转码失败时恢复。
- 用户反馈实测效果良好。提交前回归：相关测试 141 passed、3 skipped；新模块静态检查、前端语法检查及 diff 检查通过。

## 2026-09-24：Record 操作面板与 Dataset 直接入库

- Record 改用 Library 中 Dataset 的稳定 ID，服务端解析本地目录；LeRobot v3/FPS/关节顺序/相机名称与尺寸在启动前校验。Hub 缓存先复制到普通可写目录。独立 Videos 采集继续保留原双 FPS 设置。
- 主相机顶部新增 Record 操作面板：手动或倒计时 Reset、暂停/继续、撤回重录、提前结束/开始下一条、停止；快捷键 Space/左右/Esc，WebSocket 刷新后恢复状态。会话控制携带版本和操作 ID，避免重复请求跳过阶段。
- 控制线程只提交配对动作、状态及相机 JPEG 引用；有界队列在后台暂存。相机中断、写盘错误或积压会暂停当前条并要求重录。暂停保持从臂，恢复按 Joints 当前速度限速；Instant 用关节 30°/s、夹爪 30%/s。
- 已确认的 episode 在后台串行追加为原生 LeRobot Parquet/H.264。元数据最后发布，异常回滚事务记录并保留暂存供重试；Stop 不等待编码。移除启动及收尾时对旧 Videos 的自动裁剪。
- 验证：空 Dataset 录两条、已有 Dataset 续录、撤回、保存失败重试、动作/视频颜色/时间戳与旧文件哈希；虚拟从臂经 API 录制并由 Library 读取。全套回归 259 passed、1 skipped；收尾改动另跑针对性验证。
- 未验证：实体机械臂暂停保持与高分辨率负载；浏览器桌面/窄屏视觉交互。进程重启后会回滚中断的发布事务，但离线保留的失败暂存尚无跨重启的自动重试入口。
## 2026-09-25：统一控制频率、Playback 与科研诊断

- 机械臂输出频率由全局默认和 Joints、Teleop、Record、Playback、Rollout 模式覆盖配置管理，支持自定义 Hz；有稳定动作源的 Playback/Rollout 额外支持 1×/2×/4×。Record Dataset FPS 仍独立，旧 rollout `fps` 不再静默截断策略频率。任务可显式覆盖模式设置，运行中变频从下一发送截止时间生效。
- 控制线程用单调时钟分别安排机械臂发送、状态读取、策略动作和录制采样。真实成功发送时间用于实际 Hz、P95、最大间隔、漏过周期与保持计数。Replay/Joints Auto 共用后端 Playback 会话：完整 episode 轨迹按原始时间戳读取，以线性或保持插值在任意输出 Hz 执行；浏览器固定 20 Hz 发命令路径已移除。
- Rollout 的同步推理采用有界工作线程，控制线程消费最新动作并在策略周期内连续过渡；RTC 仍使用原有推理后端。策略缺货时保持最后目标并标记等待，过期工作结果不能恢复已停止任务。
- 每次控制任务异步保存运行摘要、分段与事件，可选逐次 CSV。Record 按 Dataset FPS 固定时间槽采样；首个完整样本设定时间原点，真正漏采的时间槽在后台用前一完整样本补齐。`meta/quality/episode_*.json` 保存来源、真实采样与状态时间、对应相机帧标识及接收时间、补位原因；接收时间不作为曝光时间，标准 LeRobot 特征不变。Library 和回放显示质量标记。
- 验证：可控时间测试覆盖 15 Hz 轨迹在 15/30/50/60 Hz 下的时长与端点、1001 帧完整读取、运行中变频、版本化播放控制、150 帧固定 FPS 补位及异步日志导出。虚拟机械臂 API 与原生 LeRobot 数据集写入/回读通过；全套回归 273 passed、1 skipped，前端语法检查和桌面／900px／420px 浏览器外观检查通过。实体机械臂的实际发送间隔及关节跟踪误差尚未测量。

## 2026-09-25：Joints Auto 输出启动顺序修复

- 反馈：Joints Auto 的滑条会随来源更新，但机械臂不跟随。定位到先开启来源 Auto、再开启连续输出时，前端只发送一次当前目标，没有启动后端 Playback。
- 现已使任意开启顺序都进入同一 Playback 任务；播放期间可从 Joints 关闭连续输出。启动／停止请求串行，关闭或切换来源时也能取消排队中的启动；忽略启动前的过期状态快照，避免新会话被旧快照清除。
- 回归：新增前端行为脚本覆盖 Auto→连续输出→关闭及过期状态；相关接口测试 4 passed。未向当前连接的实体机械臂发送测试轨迹。

## 2026-09-25：慢放时的来源频率展示

- Playback 的原始动作源 Hz 继续作为 1×/2×/4× 输出设置的基准；播放速度只缩放轨迹时间轴，不自动修改机械臂输出 Hz。
- Playback 状态与界面分别显示原始 Source Hz、按播放速度折算的有效 Source Hz、机械臂输出 Hz。验证 15 Hz 来源以 0.5× 播放时有效来源为 7.5 Hz，4× 输出仍为 60 Hz；变速至 2× 后有效来源为 30 Hz，输出仍为 60 Hz。

## 2026-09-25：Joints Auto 播放状态竞态与前端缓存

- 用户再次反馈：视频播放而机械臂停在首帧。运行记录显示 Playback 在 120 Hz 下成功发送约 3 秒，但 393 次全部是保持命令、源动作只更新一次，说明问题在后端未收到播放状态，而非发送频率不足。
- 修正 Playback 完整轨迹读取期间用户点击播放／暂停、变速或跳转的竞态：把最后一次用户意图保存到会话启动完成后，按变速、跳转、播放状态的顺序交给后端。启动失败时清除待处理意图。自定义速度限制为后端支持的 0（暂停）或 0.1–8×，避免视频可播放而后端拒绝速度。
- 页面 `app.js` 的固定版本号此前仍停在 2026-09-24，现更新版本号，确保刷新后浏览器加载修复脚本。新增测试覆盖“启动请求未返回时点击播放”，并验证当前服务提供新版本脚本。实体机械臂未用于自动化动作测试。

## 2026-09-25：机械臂输出频率面板的可见性与布局重做

- 反馈：宽屏下设置机械臂控制频率的板块完全不可见，右侧面板里的入口也不好看，只有窄屏能看到。根因是编辑器此前放在顶栏 `.rate-settings` 内的绝对定位 `<details>` 弹层里，而 `.topbar` 只在 `max-width: 1400px` 才放开 `overflow: hidden`，因此 1400px 以上弹层被整体裁掉；`#fps` 固定的 `width: 8ch` 也会让文本溢出并与时钟重叠。
- 改为 body 级的固定定位浮层 `#rate-panel`：位置由 JS 按打开它的触发元素计算（下方优先，空间不足时翻到上方，左右与上下都夹在视口内），因此不再受顶栏裁剪影响，也不受任何祖先的 `overflow` 约束；触发器保留顶栏按钮与 Joints／Teleop／Record／Rollout／Replay 各处入口，`aria-expanded`、`aria-controls` 与 Escape／点击外部／切换侧栏页签关闭、焦点回到触发器等行为一并补齐。
- 顺带修掉三处布局问题：`#joints-rate` 原本是 `.joint-speed-control` 三列网格的第 4 个子项，会掉进 72px 的窄格；窄屏打开编辑器会 `scrollIntoView` 把整页顶走；Apply 失败只在日志里报错。现在各处入口统一为「标签 + 值 + 来源」的 chip，面板内联显示错误，窄屏单列统计。
- 顶栏单行布局在 1440–1700px 装不下全部状态与按钮组，按钮组会盖住摘要区并把频率触发器变成不可点击，因此把换行断点从 1400px 提到 1700px，并给 `.header-actions` 加横向滚动兜底。Apply 成功后立即用返回值刷新顶栏与各 chip，不再等下一次状态推送。
- 用户随后反馈 Rollout 面板里「Interpolate between policy actions」的复选框跑到了右侧、说明文字掉到下一行。原因是全局 `label { display: flex; flex-direction: column }` 会把标签子项纵向排列，而这一行没有像 Record 面板那样使用行内布局类；补上 `class="check"` 后复选框与文字回到同一行左侧对齐，与 `chk-rec-auto-next` 等既有行一致。
- 用户要求倍率再增加 6× 与 8×：`RATE_PRESETS` 扩展为 `(1, 2, 4, 6, 8)`，`#rate-kind` 增加两档；Rollout 的倍率说明文字按用户要求移除，面板只保留档位本身。倍率仍只能在 Playback/Rollout 使用，折算结果超过 240 Hz 时（例如 60 FPS 策略 × 8）后端拒绝并回报「… this setting resolves to 480 Hz」，面板内联显示该原因。验证：新增倍率档位参数化测试、非法档位与溢出信息测试、Playback 会话中 6×→90 Hz／8×→120 Hz 的实时变频测试；`scripts/verify-rate-panel.cjs` 增加每个视口 9 项档位检查（五档齐备、Rollout 全可用、Joints 全置灰、8× 落到 chip、恢复继承），六视口共 324 项通过；全套 pytest 保持通过。
- 验证：全套 pytest 276 passed；新增静态契约测试覆盖面板 id／层级／CSS 规则与 JS 入口；新增 `scripts/verify-rate-panel.cjs` 在 2560×1440、1920×1080、1440×900、1024×900、768×500、390×844 六种视口下共 270 项检查通过（面板完整落在视口内、中心与四角 `elementFromPoint` 命中面板自身、无横向溢出、顶栏标签不压时钟、Apply／继承恢复／非法值不请求、四种关闭路径与焦点回归、窄屏滚动跟随）。冒烟使用临时配置与临时 store，未改动 `data/monitor_store.json`。
- 未验证：实体机械臂在面板里改频后的实际发送间隔；触屏设备上的粗指针样式只做了静态声明，未在真机触摸操作下验证。

## 2026-09-26：RTC 停止协议、热权重复用与 rollout 失败退出

- 反馈：真实 rollout 日志反复出现 `Stopping RTC inference thread...` 与 `RTC thread did not join within 3.0s`；rollout 失败后顶栏 rollout 灯仍亮；模型加载仍要求重复填写命令行参数。三条指向同一批状态机缺陷。
- 根因：`RTCInferenceEngine.stop()` 只做一次 3 秒 join 就返回 `False`，而 RTC 线程只能在 `predict_action_chunk` 返回后检查关闭事件，停止延迟天然 ≥ 一次在飞推理；监视器又用 `while engine.stop() is False: time.sleep(0.05)` 重试，于是每约 3.05 秒重复两条日志。更严重的是推理租约因此长期停在 `stopping`，`acquire()` 无超时轮询，下一次 rollout 启动会永久挂在 `pending="rollout_start"`。
- 停止协议改为「一次信号 + 可等待」：`stop()` 只发一次信号并做一次有界 join，返回 `False` 表示仍在收尾；新增 `wait_stopped()` 让调用方阻塞到线程真正退出；`start()` 拒绝在线程未退出时重启；RTC 循环的空闲与退避睡眠改为 `_shutdown_event.wait()`，文本查询返回后立刻检查关闭。监控端只调用一次 `stop()`，由后台线程等到线程死亡后再释放租约，租约释放即代表同一份 policy 不会有两个引擎。
- 模型实例身份改为只由权重决定：`policy_identity` 不再把命令行覆盖并进键，`policy.*` 覆盖在 rollout 启动时实时应用到常驻实例（`apply_requested_overrides`），设备拼写（`cuda` / `cuda:0` / 大小写）归一。改 `policy.n_action_steps` 之类的运行参数不再加载第二份同权重；`acquire()` 的超时只约束 `stopping` 等待，冷加载仍是合法长等待。
- 失败即退出 rollout：`_drain_commands` 在命令失败或 e-stop 拒绝时清掉该命令自己创建的 pending；五处退出路径先把 mode/pending 落定再做可能失败的录制收尾；控制循环加最后一道守卫，单次异常不再杀死控制线程，重复故障按签名只记一次日志。
- 验证：lerobot 侧 `tests/test_rollout.py` 与 `tests/test_interactive_rollout.py` 115 passed；lerobot-monitor 全套 324 passed、1 skipped。无硬件端到端冒烟（虚拟从臂 + 已缓存 SmolVLA 权重，走 HTTP API）：空 `policy_path` 的 `rollout_start` 返回 4xx 后 `pending` 为空、灯灭；同一权重冷加载一次（日志 `model ready in 98.7s`），第二次带不同 `policy.n_action_steps` 启动显示 `using cached policy` 且没有第二次加载；两次停止各只有一条 `Stopping RTC inference thread...` 与一条 `RTC inference thread stopped`，不再出现 `did not join`；引擎因缺相机图像失败时 rollout 在约 4.5 秒内自动退出（`mode=idle`、灯灭、`message` 带 traceback）。
- 未验证：没有人工制造超过 3 秒的单次策略推理，因此「`stop()` 返回 `False` → `wait_stopped()` 阻塞数秒」这条路径只由单元测试覆盖（真实 RTC 线程 + 真实引擎类）；实体机械臂上的 rollout 行为仍需真机复核。

## 2026-09-28：修复首次策略依赖导入与状态刷新 I/O

- 在线服务同一次 SmolVLA 加载耗时 380.908 秒，其中 configs 27.315 秒、factory 244.410 秒、weights 107.800 秒。加载线程采样依次位于 importlib 文件读取、pandas 模块导入、Transformers 的 `importlib.metadata.packages_distributions()` 包文件扫描，未看到同一导入锁上停滞。同期控制与状态推送线程多次在模型目录 `is_dir/resolve/stat` 中。
- 原生 configs 会引入图像变换、torchvision 等；factory 会触发策略与处理器注册、Transformers 和 pandas 等依赖。保留原生注册，未跳过包初始化、修改第三方导入器或关闭安全软件。当前证据不能把数分钟延迟完全归因于某一个 OS/GIL 因素。
- Monitor 现在通过统一的 `import_policy_dependencies` 准备共享依赖，默认在摄像头和控制线程启动前完成；Load 复用 Python 模块缓存。支持 `rollout.preload_dependencies: false`，缺失 PyTorch/LeRobot 时跳过，其他导入异常记录失败阶段后继续启动监控。启动日志和 `runtime.policy_dependencies` 保留阶段耗时。
- `all_statuses` 直接读取已归一的内存键，控制循环用布尔查询判断 stopping，不再为每次状态广播重复访问模型目录。单次外部 path 查询仍做路径归一，保留别名语义。
- 与同期取消加载功能合并后，远程模型的本地快照别名在加载阶段切换与显式路径查询时更新；状态广播读取缓存别名，避免 `source_paths` 又引入路径解析。取消与释放 API 保留原行为。
- 合并后全量测试 423 passed / 4 skipped（174.75 秒）。随后补齐同一远程模型多设备实例在快照重命名期间的内存聚合，并重跑全部 residency、加载日志、启动与 runtime 测试：44 passed（23.10 秒）。此最后调整未再次运行全量测试。
- 验证：完整环境 pytest 407 passed / 4 skipped；基础环境启动与失败降级测试 10 passed。首次定向测试断言全部通过，但默认临时目录清理遇到 Windows 权限错误；改用本次任务独立临时目录后完整测试正常退出。
- 新进程、关闭摄像头、临时 store 且禁止网络：未预载时启动加 config/factory 导入共 14.998 秒（3.941 + 8.748 秒）；预载后共 14.544 秒，预载 12.047 秒，加载线程重复导入 config/factory 为 3.5 / 2.1 微秒。此对比只验证预载没有把数分钟原封不动移到启动期，不等同于真实摄像头负载下的完整 A/B。
- 真实缓存 SmolVLA 在独立 CPU 进程加载成功，禁止所有 socket connect；预载 12.267 秒，后续加载约 49 秒，进程到就绪共 63.009 秒。为避免干扰在线 GPU rollout，该冒烟用 CPU、PyTorch 2 个计算线程，不作 CUDA 性能结论。
- 在线服务正在 rollout，本次未重启；修改将在下次服务启动生效。真实摄像头/控制负载下的重启后 CUDA 全链路时间仍待验证。

## 2026-09-28：独立云端管理（阶段记录）

- 用户要求先完成云端服务及本地独立云端管理页面；本地 Monitor 接入需要用户手动确认。本轮工作放在独立 worktree，未触及原仓库及相邻 LeRobot 的源码。
- 新增 standalone cloud manager：回环 Host/Origin 保护、主机配置、SSH 探测、隔离 wheel 初始化、隧道、后台任务、令牌留在后端的代理、带 SHA256 清单的 checkpoint 上传及独立 CUDA runtime profile 安装。
- 两个主机保留配置；依照用户最新约束，仅允许 8x4090-server 远端操作，8A6000-server 不连接、不初始化、不测试。
- 管理服务和模型运行时分别放于专用数据根目录，使用当前 package wheel、固定 CUDA/Transformers 版本、runtime 哈希锁文件。升级先安装，再请求旧 daemon 停机；活动会话拒绝升级，新版启动失败尝试回滚。
- 当前验证：manager 24 项测试通过、远端 Python 脚本语法检查通过、临时目录构建 wheel 成功。测试覆盖真实双 FastAPI 应用之间的 session 路由契约；此结果不代表已经完成真实 GPU 推理。
- 下一步：4090 初始化与真实模型推理验证、云端与 UI 汇总审查，记录最终证据；本地 Monitor 接入仍等待用户手动确认。
- 审查修复：主机连接状态与管理作业忙碌状态分离，后台任务期间仍保留云端模型面板；runtime.json 改为保留多个 profile 并迁移旧配置，避免后装 PI 覆盖 SmolVLA 环境。manager 验证增至 30 项通过。
- 上传回收审查修复：提取/校验/登记前失败以及明确 4xx 拒绝只删除本次生成且经过父路径校验的 UUID staging；登记超时或 5xx 保留文件并报告恢复路径，避免误删已被服务登记的模型。
- 新增可重复运行的 scripts/smoke-cloud-models.py：显式部署 ID 与 GPU，metadata 驱动零状态和黑色 PNG，原生 select/debug/RTC 模式及租约/epoch/重复请求验证，finally 关闭会话并卸载，输出 JSON。尚未由此子任务执行远程冒烟。
- manager 与 smoke-harness 本地验证累计 39 项通过；ROADMAP 末尾空行已修复。
- 4090 实测 ACT 首次加载暴露运行环境缺失 datasets；已将所有 profile 加上 LeRobot dataset extra，并把运行环境 ready 检查扩大到原生 policy factory、processor、rollout/RTC 和 profile 对应模型类导入。需求组合测试覆盖 ACT/SmolVLA/PI，manager+harness 累计 42 项通过；等待主线程重新安装与实测。
- 文档修正：外部 runtime 使用自身依赖，仅以 importlib 固定 Monitor worker 包到活动 release；推理图像传输明确只支持 PNG。

### 2026-09-28 最终验收与交付状态

- 独立工作树 `C:\Users\Admin\.codex\worktrees\cloud-model-manager\lerobot-monitor`、分支 `codex/cloud-model-manager` 完成本阶段；未改动原 Monitor 应用或相邻 LeRobot 源码。接入原 Monitor、实体机械臂验证仍等待用户手动确认。
- 4090 管理服务 bootstrap 成功，随后两次升级成功（各约 14 秒）；专用目录 `/data/zhuyutian/lerobot-monitor`。本地独立管理页面 `http://127.0.0.1:8095` 正在运行，使用原轻量 Python 可执行文件加 worktree/src 的 PYTHONPATH；可复用启动与复测命令已写入 `docs/cloud_models.md`。
- 模型运行时采用 LeRobot `79f1e10d` 的 0.6.2 wheel，SHA256 `5d8af18eaf294a690b4325f6f335c4a92cdbd375a9a313f1dbfcba63bb60fdcd`。首次 ACT 暴露的 datasets 依赖缺失已修复，新的含 dataset profile 成功安装；早期轻量环境中的回归依赖失败也已由完整依赖环境重新验证解决。
- 真实 ACT 验证 `.tmp_cloud_results/act.json` 为 passed：select_action `[1,6]`、debug_chunk `[8,6]`。真实 SmolVLA 验证 `.tmp_cloud_results/smolvla.json` 为 passed：select_action `[1,6]`、debug_chunk `[8,6]`、RTC `[50,6]`，并验证 guided 前缀推理。覆盖独占会话、使用中拒绝卸载、心跳、重复请求、reset 与过期 epoch；两个模型最终均卸载。
- 本地上传样例实测完成 manifest/SHA256 校验、托管登记、delete_files 删除，远端 staging 和 asset 均确认不存在。既有 ACT、SmolVLA 外部缓存权重保留。
- 最终测试：远端 Linux cloud + manager + smoke-harness **79 passed**；既有本地完整回归在完整重依赖环境与固定 LeRobot archive 下 **357 passed、4 skipped**；UI **12 项测试及 4 个视口检查通过**。
- 验收边界：输入为零状态与黑色 PNG，未连接实体机器人，不表示策略任务准确率或实机性能。PI profile 已实现，但无缓存 PI checkpoint，因此未完成 PI 实际推理验收。A6000 本轮未连接、未安装、未测试。本地 Monitor 集成的手动确认门禁继续保留。

## 2026-09-28：Monitor 远端推理与 GPU 占用归属

- 用户已明确解除 Monitor 接入门禁。模型库新增 `cloud://` 远端条目，Models 页面可连接 SSH 主机、选择部署和 GPU；云模型沿用既有 Load／Unload、Debug 和 Rollout 入口，令牌只保存在后端。
- 云端 GPU 探针合并 `nvidia-smi` 计算进程、`/proc` 用户和程序信息。8x4090-server 实测能标注 Blender／Python 的主要占用用户和显存；页面禁用忙卡，并在单卡查询故障时保留其余七张健康卡。
- ControlLoop 新增远端 Debug、同步和 RTC 分流。RTC 保留 raw／absolute 前缀、延迟补偿、后台网络请求、心跳、epoch reset 和显式 close；停止或异常会回收远端独占 session。远端配置解析不要求本机安装 LeRobot。
- 8x4090-server 实测：ACT Debug 返回 4×6、Sync 返回 1×6；SmolVLA RTC 生成 50 个六关节动作。完整 Monitor API、ControlLoop、虚拟 follower 跨层测试通过；浏览器完成 ACT 云模型登记。输入为零状态和黑色图像，未连接实体机械臂。
- 当前 LeRobot runtime wheel 来自 `e5315df2`，SHA256 `ae5a3dbb2f67298ee1953319b7805603047c4ee680817245bd23cef413b29803`，ACT／SmolVLA 独立 runtime 安装成功。8A6000-server 未连接、未安装、未测试。
- 最终受影响回归 **231 passed、2 skipped、1 deselected**；云端与兼容性定向 **38 passed**，Python／JavaScript 语法和差异检查通过。未运行的录制用例需要本地轻量环境未安装的 `huggingface_hub`，与远端推理路径无关。

### 云模型常驻登记与按次选择 GPU

- 根据用户反馈，将 GPU 从云模型持久身份移到 Load 请求。新增模型只保存主机和部署；Unload 后条目保留，下次 Load 可换卡。
- Models 添加窗口不再要求 GPU。模型卡 Load 会刷新 8x4090-server 的实时 GPU 列表，显示主要占用用户／程序／显存，禁用忙卡，并提交所选 GPU UUID。
- 旧 `cloud://...?...gpu=` 地址仍可解析；模型注册表启动时保留原模型 ID，自动清除旧 GPU 字段并写成稳定地址。
- 浏览器验收确认添加窗口仅选择主机／部署，模型卡 Load 才显示实时 GPU；GPU 5 空闲，其余忙卡显示占用者并禁用。ACT 在 GPU 5 的真实 Load 成功，随后 Unload 成功，模型条目与稳定地址继续保留；未执行推理或机械臂动作。
- 最终受影响回归 **233 passed、2 skipped、1 deselected**；定向云模型身份、迁移与 Load 选择测试 **12 passed**，Python／JavaScript 语法和差异检查通过。

## 2026-09-29：Cloud manager 合并进 Monitor 主页

- 用户要求把独立 `lerobot-cloud-manager`（端口 8095）的功能全部并入 Monitor 主页作为 Cloud 面板。新增 `cloud_api.py`，把主机注册／探测／连接／断开／初始化／升级／上传／运行环境／任务和云端 API 代理统一挂到 Monitor 的 `/api/cloud/*`，后端仍复用 `CloudManager`、`SSHTransport` 与 `MonitorCloudClient`，浏览器不接触令牌。
- 前端新增 `cloud-panel.js`（ES 模块）与 index.html 的 `cloud` 侧栏页签，复刻服务器、GPU、Cloud models、Recent jobs 四个区块和新增／加载／日志／移除对话框；轮询只在面板可见且标签页活动时进行。`app.js` 在 Cloud 页签隐藏 preset 工具栏；六个 tab 的 `min-width` 调整到 54px，保证默认侧栏宽度下 Cloud 页签完整可见。
- 模型库接入保持原有 `cloud://<host>/<deployment>` 流程，面板的 Use 按钮复用 `POST /api/models/cloud`，把部署登记到普通模型库供 Rollout／Debug 选择。
- 合并时发现既有健壮性问题：`CloudManager` 构造会直接读取 `~/.lerobot-cloud-manager/hosts.json` 与 `credentials.json`，本机 `credentials.json` 被 ACL 锁定导致 Monitor 完全无法启动。改为「不可读或损坏即降级为空集合」，主机行逐条跳过损坏条目；损坏的 hosts.json 不再回退到默认主机。
- `MonitorCloudClient.request` 新增 `params` 透传并携带上游状态码，使代理能把远端 409（重名部署）与网关 502 区分开；代理继续复用独立管理器的 `safe_proxy_path` 白名单，请求体上限 64 MiB。Monitor 面向可信网络且可绑定 `0.0.0.0`，因此未照搬独立管理器的回环 Host／同源校验，并在文档中写明信任边界。
- 测试：新增 `tests/test_cloud_api.py`（hosts／probe／bootstrap／upload 校验／runtime／代理参数与状态／不可用 manager），`test_cloud_manager.py` 增加损坏状态降级用例，`test_monitor_cloud.py` 增加请求参数与状态用例；新增 `scripts/test-cloud-panel.mjs`（纯函数边界 + jsdom DOM 渲染/加载/上传/日志/错误对话框）与 `scripts/verify-cloud-panel.cjs`（启动真实 Monitor + Chromium 四视口）。
- 验证结果：cloud 相关后端 94 passed、1 skipped；Cloud 面板 JS 7 passed；原独立 cloud UI 12 passed；Chromium 390／768／1440／1920 全部通过，无横向溢出、无 console 错误，Add 对话框在轮询期间保持输入，Load 走同源代理 `POST .../pi/load`。本机 .venv 缺少 `torch`／`huggingface_hub`，`test_native_rollout_profiling.py` 及 8 个依赖这两者的既有用例无法运行，属环境缺口；`test_status_without_hardware` 的 `Deploy` HTML 断言随面板文案（+ Add）调整后通过。
- 未验证：未连接真实 8x4090-server，未执行真实 SSH bootstrap／upload／runtime／推理；独立服务仍在，真实远端链路沿用此前验收记录。

### Library「Add model」改为本地／云端双页签

- 用户反馈：添加模型弹窗把来源做成下拉框，云端和本地字段同时出现，云端还要求填本地地址。
- 根因：`openDownloadModal` 只对 `.library-modal-grid`／`.library-modal-search` 切换 `hidden` 类，而样式表里没有这个类的通用规则，所以两个来源的表单一直同时显示，「Source」下拉只改了按钮文案。
- 改为顶部两枚 `role=tab` 分段控件 Local／Cloud：本地页签保留搜索、Address、Revision；云端页签只保留 SSH host、Deployment、Connect and refresh。Name 提到页签下方，两个来源共用。切换页签会隐藏整块 pane 并清空旧的状态/错误提示，支持左右方向键切换与 roving tabindex。
- 补充 `.library-modal-pane.hidden` 与 `.library-modal-tabs` 样式（沿用产品既有的 10px 大写、`--accent` 下划线、`--line` 边框语言），并给 `.library-modal-body > label input/select` 补上输入框样式——该规则同时修好了此前同样未套用样式的云端 Load 弹窗 GPU 选择框。
- 校验：新增 `scripts/verify-library-model-tabs.cjs`，在真实 Monitor ＋ Chromium 中断言本地校验不再触发云端登记、切到 Cloud 后 Address 完全不可见、Connect 后出现 2 个部署、提交只发出一次 `POST /api/models/cloud`（`host_id`／`deployment_id` 正确），并在 390／768／1440 三个宽度确认无横向溢出、无 console 错误。

### 云模型侧栏加载状态与云端 rollout 动作预测线

- 现象一：Models 侧栏的云模型加载时没有进度条，完成后也不显示 Ready。根因：`MonitorCloudClient.residency()` 只读 `_deployments` 缓存，而缓存只在 catalog／`deployment()` 请求时刷新；`submit_load` 之后没有任何刷新，状态一直是 unloaded，也没有 loading 状态。
- 修复：`submit_load` 立即把缓存写成 loading；`residency()` 触发限流（loading/unloading 时 1s，否则 3s）的后台线程刷新已连接主机的部署列表（不连接的主机不会因状态推送而被建立 SSH 隧道），并用 `_local_changes` 代数丢弃提交前发出的过期应答。新增 loading／unloading／error 状态及带 `remote`、`elapsed_ms` 的实例；侧栏对云实例显示「Loading on cloud GPU · Ns elapsed」，并隐藏对云端无意义的 Cancel load 按钮。云端不提供阶段信息，进度条保持不确定态。
- 现象二：云端 SmolVLA rollout 时动作曲线没有本机推理时的虚线预测。根因：云 sync 的 `PolicyWorker.preview` 恒为空列表，云 RTC 走本机 tensor 映射读不到远端队列。
- 修复：云 worker 的 `select_action` 响应新增 `queued_actions`（策略队列或 ACT temporal ensembler 中剩余动作经 postprocessor 处理后的绝对值，失败则为空，不影响推理）；`RemoteSyncInferenceEngine`／`RemoteRTCInferenceEngine` 新增 `leftover_poses`，`inference_leftover_poses` 优先使用引擎自带钩子。旧版云服务不返回该字段时预测线为空，行为与之前一致。
- **需要重新升级 4090 上的云服务**才能让 worker 回传 `queued_actions`（worker 代码哈希已变）。
- 测试：新增 worker 队列预览／失败降级、sync／RTC 引擎预览、residency loading→ready、失败加载、未连接主机不刷新共 9 项；相关回归通过。已知与本次无关的失败：`ModelRegistry.list()` 会扫到本机真实 HF 缓存模型导致两个云模型注册用例断言 `len == 1`／`== []` 不成立，以及缺少 `huggingface_hub` 的用例。未在真实 4090 上验证。
