# LeRobot Monitor

独立于 `lerobot-record` / `lerobot-teleoperate` / `lerobot-rollout` 的常驻监控与控制面。
进程自己持有 follower 总线和相机，因此机械臂训练/采集程序未启动时仍能返回关节、画面与任务状态。

参考 `huggingface/lerobot` 的 web dashboard（`web_visualization.py`）与本仓库 `tools/move_joints.py` 的关节控制约定。

## 架构

单一控制线程独占 Feetech 总线（总线非线程安全）。相机采集与 HTTP 服务并行，互不阻塞。Web 请求只向控制线程投递命令。

```
Browser  --HTTP/WS/MJPEG-->  FastAPI
                                  |
                                  v
                            RuntimeHub
                     /        |         \
              CameraHub   ControlLoop   SessionWriter
                              |
                     Follower bus (+ Leader when teleop)
```

模式：`idle`（读状态、可选保持位姿）→ `jogging` / `teleop` / `record` / `rollout`（互斥任务）。

## 里程碑

1. 常驻 HTTP 服务 + 相机 MJPEG + 关节 WebSocket，硬件离线时 UI 仍可用
2. 单关节/预设位姿控制，E-stop
3. 遥操作测试（leader → follower）
4. 录制 episode：视频 + 关节 CSV
5. 部署策略（rollout）并可把结果写成同一套 session 格式
6. Library 回放：预览切到主屏并以 REPLAY 呈现，顶部时间条可拖动，Stop 退出
7. 本机 policy 扫描：HF hub cache、HF_LEROBOT_HOME、outputs/checkpoints
8. Episode 管理：独立 Episode 列、命名/任务/备注编辑、逐条播放，回放时下方 Joint state / Control state 时间条

## 已完成（2026-09-22）：Hardware preset、固定工具栏与设备身份恢复

目标：让右侧五个任务页共享一致的 preset 交互，并把 Hardware preset 作为可跨进程
恢复的完整硬件配置，而不是依赖相机名称或当前 COM 号的弱引用。

实现边界：

- 左侧 Library 在标签栏下使用固定共享搜索框；四个标签各自持久化关键词，Models 本地
  过滤与 Hub 搜索保持独立。
- 右侧五页共用固定 preset 工具栏：下拉选择加 Load / Save / Rename / Duplicate /
  Delete 图标按钮；新建和重命名使用内联名称弹层。切换下拉项只选择，点击 Load 才应用。
- 每个右侧标签独立保存 preset 选择和滚动位置；浏览器刷新后恢复，不自动应用普通表单
  preset。
- 新增只读系统 preset `Disconnected`，用于显式断开 arm/leader 并停用全部相机；
  首次运行默认选中但不执行，禁止覆盖、重命名和删除。
- Hardware preset 以 `identity` 记录设备：真机串口优先匹配 `hwid`，同 hwid 时用 port
  区分；虚拟设备匹配 role + robot_id；远程相机匹配 robot_id + camera_id；本地相机
  匹配 index/name。label 只作为设置保存，不参与匹配。
- `POST /api/hardware/apply` 在控制线程中串行执行硬件变更，返回逐项 success / skipped /
  failed；每个设备与相机的动作和原因都写入 UI 日志及 `run.log`。
- 活动 Hardware preset 写入后端 `ui.active_hardware_preset`；Monitor 启动后自动排队
  恢复，首次没有活动 preset 时不产生硬件副作用。
- arm/leader 端口下拉提供空选项 `No device`；设备选择与连接状态分离，右侧电源图标
  单独显示并切换实际连接，旧按钮不再占用额外行。
- Hardware 页面提供 `Force disconnect`，直接释放 arm/leader，不执行 relax 软断开。
  Hardware preset 载入期间 Load 图标变黄；再次点击会发送 force 信号，跳过当前
  relax 等待并继续载入。
- Arm 与 Leader 各自使用一行 `标题 + 设备选择 + 连接电源图标 + force disconnect
  图标`，连接信息固定显示在该行下方；force 接口支持按 role 单独释放。
- 控制命令不再隐式连接设备：jog/relax、resume、读取 follower/leader、teleop、record
  和 rollout 在对应设备未连接时直接拒绝并记录错误；只有连接图标和 Hardware preset
  可以建立连接。
- 顶栏任务按钮不再切换成 Stop 文案，只在任务激活时点亮。Stop 对 teleop/record/
  rollout 使用两阶段停止：第一次请求软停止并变黄，第二次发送 force stop，立即脱离
  推理引擎。Follower/leader 的顶栏与 Hardware 电源按钮共用软断开/强制断开逻辑。
- 顶栏 F/L 标签移到按钮外，设备名按钮宽度随文字收缩并保持与 HOLD pill 同高；
  Hardware 保存的空端口不再被默认 COM 口回退覆盖，刷新后保持 `No device`。
- 左侧品牌区固定占用 260px（窄屏 220px）布局轨道，模式 pill 文字变化不会推动
  右侧设备按钮。

验证：排除缺少 `scservo_sdk` 的仿真测试后为 `155 passed, 2 skipped`；`node --check`、
Python compile 与浏览器 smoke 通过。浏览器覆盖固定搜索/工具栏、per-tab 搜索与 preset
状态、内联重命名、显式 Load 日志、刷新恢复、服务重启自动恢复，以及 1024/390px
无横向溢出。真实串口与相机的换端口连接仍需在硬件现场做最终 soak 验证。

## 已完成（2026-09-22）：Action 序列图可读性

目标：让底部 `Commanded action` 图在遥操作、录制与 rollout 时保持稳定的时间语义，并在不改变关节控制值的前提下限制视觉范围。

实现边界：

- Joint state 与 Commanded action 的 Y 轴统一固定为 `[-180, 180]`，超出曲线在绘图区边缘硬裁切；原始关节值和策略输出不做 clamp。
- 图例从 Chart.js 内置图例移出，固定在两张图最左侧窄列并共用一组；关节颜色按 Joints 面板顺序从 gripper 到 shoulder_pan 排列，图例只保留实线、虚线、预测断档背景和当前时间线的简短说明。
- Prediction gap 不再使用红色菱形点，而是在断档起止时间之间绘制横跨绘图区的半透明红色背景带。
- teleop、record、loading、rollout 与 jog 使用快照 `ts` 的绝对时间戳作为横轴；空闲/离线时继续使用原有无时间轴的紧凑模式。
- 白色竖线标记当前时间但不显示 `now` 文案；rollout 的未来窗口由最新 action chunk 的 `step_s × actions.length` 估算，relax/jog 等非 rollout 行为不预留未来时间。
- `loading → rollout` 保持同一时间轴和实时缓存，不因任务切换清空已有历史曲线；实时缓存扩展到约 60 秒，避免实线提前消失。
- Joint state 与 Commanded action 标题固定展开，不再提供收起按钮。

