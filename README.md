# LeRobot Monitor

独立于 `lerobot-record` / `lerobot-teleoperate` / `lerobot-rollout` 的机械臂监控与控制网页。

空闲时不占用 COM 口。相机由本进程探测（不再依赖 `win_cam_server`）；在 Cameras 菜单里改分辨率/端口并打开网络流后，主界面才显示该路画面。

## 运行

不要同时再开 `robot_tools/win_cam_server.py`（同样占用 8090）。机械臂/策略需要 sibling 的 `lerobot` 环境：

```powershell
cd d:\repos\lerobot\lerobot-monitor
.\run.ps1
```

浏览器：`http://127.0.0.1:8090/lerobot/`

无硬件时：`uv sync --extra dev; uv run lerobot-monitor`。

## 界面

- Cameras：列出本机设备，设置宽高、网络端口，开关网络流。仅 `stream on` 的相机出现在主网格。网络流默认 `http://127.0.0.1:5000/`（index 0 → 5000）
- 关节滑条：第一次拖动才申请 COM6，跟手实时调节；停止约 2s 后释放串口
- 遥操作 / 录制 / 部署 rollout（可选把视频+关节写成 session）
- E-STOP（Esc）释放力矩并交还串口

Session 目录：`data/sessions/<timestamp>_<kind>/`。
