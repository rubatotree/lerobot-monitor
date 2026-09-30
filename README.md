# LeRobot Monitor

用于 SO-101 的本地监控与控制网页。它提供相机预览、关节控制、遥操作、录制、数据集与模型管理、策略 rollout、云端模型管理（SSH 主机、GPU、部署与运行环境），以及无硬件时的虚拟从臂和 3D 预览。

项目默认只监听 `127.0.0.1:8090`。控制接口没有登录认证；需要从其他设备访问时，请只在可信网络中使用，并在入口处配置认证。

## 云端模型 Cloud 面板

主页右侧的 **Cloud** 面板已合并原独立 `lerobot-cloud-manager`（端口 8095）的全部功能，浏览器只需要访问 Monitor 一个地址：

- **服务器**：注册/选择 SSH 别名，Probe 探测解释器与目录，Initialize/Upgrade 安装与升级云端服务，Runtime 安装模型依赖环境，Connect/Disconnect 管理隧道。
- **GPU**：显存、健康状态与主要占用用户/程序；Load 时只能选择真正可用的卡。
- **Cloud models**：从 Hugging Face、服务器路径或本地上传添加部署，支持 Load / Unload / Logs / Use / Remove；**Use** 会把部署登记进普通模型库，供 Rollout 与 Debug 选择。
- **Recent jobs**：合并本地初始化/上传/运行环境任务与远端服务任务。

面板的轮询只在面板可见且浏览器标签页处于活动状态时进行。SSH 凭据与云端令牌始终只保留在 Monitor 后端；`/api/cloud/*` 与原独立管理器共用 `~/.lerobot-cloud-manager` 状态，因此两处添加的服务器互相可见。

独立服务仍可运行以兼容既有脚本：`lerobot-cloud-manager --port 8095`。详细接口、运行环境与验证记录见 [docs/cloud_models.md](docs/cloud_models.md)。

## 环境

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)（推荐）
- 浏览器支持 WebGL；虚拟从臂不需要实体设备
- 真正的 SO-101 控制和策略推理需要在同一 Python 环境安装兼容的 [LeRobot](https://github.com/huggingface/lerobot) 及其硬件/模型依赖

## 快速开始

在仓库根目录运行：

```powershell
uv sync --extra dev
uv run lerobot-monitor --config config.example.yaml
```

打开 [http://127.0.0.1:8090/lerobot/](http://127.0.0.1:8090/lerobot/)。示例配置默认启用虚拟从臂，串口自动连接关闭，可以先在无硬件环境中检查界面。

如需使用自己的串口、数据目录或相机设置，复制 `config.example.yaml` 为 `config.yaml` 后编辑。后者已被 Git 忽略：

```powershell
Copy-Item config.example.yaml config.yaml
uv run lerobot-monitor --config config.yaml
```

在 Linux/macOS 上可用 `cp config.example.yaml config.yaml`，然后执行相同的 `uv` 命令。

Windows 也可以运行 `.\run.ps1`。它优先读取本地 `config.yaml`，否则读取示例配置；若检测到相邻的 LeRobot 开发环境，会沿用该 Python。也可设置 `LEROBOT_MONITOR_PYTHON` 指向已经装有 LeRobot 的解释器，设置 `LEROBOT_SRC` 指向 LeRobot 的 `src` 目录。

## 配置与数据

- `server.host` 默认是 `127.0.0.1`。显式改为 `0.0.0.0` 或使用 `--host` 会开放控制接口；应用本身没有认证。
- `robot.port` 和 `leader.port` 初始为空。连接实体设备前，填入对应端口和校准 ID。
- 缓存迁移后可设置 `huggingface_home`；若校准文件仍在旧目录，分别设置 `robot.calibration_dir` 和 `leader.calibration_dir` 为包含 `<id>.json` 的目录。
- `recording.root`、`library.*_roots`、`robot_models.root` 可改为你自己的目录。
- 运行数据默认放在 `data/`，不会提交到 Git。Hugging Face 凭据请使用其标准本机认证方式，不要写入配置文件。
- Hugging Face 连接也读标准环境变量：`HF_HOME` 优先于配置里的 `huggingface_home`；`HF_ENDPOINT` 用于下载与搜索（可以是 `https://hf-mirror.com` 这类镜像）；上传需要写权限，取 `HF_TOKEN`（优先）或 `huggingface-cli login` 保存的登录凭据。`HF_ENDPOINT` 指向只读镜像时，写入会自动改用 `https://huggingface.co`；自建 Hub 或其他可写 endpoint 用 `HF_UPLOAD_ENDPOINT` 显式指定。上传前会先用 `whoami` 校验凭据，被拒时立即报错并指出要改的变量，不会传到一半才失败。启动横幅会打印一行 `hf: cache=… endpoint=… upload=… token=…`，其中 token 只显示来源（`env HF_TOKEN`／`login store`／`missing`），不显示令牌本身。
- `rollout.preload_dependencies` 默认开启：服务在摄像头和控制线程启动前导入策略共享依赖，避免首次 Load 在运行中的线程间竞争。此步骤不构建模型、不读取权重；阶段耗时写入启动日志与 `runtime.policy_dependencies`。仅使用监控功能时可设为 `false`，恢复按需导入。未安装 PyTorch/LeRobot 时跳过，其他依赖导入失败会记录错误并继续启动监控。

## 开发与验证

RTC rollout 使用 LeRobot 自带的 `RTCInferenceEngine` 与 `ActionQueue`。当前后台观测准备和 chunk 事件接入需要本地 LeRobot 分支提交 `79f1e10d`（或包含相同接口的后续版本）；旧版本会在启动 RTC 时明确提示不兼容。通过原有 `inference.type=rtc` 开启，不需要另起 RobotClient 或 PolicyServer。

`rollout.observation_max_age_s`、`camera_max_skew_s` 和 `inference_timeout_s` 分别控制输入最大接收年龄、多相机时间差以及无可用动作超时，默认 1、0.25、30 秒；需按任务调整。陈旧输入会让原生引擎报错退出，超时停止 rollout 并保持当前目标。这些接收时间不是相机曝光时间，也不是实机安全时延保证。

```powershell
uv run pytest -q
node --check src/lerobot_monitor/web/static/app.js
node --check src/lerobot_monitor/web/static/cloud-panel.js
node scripts/test-rollout-lanes.mjs
node scripts/test-cloud-panel.mjs
node scripts/verify-model-delete-id.cjs
node scripts/verify-cloud-panel.cjs
node scripts/verify-library-model-tabs.cjs
```

应用后端位于 `src/lerobot_monitor/`，前端资源位于 `src/lerobot_monitor/web/static/`。控制循环独占机械臂总线；HTTP 接口向循环投递命令。硬件依赖按需导入，策略共享依赖默认在启动时准备；基础页面与虚拟从臂可在未安装模型依赖时独立运行。

## 许可与资源

项目源码采用 Apache-2.0，见 [LICENSE](LICENSE)。内置 Three.js、Chart.js、URDFLoader 和 SO-101 模型资源的来源与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