验收：静态 DOM、JS 语法、非仿真测试和浏览器 smoke 覆盖图例、固定 Y 范围、固定 now 线、rollout 未来窗口与窄屏布局。

## 已完成（2026-09-22）：工作区标签化

目标：在不改变机器人控制、Library 数据契约和异步刷新语义的前提下，将侧栏从纵向堆叠改为接近 VS Code 的单面板工作区。Library 的 Videos、Datasets、Snapshots、Models 使用同一内容区的标签页；右侧 Joints、Tasks、Hardware、Debug 同样每个任务一个标签页。

实现边界：

- 标签栏只控制 DOM 的 `hidden`、`aria-selected` 与 `tabindex`，不复制或移动表单控件，因此现有事件绑定、预设持久化和 WebSocket 更新路径保持不变。
- 左右标签选择分别保存在 localStorage；支持鼠标、触摸、左右方向键、Home 与 End，并提供完整的 `tablist` / `tab` / `tabpanel` 语义。
- 右侧每个标签面板拥有独立滚动容器，切换任务不会继承上一页的滚动位置；Library 折叠后仍恢复为窄图标轨道。
- 右侧主标签固定为 Joints / Record / Rollout / Debug / Hardware；Teleop 命令预览位于 Record 页下方，顶栏统一提供 E-STOP 与 Resume torque；所有 `Info` 折叠项统一改名为 `Command preview`。
- 窄屏沿用现有单列布局，标签栏保持横向可达且不产生页面级横向溢出。

验收：桌面与 1024px 窄屏浏览器检查通过，五组标签切换、键盘导航、刷新恢复与长表单滚动正常；非仿真测试 `133 passed, 2 skipped`。

## 已完成：Library 回放与本机 policy 扫描

目标

- 左侧 Library 只负责选择与导航，视频在主屏 `#cameras` 内播放，状态 pill 显示 `REPLAY`。
- 复用 record 时显示的顶部时间条：REPLAY 期间可拖动跳转；Stop 退出回放并恢复实时相机。
- Models 扫描本机 policy（HF hub cache 快照、`HF_LEROBOT_HOME`、`models_roots`），给出可读名称。
- Episode 独立成列（Library 与主屏之间）：点击条目只展开 episode，播放/编辑/删除/拖拽排序都是行右侧图标。
- Episode 的名称、任务、备注写进 Monitor 自己的 `store.json`，不改动原始 parquet / JSONL / 视频。
- 回放时下方 Joint state 与 Control state 各带时间条，与顶部时间条同步；可选是否让机械臂重放动作，默认关闭。

技术方案

- 回放是纯前端模式（`replayActive`），复用 `/api/preview` 与 `/api/preview/file`，不新增后端任务，避免与总线控制线程耦合。
- `applyStatus` 在 REPLAY 期间不再写 `#prog-fill`，改由 `onVizTime` 驱动；`#prog-seek` 只在 REPLAY 启用，其余时间保持 disabled。
- `list_local_models` 扩展为：hub cache 每个 `models--org--name` 取最新 snapshot（名称 `org/name`）+ lerobot home + 显式 roots，按 resolved path 去重，仍以 `config.json` 加权重文件判定 policy。
- Episode override 由 `Store` 按 `kind/id/episode` 存键；`/api/episodes` 读取时合并，`/api/preview` 一并返回 `episode_name` / `episode_note`。
- 机械臂重放走 `/api/joints` 队列，约 20 Hz 对 `act.*` 线性插值，仅在已连接、视频播放中、开关开启、非 E-STOP 且队列不积压时发送。
- Library 三列各自独立请求并立即渲染，policy 扫描不再阻塞 Videos / Datasets 首次显示；轮询用 in-flight 合并避免堆积。

验收

- 点击 Videos / Datasets 条目只在 Episode 列展开 episode，不自动播放；点击 episode 行的 play 图标才进入回放，pill 为 `REPLAY`。
- 回放主屏不出现 `merged`，顶部有 Play/Pause、回到最开始、机械臂重放开关（默认否）；拖动顶部或下方任一时间条三处同步跳转。
- Stop 退出回放、暂停视频并恢复实时相机；若后端任务在跑，同一次 Stop 仍然停止任务。
- Models 列出本机 policy，点击填入 Rollout 的 Policy path。

## 技术选型

- FastAPI + uvicorn：REST、WebSocket、MJPEG
- OpenCV：相机与视频落盘
- 可选依赖 sibling `lerobot`：SO-101 总线、leader、策略推理

## 已完成里程碑（2026-09-21）：录制、数据集与回放交互收敛

状态：已完成实现与回归验证。项目 venv 为 `49 passed, 2 skipped`（缺 pandas/pyarrow），完整可选依赖环境为 `51 passed`；Chrome headless 浏览器 smoke 覆盖 Library → Episodes → Replay、退出回放后的选择保持与 Capture 参数透传。

架构动机

将“用户意图、异步请求、运行时任务、持久化数据”分成四层：前端以显式状态机表达新录制、续录和回放，API 使用稳定的请求/响应契约，`RuntimeHub` 以 generation 标识任务所有权，数据适配层统一 LeRobot v3 的 episode、series 与时间戳。这样可避免旧请求或已取消任务覆盖新状态，也使 Library → Episodes → Replay 成为唯一、可预测的数据集查看路径。

技术风险

- rollout 在模型加载或其他 `await` 返回后仍可能继续运行；取消、Stop 和新任务必须递增 generation，并在每个异步边界及写盘前校验，旧 generation 不得更新状态、发送动作或落盘。
- 录制语义不得由目录是否存在隐式推断。UI/API 必须明确区分 `new` 与 `resume`，同时传递规范化的 output root；capture 请求字段、默认值、校验错误和返回结构保持单一契约。
- episode 的展示序号会因重排或删除改变。override 不能只依赖瞬时行号；操作后必须按稳定 episode identity 重映射，清除被删除项，禁止名称、任务和备注串到其他 episode。
- Library、Episodes 与 Preview 的并发请求可能乱序返回。每类请求使用递增 token 或 `AbortController`，仅最新选择可提交 UI；切换条目时立即清理旧 episode、播放源和错误状态。
- Chart.js/CDN 离线时页面仍须可操作；图表降级为无依赖的占位或轻量绘制，不能阻断录制、浏览和回放。数据集扫描、视频探测、模型发现及元数据解析等重任务必须转入线程池/异步执行，避免阻塞事件循环。
- LeRobot v3 可能出现 series 类型差异、分片索引和非零起点时间戳；适配层需将 series 转为一致数组，并把 episode 时间归一到从零开始、单调且有限的秒值，同时保留原始标识用于 override。

