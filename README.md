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

- Cameras：列出本机设备，设置宽高、网络端口，开关网络流。Blender 注册表里
  `cameras[]` 公布的相机也会自动出现为远程 MJPEG 设备；远程相机的 URL、画幅
  与 FPS 由 Blender 面板控制，monitor 只负责启用、主视图和策略输入开关。
  仅启用的主视图相机出现在主网格。
- Joints 面板：Serial control 显式连接/断开 follower；同步源可选 Follower、Leader、
  Joint state、Command、Predict；Speed cap 统一限制 live 输出与 leader relay 的
  `°/s` 最大变化量。Serial control 开启且无控制任务时，面板直接发送当前 command，
  不再需要 Apply。回放 dataset/video 时，Joint state 与 Command 会跟随白色游标处的
  `obs.*` / `act.*` 插值；Serial control 开启时会把这些 pose 直接下发到 follower。
  关闭 Serial control 立即断开串口，Joints 面板操作不会退出 replay。同步源与
  Speed cap 不进入 pose preset。Sync 支持 None 和全部源手动编辑；编辑或加载 pose
  preset 会暂停对应关节的持续同步，来源接近或按下 Sync 按钮后恢复。Sync 右侧按钮
  也可立即把当前源数值填入 command；所有关节进入 cur 容差并全绿后自动恢复同步并点亮
  Sync。EXIT 固定在视频区域右上角。Serial control 右侧的 `send` 单次下发当前 command，`control` 灯点亮
  后持续下发，默认熄灭；control 关闭时 preset 只更新面板，不写串口。Sync 使用 `sync`
  单次拉取与 `auto` 持续跟随双控，所有源行为一致，None 不锁定 auto。send/sync 使用
  上传/下载图标表示相反方向，两个持续输出灯都显示 `auto`。
- 遥操作 / 录制 / 部署 rollout（可选把视频+关节写成 session）
- E-STOP（Esc）释放力矩并交还串口
- 右侧 Joints / Record / Rollout / Debug / Hardware 各自保存 preset；Hardware preset
  按设备身份恢复串口和相机，并可在 Monitor 服务重启后自动重连。

Session 目录：`data/sessions/<timestamp>_<kind>/`。
