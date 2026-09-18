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

## 技术选型

- FastAPI + uvicorn：REST、WebSocket、MJPEG
- OpenCV：相机与视频落盘
- 可选依赖 sibling `lerobot`：SO-101 总线、leader、策略推理