分阶段实施

1. 固化 capture API 契约：明确 `mode: new | resume`、output root、dataset/repo 标识、episode/task 参数及结构化错误；UI 分开呈现“新建录制”和“续录”，提交前展示最终输出位置。
2. 为 record/rollout 引入 generation-safe 生命周期；Stop、取消、失败和任务替换统一收口，所有异步恢复点、硬件动作与写盘路径执行所有权检查。
3. 收敛 Library → Episodes → Replay：选择 dataset 只加载 episode，选择 episode 才进入 replay；删除/重排后以稳定 identity 重建顺序并重映射 overrides。
4. 加入请求竞态保护、离线 Chart fallback，并将重扫描/解析端点改为异步非阻塞实现；切换与错误恢复不保留陈旧 UI。
5. 完成 LeRobot v3 series/时间戳归一化，补齐后端契约与取消竞态测试、前端状态机回归测试，并对录制、浏览、回放、空态、加载态、错误态和窄屏布局执行视觉 QA。

验收标准

- 快速执行 Start → Stop → Start 时，旧 rollout/record 不再复活、写入新 session、覆盖进度或发送动作；模型加载期间也可可靠取消。
- 新录制始终创建明确的新输出，续录只作用于用户选定的数据集；请求与结果均显示解析后的 output root，非法组合返回可读且稳定的错误。
- capture 请求在前后端使用同一字段集合；缺失、类型错误、路径冲突与硬件离线均有覆盖测试，UI 不以猜测方式补齐语义。
- Dataset 点击后按 Library → Episodes → Replay 流动；快速切换数据集或 episode 时只显示最后一次选择。重排/删除后 episode 的名称、任务、备注仍绑定原 episode，被删项不残留。
- 断网或 Chart.js 加载失败时，录制、episode 浏览、拖动回放和 Stop 仍可用；重型端点执行期间状态轮询、视频和控制请求保持响应。
- LeRobot v3 的 list、Arrow/Pandas series、分片 episode 与非零/异常时间戳样例均能稳定预览，归一化时间从 `0` 开始且单调。
- 自动化测试覆盖 generation 取消、capture 契约、override 重映射、请求乱序、离线 fallback 和 v3 归一化；桌面与窄屏视觉 QA 无遮挡、跳变、陈旧状态或不可达操作。

## 第二阶段里程碑（2026-09-21）：回放体验与双采样率

状态：已完成实现与自动化回归验证。本阶段没有改变机器人控制环的职责，以“单一前端回放状态机、统一 source metadata 契约、录制写入双时钟”为边界，收敛了退出、选集、拖动、图表和不同采样率下的持久化语义。

### 状态机

Episode 区域只允许以下三个互斥主状态；编辑是由 `expandedEpisode` 表示的正交子状态，不再隐式改变回放状态。

- `CLOSED`：`episodeSource = null`、`replayActive = false`。Episode 栏与分隔条均不占宽度，Library 无选中高亮，所有视频、机械臂重放和拖动会话均已停止。
- `BROWSING`：`episodeSource` 已加载且 `replayActive = false`。Episode 栏展示来源信息与 episode 列表，主区域继续显示实时相机。
- `REPLAY`：保留当前 `episodeSource` 且 `replayActive = true`。`loading | paused | playing` 由主视频媒体状态派生，不再维护第二套易漂移的布尔状态。

状态转移必须显式且幂等：点击 Library 来源进入 `BROWSING`；点击 episode 主行进入 `REPLAY`；仅点击铅笔按钮进入编辑；切换上一/下一 episode 保持 `REPLAY`；点击 Exit、Episode 栏关闭按钮、按 `Escape` 或启动任一控制任务进入 `CLOSED`。实现上分离 `leaveReplay()` 与 `closeEpisodeSelection()`：来源切换只退出旧回放再加载新来源，完整关闭才清空来源，避免旧清理逻辑误删新选择。

### 前端交互与显示验收

- Exit 与 Episode 栏标题区的关闭按钮执行同一 `CLOSED` 转移：先停止机械臂重放定时器，再暂停并清空媒体，恢复实时相机，关闭 Episode 栏并清除 Library 高亮；重复触发不得报错或重新打开面板。
- Library 折叠为约 `44 px` 的垂直图标轨道，不残留标题、列表或横向溢出；折叠按钮具备 `aria-expanded`、键盘焦点和可读标签，刷新后按现有偏好机制恢复状态。
- 回放顶栏与主回放区各提供一条可拖动的全宽进度条，两者与当前时间、所有视频和图表游标双向同步。拖动开始时记录原播放状态，拖动期间可暂停，结束后仅在原本播放时恢复。
- joint state/action 图取消底部范围条，以白色垂直游标表示当前回放时间。横轴使用线性秒值；hover 只显示该时刻及各 joint 的数值，不触发 seek。播放、暂停、单步与任一进度条跳转后，游标必须落在相同 episode-relative 时间。
- 点击 episode 主行默认直接查看回放，不进入编辑；编辑入口仅为明确的铅笔按钮并阻止事件冒泡。没有视频但有 series 的 episode 仍可进入检查模式并拖动图表时间。
- Episode 面板展示来源的 `title`、`subtitle` 与 `description`；空字段不占位，长文本截断并允许通过可访问方式查看完整内容，所有外部文本仅以 `textContent` 渲染。

### Source metadata DTO

`GET /api/episodes` 统一返回来源元数据，前端不再从路径或条目类型猜测展示文案：

```json
{
  "kind": "video | dataset",
  "id": "stable-source-id",
  "source": {
    "title": "Display title",
    "subtitle": "Task, repository or local recording",
    "description": "Optional dataset description"
  },
  "playable": true,
  "has_video": true,
  "episodes": []
}
```

本地视频的 `title` 来自显式名称或稳定 id，`subtitle` 优先使用 task/repository/“Local recording”，`description` 来自已保存的 description/note，禁止暴露本机绝对路径。LeRobot 数据集优先读取 repository、task 与元数据描述；缺失时退化为 `source · fps · episode count`。空字符串合法且由前端隐藏。迁移期保留既有顶层 `title` 作为只读兼容别名，新代码只消费 `source`。

