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
