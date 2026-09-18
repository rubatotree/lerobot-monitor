# LeRobot Monitor

独立于 `lerobot-record` / `lerobot-teleoperate` / `lerobot-rollout` 的机械臂监控与控制网页。

进程自己打开 follower 总线和相机，因此采集/推理程序未启动、也未分配任务时，界面仍持续返回关节位姿与摄像头画面。可在同一页面里点动关节、测遥操作、录制、部署模型，并把 rollout 的视频与关节写成 session。

## 运行

前置：Windows 上先开 `robot_tools/win_cam_server.py`，把 front/side 推到 `localhost:5000/5001`（见仓库根目录 `scripts.ps1`）。机械臂端口默认 follower=`COM6`、leader=`COM5`。

机械臂/策略推理需要 sibling 的 `lerobot` 环境（本仓库的 `uv sync` 只装网页服务本身）：

```powershell
cd d:\repos\lerobot\lerobot-monitor
.\run.ps1
# 或显式：
#   d:\repos\lerobot\lerobot\.venv\Scripts\python.exe -m pip install -e .
#   d:\repos\lerobot\lerobot\.venv\Scripts\python.exe -m lerobot_monitor --config config.yaml
```

无硬件时可用独立 venv 跑 UI：`uv sync --extra dev; uv run lerobot-monitor`。

浏览器打开 `http://127.0.0.1:8088`。

没有机械臂时服务同样启动：相机按配置重连，关节面板显示 offline，可稍后在界面里点 Connect arm。

## 界面能力

- 摄像头 MJPEG、关节数值与时间序列
- 单关节滑条 / Home / Zero / Apply targets
- 遥操作测试（leader → follower）
- 录制：每个 session 一份 `joints.csv` + `videos/*.mp4`
- 部署策略（HF repo 或本地 checkpoint），可选把 rollout 写成同一套 session
- E-STOP（断力矩，Esc）与 Resume torque

Session 目录：`data/sessions/<timestamp>_<kind>/`。

## 与现有 dashboard 的差别

`tools/robot_dashboard.py` 是旁路可视化：无 record 进程时相机为 No signal。本服务持有硬件，空闲循环一直在读。同一 COM 口不能被两个进程同时打开——跑 monitor 时不要再开 `lerobot-record`。