### `action_fps` / `video_fps` 兼容规则

- `action_fps` 表示 observation/action/series 的采样率；`video_fps` 表示每路相机视频的目标采样率。旧字段 `fps` 继续作为 `action_fps` 的兼容别名，并在数据集元数据中保留 `fps = action_fps` 以兼容 LeRobot 读取器。
- 请求解析优先级为：`action_fps` 显式值 > 旧 `fps` > 配置默认值；`video_fps` 显式值 > 配置 `video_fps` > 旧数据集的 `fps`。响应与新写入元数据必须同时明确返回 `action_fps`、`video_fps`。
- `action_fps` 与 `video_fps` 均为有限正数；两者不得高于机器人控制环频率。示例配置 `control_fps = 30`、`video_fps = 30`、`action_fps = 15` 合法；超出控制环可唯一采样能力的请求返回稳定的 `400`，不得通过复制动作样本伪造高频率。
- 续录必须解析既有数据集速率与编码格式；显式请求与既有 `action_fps`、`video_fps` 不一致时返回稳定、可读的 `400`，不得在同一数据集中静默混合采样率。旧数据集没有新字段时按 `fps` 同时推导两者。
- 相机实际供帧率低于目标时允许视频层复用最后一帧维持时间轴，但元数据和 UI 必须区分 requested/effective rate；该行为不得增加 action 样本数。

### Writer 双时钟与独立计数

控制循环每 tick 使用单调时钟分别判断 `action_due` 与 `video_due`，各自推进下一截止时间；禁止先按低频捕获再复制成高频视频。两条时间线共享 episode `t0`，但分别维护 `action_frame_index` / `action_frames` 与每路相机的 `video_frame_index` / `video_frames`。

- `action_due` 只写 observation、action、timestamp 与其他 series；相机卡顿时不得补造动作样本。
- `video_due` 只快照和编码相机帧；`video_copies` 仅用于视频掉帧、相机延迟或结束补齐，不得推进 action 计数。
- 多相机各自维护最后有效帧和独立计数，同时以共享 episode-relative 时间对齐。episode 元数据记录 `action_fps`、`video_fps`、`action_frames`、每路 `video_frames`、duration 及 requested/effective rate。
- 截止时间按累计目标周期推进并在严重落后时有界追赶，避免浮点取模漂移与一次 tick 无限补帧；写盘由单一 writer 所有权或同一锁串行化，Stop/取消后旧 generation 不得继续增加任一计数。

### 竞态与边界条件

- Episodes 与 Preview 请求分别使用递增 generation 或 `AbortController`。关闭、切换来源、切换 episode 时先递增 token；任何迟到响应在提交 DOM、媒体源或错误状态前必须再次校验，不能使 `CLOSED` 复活或让来源 A 覆盖来源 B。
- 关闭与机械臂重放并发时，先停止定时器并使 generation 失效；已在途 `/api/joints` 完成后不得重新调度。启动 Record/Rollout/Teleoperate 同样原子进入 `CLOSED`。
- 多视频 seek 统一使用 episode-relative elapsed time，并按各流 `streamStart + elapsed` 设置 `currentTime`，禁止把第一路视频的绝对时间直接复制给其他流。
- 空数据集、series-only、video-only、相机延迟加入、单路掉线、零长度或异常时间戳、快速 A → B → Close，以及拖动过程中 Exit 均必须收敛到确定状态；所有清理函数可重复调用。

### 分阶段实施

1. 先固化 `CLOSED/BROWSING/REPLAY` reducer、关闭入口与 Library 折叠布局，并加入请求 generation，形成唯一状态所有权。
2. 实现双进度条、episode-relative 多视频同步、图表白色游标与 tooltip，再将 episode 主行点击和编辑按钮语义解耦。
3. 扩展 `/api/episodes` 的 source metadata DTO，补齐本地录制与 LeRobot 数据集的 subtitle/description 适配及兼容回退。
4. 扩展 capture 配置与 API 的 `action_fps` / `video_fps`，在 writer 引入双截止时间、独立计数与 requested/effective 元数据。
5. 集中完成竞态、旧数据兼容、相机掉帧与 UI 状态机回归，并执行桌面、窄屏和仅键盘视觉/可用性 QA。

### 测试与完成标准

- 后端契约测试覆盖本地视频与 LeRobot 的 source DTO、缺失元数据回退、旧 `fps` 请求/数据集兼容、非法速率和续录速率不匹配。所有校验错误的状态码与错误结构稳定。
- writer 时间测试使用可控单调时钟：录制 `2 s`、`video_fps = 30`、`action_fps = 15` 时应得到约 `60` 个视频帧、`30` 个 action 样本和约 `2 s` duration；另覆盖相机停顿、单路掉线、多相机延迟加入、无视频、Stop 与 generation 取消，且 action 计数不受视频补帧影响。
- 前端浏览器测试覆盖 Library 点击仅进入 `BROWSING`、episode 主行进入 `REPLAY`、铅笔进入编辑、Exit/栏关闭按钮/`Escape`/任务启动进入 `CLOSED`、`44 px` 折叠轨道与可访问属性、series-only episode 可检查。
- 回放同步测试验证顶栏与主区进度条、时间文本、多视频和图表游标始终一致；图表不存在底部 seek 条，白色游标随播放与跳转移动，hover 能显示准确时间和值且不改变播放位置。
- 竞态测试人为延迟来源 A，再选择 B 或关闭面板；A 的结果不得覆盖 B 或重新打开 Episode。机械臂重放在关闭后不得继续发送或重新调度请求。
- `node --check`、后端测试套件与浏览器 smoke 全部通过；桌面、窄屏和 coarse-pointer 视觉 QA 无溢出、遮挡、跳变、陈旧高亮或不可达关闭/编辑操作，才可将本里程碑标记为完成。

### 最终实现与验证

