from lerobot_monitor.types import JOINT_ORDER, lerp_pose, merge_partial


def test_merge_partial_keeps_unspecified_joints() -> None:
    current = {name: 10.0 for name in JOINT_ORDER}
    merged = merge_partial(current, {"gripper": 40.0})
    assert merged["gripper"] == 40.0
    assert merged["shoulder_pan"] == 10.0


def test_merge_clamps_gripper() -> None:
    current = {name: 0.0 for name in JOINT_ORDER}
    merged = merge_partial(current, {"gripper": 140.0})
    assert merged["gripper"] == 100.0


def test_lerp_endpoints() -> None:
    start = {name: 0.0 for name in JOINT_ORDER}
    goal = {name: 10.0 for name in JOINT_ORDER}
    assert lerp_pose(start, goal, 0.0)["elbow_flex"] == 0.0
    assert lerp_pose(start, goal, 1.0)["elbow_flex"] == 10.0
    assert lerp_pose(start, goal, 0.5)["elbow_flex"] == 5.0