- Episode 标题栏具备独立关闭入口，Exit 与关闭按钮统一清理选集和回放；Library 折叠为窄轨道。episode 主行负责查看，编辑仅由独立按钮触发，来源的 title/subtitle/description 通过统一 source metadata 展示。
- 顶栏与主回放区的 timeline 双向同步；joint state/action 图改为白色当前时间游标与时间/数值 tooltip，不再使用图表底部范围条。回放布局已适配桌面与窄视口，多视口 smoke 在上一轮验证中通过。
- 录制链路已分离 `action_fps` 与 `video_fps`，rollout 另使用 `policy_fps` 控制推理频率；旧 `fps`、新建与 resume 路径保持兼容。writer 采用独立 action/video 时钟与计数，相机晚接入按真实 offset 对齐，不把视频补帧计入动作样本。
- session、episode 与 preview 返回真实的 action/video 帧数、速率、duration 和晚接入信息；preview 使用 safety generation，关闭或切换后的迟到响应不能复活旧来源或覆盖当前 UI。
- 最终自动化结果为 `65 passed, 2 skipped`；同时通过 JavaScript `node --check`、Python compile 检查与 `git diff --check`。多视口浏览器 smoke 沿用上一轮通过结果。
- 尚未执行真实机械臂与多相机的长时间运行验证；自动化已覆盖双采样率、resume、晚接入与竞态，但硬件时钟漂移、驱动抖动、持续编码负载和长录制磁盘行为仍需实机 soak test 后确认。

## 后续里程碑（2026-09-21）：回放时间轴与 Windows 断连稳定性

状态：已完成实现。本轮收敛了两个高频边界：回放界面以唯一 episode clock 驱动所有时间表现；Windows 下机器人、leader 与串口设备以有界、幂等、可重连的生命周期完成断连。两者分别属于前端展示状态与后端硬件所有权，不互相引入隐式依赖。可运行的非仿真测试已通过；受环境和本轮用户验证范围限制，完整 `test_sim` 与浏览器检查未执行，详见“最终实现与验证”。

### 范围

- 移除全局应用顶栏中的进度条与回放 seek，只保留紧凑的全局状态、任务动作和始终可见的 Stop / E-STOP；消除 mode 重复字段，将任务 elapsed/episode 信息合并为单一上下文，窄屏不得把 E-STOP 放入需要横向滚动才能到达的区域。
- Replay 顶栏内只保留一条主时间轴；播放/暂停、回到开头、当前时间/总时长、主时间轴与 Exit 组成同一条 transport row，加载/错误状态留在相邻的来源信息区。删除 Replay 底部按钮栏及其 SCAN、RELAX、PLAY/PAUSE、REWIND、CONNECT ARM、EXIT 重复入口，释放主画面垂直空间；全局或 Hardware 区已有能力不在 Replay 内重复。桌面、窄屏和 coarse-pointer 布局均保留完整时间轴与可达控制。
- Joint state / Action 图继续由 Chart.js 绘制数据与 hover tooltip，白色游标只读取统一的 episode-relative clock。鼠标或触摸在图表 canvas 上按下并拖动可直接 seek；普通 hover 不改变播放位置，主 Replay range 仍是完整的键盘入口。
- 统一 series、视频 clip 与晚接入相机的时间域：横轴覆盖完整 episode duration，视频仅按 `streamStart + episodeElapsed - cameraOffset` 从属于主时钟，任何媒体事件都不得反向夺取时钟所有权。
- Windows 断连覆盖 follower、leader、相机/串口读取线程以及 teleop、record、rollout、hold/idle 中的主动 Disconnect、Stop/E-STOP、USB 拔出和设备异常。断连后必须停止旧任务、释放句柄、清除所有权并允许同端口重新连接。
- 不在本轮改动录制格式、策略接口或数据集契约；真实硬件长时 soak test 是完成本里程碑的必要验证，不以 mock 回归替代。

### 架构动机

回放侧采用“单一时钟、多个只读投影”：`episodeElapsed` 是唯一可写时间状态，RAF、range、canvas drag 都只通过一个 seek 入口更新它；视频位置、时间文本、range 值和 Chart cursor 都由该状态派生。这样可以删除全局/Replay 双 slider 的同步分支，也避免媒体时钟、Chart 横轴与 late-camera offset 之间互相纠正造成跳变。

Windows 硬件侧采用“单一设备所有者、generation 失效、有界关闭”：连接实例和后台 I/O 只属于当前 generation；Disconnect 首先使 generation 失效并禁止新动作，再请求任务/线程退出，最后在唯一所有者处关闭 SDK/serial handle 并发布 `disconnected`。阻塞的设备调用不得占用 FastAPI 事件循环；关闭函数必须可重复调用，并让迟到的读写结果在提交状态或动作前被丢弃。

两个方向共享同一原则：用户发出的 Exit、Stop、E-STOP、Disconnect 或新选择先改变所有权，再执行可能阻塞的清理。UI 和后端均不得等待旧工作完成后才声明新状态，也不得让旧异步结果复活已关闭的对象。

### 主要风险

- Chart cursor 当前随每个 RAF 对两张完整图执行 draw；长 series 可能造成主线程抖动。实现需在不破坏 tooltip 的前提下跳过同一像素的重复绘制或限制 cursor 重绘频率，drag 中禁止触发完整 `chart.update()`。
- Canvas pointer capture、触摸滚动与 Chart.js 默认 mouse/touch tooltip 可能竞争。只有 active primary pointer 可以 seek；`pointerup`、`pointercancel`、`lostpointercapture` 与窗口级释放均需幂等收口，`touch-action: none` 只在 Replay 图表上启用。
- series 时长短于视频或相机存在正 offset 时，若横轴仍以最后一个 series 时间为上限，白线会越出 chart area。横轴必须以完整 episode duration 为上限，并对异常、空数据与零时长做有限值保护。
- Windows 串口或设备 SDK 的 read/write/close 可能阻塞、抛出 access denied/device removed，也可能在另一个线程仍持锁时无法释放。不得在事件循环或持有全局生命周期锁时等待不可控 I/O；需要有界 join、异常归一化与清晰的超时状态。
- Stop、Disconnect、E-STOP、USB 拔出及新 Connect 可同时发生。若检查与关闭分属不同所有者，可能产生旧线程继续发动作、重复 close、状态回退或 COM 端口长期占用；所有提交点必须校验 generation，句柄关闭只能由其创建者或明确移交的唯一 owner 执行。
- Windows 强制超时后线程可能仍滞留，不能通过伪造 `disconnected` 隐藏资源未释放。超时必须进入可诊断的 degraded/error 状态，阻止同一实例复用，并在日志中保留设备、阶段与异常信息。

### 验收标准

- 非 Replay 页面不存在全局进度条或可交互 seek；进入 Replay 后只出现一条主 range，且位于 Replay 顶栏。录制/reset 进度仍以紧凑文本表达，不丢失任务语义。
- Replay transport row 同时包含播放/暂停、回到开头、唯一主 range、时间读数与 Exit；这些控件在验收视口中保持同一操作行和清晰的视觉顺序。页面不存在 Replay 底部按钮栏或 `viz-bottom-*` 重复控件，移除后的视频区域获得对应的垂直空间，退出回放不需要滚动到页面底部。
- `episodeElapsed`、主 range、时间文本、多视频与两张图的白色游标在播放、暂停、重启、episode 切换及任意 seek 后误差不超过一个显示帧。series 早于视频结束、video-only、series-only 与 late-camera 样例中游标始终位于正确横轴范围。
- 鼠标和触摸从图表 25% 拖到 75% 可更新统一时钟；播放中拖动只在手势期间暂停并在释放后恢复，原本暂停时不自动播放。普通 hover 不 seek，tooltip 继续显示准确的时间和 joint 值；取消或丢失 pointer capture 后不残留拖动状态。
- Replay range 有可读 label、动态 `aria-valuetext`、清晰 focus-visible 与不小于 `44 px` 的触控命中区；键盘 Arrow、PageUp/PageDown、Home、End 均可操作。Chart.js 不可用时图表降级但主时间轴仍可 seek。
- 1440、1024、600 与 390 像素视口无页面横向溢出；Replay 时间轴不被隐藏，Stop / E-STOP 始终在首屏可达，触控模式下关键目标满足最小命中尺寸。
- Windows mock/仿真测试覆盖主动断连、重复断连、连接中取消、读写异常、close 异常、Stop 与 Disconnect 竞态、旧 generation 迟到返回及立即重连；任何旧任务在断连提交后不得发送动作、写 episode 或覆盖新连接状态。
- 真实 Windows 设备验证至少覆盖 follower 与 leader 的多轮 connect → idle/hold → disconnect → reconnect，以及 teleop/record 期间 Stop、E-STOP 和 USB 拔出。UI/WebSocket/API 在设备清理期间保持响应，断连完成后原串口句柄可被同进程重新打开，无僵尸控制线程或持续动作。
- 断连超时和硬件错误返回稳定、可诊断的状态与日志；成功断连为幂等结果，失败不得伪装成功。真实硬件连续运行与反复重连的 soak 结果需记录持续时间、循环次数和所有异常，完成前不得将本里程碑标记为已验证。
- 自动化与静态检查至少包括前端 DOM/交互 smoke、设备生命周期竞态测试、完整 pytest、`node --check`、Python compile 与 `git diff --check`；所有项目测试通过且无新增资源泄漏告警。

### 最终实现与验证

- 全局应用顶栏已移除进度条和回放 seek；Replay 使用紧凑 transport row，播放/暂停、回到开头、唯一主时间轴、时间读数与 Exit 位于同一操作区。Replay footer 及其重复按钮已删除，视频区域获得更多垂直空间。
- Joint state / Action 的当前时间白线改为独立 DOM overlay，由统一 episode clock 驱动，不再为移动游标重绘整张 Chart。图表 canvas 支持鼠标与触摸拖动 seek；tooltip hover 仍由 Chart.js 处理。拖动状态按 pointer id 和来源持有所有权，多指针、cancel、lost capture 与窗口释放不会互相结束或遗留 scrub。
- Windows 关闭链路已覆盖应用 lifespan 收尾、日志 handler 卸载、MJPEG 流生成器结束、WebSocket 断开与 `RuntimeHub.stop()`。关闭顺序先撤销任务/运行时所有权，再停止后台工作并释放资源；重复 stop 与客户端提前断开保持幂等，不让迟到工作重新激活运行时。
- 自动化结果为 `74 passed, 2 skipped`，统计时排除 `test_sim`；其余可运行测试和差异检查通过。
- 完整 `test_sim` 未验证，因为当前环境缺少 `scservo_sdk`。该缺口涉及依赖真实/仿真总线 SDK 的路径，不能由本轮通过结果推断为已覆盖。
- 按用户要求，本轮没有执行浏览器检查或多视口视觉 smoke；Replay transport、DOM overlay、触摸拖动与响应式表现仍需在后续浏览器验证中确认。

## 后续里程碑（2026-09-21）：Snapshot 库、可编辑备注与 VLA Model Debug

状态：已完成实现与可运行回归验证；真实硬件与浏览器视觉验证未在本轮执行。

### 架构动机

快照采用“目录即记录”的文件系统模型：`snapshots_root/<id>/snapshot.json` 是唯一索引，
目录名是权威 ID，相机 JPEG 与缩略图是附属资源。这样用户可以直接修改或复制目录，
monitor 只负责校验、重扫和展示，不引入数据库迁移或把机器人数据写入原始数据集。

VLA 调试复用控制线程已有的 policy 缓存，但通过显式 debug lease 与 teleop、record、
rollout、jog、relax 等动作入口互斥。lease 在 idle（含 hold）或 offline（未连接 follower）
且无 pending 任务时授予，不要求 follower 在线；推理在事件循环外执行，默认只返回
action chunk，不向机械臂发送动作。这使模型调试可以复用生产推理路径，同时保留
E-STOP、Stop 和任务切换的现有所有权语义。

### 核心模块

- `SnapshotLibrary`：目录扫描、manifest 校验、路径约束、相机 key 归一化、复制/删除、
  静态相机文件和 `preview.jpg` 缩略图。
- `JsonStore.library_overrides`：按 video/dataset 来源保存 note 与 description，
  删除或重排 episode 时沿用既有 override 语义。
- `ControlLoop` debug lease：控制线程授予/释放 token，lease 生效时显示 `debug`，
  阻断动作启动；E-STOP、Stop 和任务切换会清除 lease，follower 断连不会取消只读推理。
- `policy.predict_action_chunk`：优先原生 chunk API，失败时回退逐帧 `select_action`，
  统一 postprocess、关节映射、补齐、截断和 degraded 标记。
- Web：Snapshots Library 分组、行内 note/description 编辑、顶部保存快照、快照查看，
  以及 Model Debug 面板、preset、相机映射和虚线 action chunk overlay。

### 风险与边界

- 快照 ID、相机 key、JSON 内容和路径必须经过严格校验，任何读写都不得越过
  `snapshots_root`；损坏 manifest 只跳过该目录，不能使整个 Library 失效。
- 请求体使用 base64 JPEG，必须在解码前限制单张、相机数和总量，避免内存放大攻击。
- policy 对象可能有状态，推理必须串行；lease 的授予、释放、estop 和任务切换清理必须
  幂等，不能让迟到推理覆盖新任务或让动作入口绕过 lease。
- 快照图像和关节不在 UI 内编辑；note/description 是 monitor override，不回写原始数据。
- 第一版硬件状态通过“保存快照后再调试”进入，不做硬件直连推理。

### 分阶段实施

1. 快照配置、`SnapshotLibrary`、Hub/API 接入、文件服务与快照测试。
2. library override 存储、video/dataset/episode 合并、删除清理、前端行内编辑与测试。
3. debug lease、chunk 推理、`/api/debug/infer`、Model Debug 面板、快照查看与
   chart overlay，并完成全量回归和本地记录更新。

### 验收标准

- 创建、列表、读取、编辑、复制、删除快照均以目录名 ID 为准；损坏目录跳过，
  路径穿越、超限 base64 和非法 key 有稳定错误。
- video/dataset 的 note 与 description 可编辑并持久化；episode description
  以 override 优先，删除来源时同步清理 override。
- 无 idle lease 时 infer 返回 409；无策略或策略加载失败返回 400；
  chunk 推理返回策略/降级路径、latency、fps 与关节动作序列，且不自动控制机械臂。
- 顶部快照按钮能从回放或硬件状态抓取图像与关节；快照视图显示相机、扁平状态线和
  action chunk；Model Debug 可保存 preset、映射相机、运行推理并发送首步。
- `pytest`、`node --check`、`git diff --check` 通过；硬件与浏览器验证边界在
  `docs/dev_log.md` 中明确记录。

### 最终实现与验证

- `SnapshotLibrary` 以 `snapshots_root/<id>/snapshot.json` 为唯一记录，目录名是权威 ID；
  扫描时忽略 JSON 内旧 ID、跳过损坏目录、校验 ID 与解析路径，并提供相机 JPEG 与
  `preview.jpg`（首张相机图缩放到宽 ≤ 320）静态文件路由。
- 快照 API 覆盖创建、列表、读取、编辑、复制、删除与文件服务；相机载荷为 JSON +
  base64 JPEG，单张 ≤ 8 MiB、相机数 ≤ 12、解码总量 ≤ 48 MiB，超限统一返回 413。
- `JsonStore.library_overrides` 保存 video/dataset 的 note 与 description，并在
  `/api/videos`、`/api/datasets`、`/api/episodes` 合并；episode description 以 override
  优先于数据集自带描述，删除来源时同步清理 override。
- `ControlLoop` 新增 debug lease：在 idle（含 hold）或 offline（未连接 follower）且无
  pending 任务时授予 token，`display_mode()` 显示 `debug`，
  teleop/record/rollout/jog/resume/capture 等入口被拒绝；E-STOP、Stop 与任务切换清除
  lease，follower 断连不取消只读推理，lease 生效时不再发送 hold 位姿。
- `policy.predict_action_chunk` 优先使用 `policy.predict_action_chunk`（`(B, T, A)`），
  不支持或返回非法形状时回退逐帧 `select_action` 并标记 `degraded`；两条路径统一
  postprocess、`observation_to_pose` 映射、输入关节补齐与 `chunk_size` 截断。
- `POST /api/debug/infer` 返回 `strategy`、`degraded`、`latency_ms`、`fps`、`actions` 与
  `warnings`；lease 冲突为 409，策略加载或推理失败为 400，推理在 `asyncio.to_thread`
  中执行并在 `finally` 释放 lease。
- Web 侧新增顶部快照按钮、Snapshots Library 分组、快照编辑面板与快照查看模式（静态
  相机图、两点扁平状态线、chunk 图），以及 Model Debug 面板：preset、模型下拉、task、
  device、extra 参数、chunk_size、fps、相机映射、Run、状态行与 Send first step。
- action chunk 以同色虚线从当前 elapsed 向右叠加到 command action 图；快照模式从 0 起
  整段显示；切换 episode、打开 snapshot、拖动时间轴、离开回放或重新 Run 都会清空 chunk。
- 自动化结果为 `105 passed, 2 skipped`（排除 `test_sim`），并通过 `node --check` 与
  `git diff --check`；包含 `test_sim` 时为 `128 passed, 2 skipped, 5 failed/errored`，
  非通过项全部来自当前 venv 缺少 `scservo_sdk`。
- 未执行真实硬件与浏览器视觉验证：快照抓帧、note/description 行内编辑、chunk overlay
  与 Send first step 的手动清单仍待在有机械臂与相机的环境确认；`test_sim` 因当前 venv
  缺少 `scservo_sdk` 无法运行，与本次改动无关。

## 2026-09-21（Rollout 预测对照与模型库闭环）

### 目标

在现有 Replay、Snapshot 与 Model Debug 基础上，修正模式标题和图例状态，并把模型管理、
推理评分和 rollout 预测轨迹做成可直接解释、可回归验证的工作流。

### 设计

- 模式标题由单一的 `snapshotActive` 派生：Snapshot 只显示 SNAPSHOT，普通 episode replay
  只显示 REPLAY；标签 `.hidden` 必须有明确的样式所有权，不能依赖不存在的全局规则。
- 模型库注册表保存用户可编辑的 remote/revision，Hugging Face 搜索与本地路径共享同一
  注册语义；重复 remote 采用更新而不是追加，权重更新后重新解析本地路径。
- debug inference 以 replay 的 `act.*` 为标准 command reference，报告 action-chunk
  MAE、RMSE、DTW、归一化分数与参考覆盖率，不以单一不可解释分数替代原始误差。
- rollout 预测轨迹使用带单调 ID 的 chunk 记录。后一个 chunk 覆盖重叠时间窗，超过
  正常采样步长的断档标红；预测的可用时间从实际推理完成时刻计算，避免高延迟被画进过去。

### 实施任务

1. 修复 REPLAY/SNAPSHOT 标签互斥和 Chart.js 预测图例过滤。
2. 完善模型搜索、拖拽/文件入口、远程地址编辑、去重注册和更新路径。
3. 结构化展示 chunk 评分，补充 reference 覆盖范围与空参考状态。
4. 让 rollout chunk 记录具备唯一 ID、完成时刻和迟到断点语义，并保持两张图同步。
5. 增加 metrics、model hub、API、loop 与静态 UI 约束测试，执行回归与浏览器 smoke。

### 验收标准

- Snapshot 视图不会同时出现 REPLAY 标签；虚线预测数据不出现在图例中。
- 每个相机可独立决定是否输入模型，至少选择一台相机才能 Run inference。
- 模型可从 Hugging Face 搜索、remote 地址或拖拽 model card/本地路径添加，可编辑
  remote/revision 并显式更新权重。
- 有标准 command reference 时显示 chunk 分数及常用误差指标；无参考时明确说明不评分。
- rollout 两张图在预测时间经过后保留虚线，新推断覆盖重叠区，断档/迟到显示红色断点。
- `pytest`、`node --check`、`git diff --check` 与可执行的浏览器 smoke 通过，无法覆盖的
  真实硬件边界记录在 `docs/dev_log.md`。

### 回归修复（2026-09-22）

实时 rollout 的虚线预测不得再触发第二次 policy inference。overlay 必须复用
`select_action()` 已填充的 action queue；模型调用次数与关闭 overlay 时一致，避免
SmolVLA 等大模型因额外 chunk 推理拖慢控制线程或出现周期性动作断续。

rollout 必须使用 LeRobot 自带的 `create_inference_engine()`，不得在 monitor 内手写
替代 engine。RTC 参数必须完整进入 `RTCInferenceConfig`，并安装到 policy 的
`rtc_config` 后调用 `init_rtc_processor()`。E-STOP/Disconnect 仍先执行硬件安全动作，
再停止 engine。

cached policy 的 repo id 必须先通过 `local_files_only=True` 解析到本地 snapshot；
本地路径和已缓存 repo 不得进入网络 fallback。Policy path 的模型选择必须写入实际
snapshot 路径，并可从 cached policy 候选列表直接选择。

## 后续里程碑（2026-09-21）：Blender 远程虚拟相机接入

状态：已实现并通过自动化与当前运行中的 Blender 流验证。

目标

- Monitor 扫描本机设备时，同时读取 Blender 注册表中的 `cameras[]`，把每一路
  注册成独立的远程 MJPEG 相机。
- 远程相机使用与本地相机相同的显示、录制和策略输入接口；URL、画幅、FPS 与
  质量由 Blender 面板控制，Monitor 只允许修改名称、启用、主视图和策略输入。
- 本地扫描只维护 DirectShow / V4L 设备，不能因为远程相机不在本机枚举结果里
  就将其删除。

实现

- `sim.py` 新增 `sim_cameras()`，优先解析 `cameras[]`，并兼容旧注册表的单数
  `camera`。
- `cameras.py` 新增 `RemoteMjpegCamera`：连接超时、增量 JPEG 分帧、单帧上限、
  指数退避重连，以及 `latest_jpeg/latest_bgr/latest_rgb/wait_for_frame` 接口。
- `CameraHub` 独立维护远程设备与后台发现线程；手动扫描同步远程列表，断开的
  Blender 流从列表移除，已有用户的启用/显示/策略开关设置保留。
- 前端远程相机卡片不再暴露宽度、端口、自动对焦、焦点和网络流按钮，主视图
  元数据显示 Blender 与目标 FPS；策略配置使用远程 URL。

验收

- 当前 Blender 注册表的单数 `camera` 回退路径已实测注册为
  `blender_sim_follower_camera_1`，成功收到 `640×480` JPEG 帧。
- 新增测试覆盖多相机解析、单相机回退、远程注册与移除、本地重扫不误删、
  MJPEG 分帧、远程只读 API 返回 400。

## 2026-09-22（Library 搜索与实时图表时间语义）

状态：已完成实现、静态回归与 Chrome 浏览器验证。

### 目标

把 Library 从“每个分组各自滚动”收敛为单一工作区滚动，并让四类资源都能按标题或
note 即时过滤。底部实时图表不再把模式切换当作数据边界：所有控制模式共享同一段
绝对时间历史，只有 rollout 需要白色 `current time` 线与预测未来窗口。

### 实现

- Library 列表移除 `max-height` 与内部 `overflow-y`，由 `#library` 独占滚动；
  Videos、Datasets、Snapshots、Models 各自增加标题/note 搜索框，Models 页的本地
  过滤与 Hugging Face Hub 搜索保持分离。
- Joint state 与 Commanded action 的实时点统一使用快照 `ts` 写入时间轴。模式从
  jog、teleop、record 切到 rollout 时保留已有曲线，不再因轴类型变化清空缓存。
- 模式切换写入灰色竖线，并在底部 action 时间轴内以竖排标签记录模式名；标签不会在
  连续切换时横向重叠。
- `current time` 白线只在 rollout 中显示；relax、teleop、record 与 jog 直接沿时间
  轴追加。replay 使用秒数，live 使用本地 `HH:MM:SS`，时间刻度始终可见。
- prediction gap 继续使用半透明红色背景带；图例文案修正为 `command`、`prediction`，
  并补充 `prediction gap`、`mode change` 与 rollout 专用的 `current time`。
- 图表悬停改为自定义时间提示卡：同一时间同时列出 actual 与 prediction；悬停在红色
  gap 内且没有预测点时，仍回退显示该时间之前最近的 actual。提示卡时间与底部刻度
  共用同一个格式化函数，因此 wall clock、mode age、since start 三种单位完全同步。
- 左侧图例新增 `Time` 区：可切换 `wall`、`mode`、`start` 时间单位；`mode` 单位会在
  每条模式切换线上标记 `0s`，后续刻度从该次切换重新计时。
- 监控窗口新增 `2s / 10s / 30s / 1m / 10m` scale 选择并持久化。原始历史最多保留
  36k 点，绘制前按当前窗口降采样到 1200 点以内，避免 10min 视图把全部高频点交给
  Chart.js。图例不再显示 `mode age` 与 `total`。
- 图表只绘制真实采样点，右侧新采样延迟一帧进入画布；画布按 60Hz 每帧重绘。降采样
  使用固定绝对时间桶，窗口左端单独用真实相邻采样插值出边界点，使曲线始终连到左边缘。
  时间刻度文字使用 140ms 交叉淡化，曲线改为零张力并使用 butt cap，避免边缘变粗。

### 验证

- 非仿真回归：`134 passed, 2 skipped`。完整测试为 `160 passed, 2 skipped`，其余
  5 个失败/错误均来自当前 venv 缺少 `scservo_sdk`。
- `node --check` 与 `git diff --check` 通过。
- Chrome 浏览器 smoke 14/14 + 7/7 通过：前一组覆盖 Library、历史与布局；后一组覆盖
  actual/prediction 同屏、gap 内 actual 回退、tooltip/底部时间戳一致性、时间单位切换
  与紧凑图例布局。
- scale 专项 smoke 5/5 通过：五档选项、移除 mode age/total、2s 与 10m 轴范围以及
  长时间窗口的 1200 点降采样上限。
- 刷新专项通过：右尾延迟一帧、左边界点精确落在 `scales.x.min`、tooltip 相邻采样
  插值得到连续数值、时间刻度淡入淡出，并确认曲线 tension 为 0 且 cap 为 butt。
