from lerobot_monitor.metrics import dtw_distance, evaluate_action_chunk


def test_exact_action_chunk_scores_one_hundred() -> None:
    reference = [{"joint": 0.0}, {"joint": 1.0}, {"joint": 0.5}]
    result = evaluate_action_chunk(reference, reference)

    assert result is not None
    assert result["score"] == 100.0
    assert result["mae"] == 0.0
    assert result["rmse"] == 0.0
    assert result["dtw"] == 0.0
    assert result["coverage"] == 1.0


def test_action_chunk_reports_standard_trajectory_errors() -> None:
    predicted = [{"joint": 0.0}, {"joint": 1.0}]
    reference = [{"joint": 1.0}, {"joint": 3.0}]
    result = evaluate_action_chunk(predicted, reference)

    assert result is not None
    assert result["steps"] == 2
    assert result["predicted_steps"] == 2
    assert result["reference_steps"] == 2
    assert result["mae"] == 1.5
    assert result["rmse"] == 1.581139
    assert result["endpoint_error"] == 2.0
    assert result["dtw"] == 1.5
    assert result["score"] == 20.94


def test_action_chunk_coverage_uses_available_reference_steps() -> None:
    predicted = [{"joint": 0.0}, {"joint": 1.0}, {"joint": 2.0}]
    reference = [{"joint": 0.0}, {"joint": 1.0}]
    result = evaluate_action_chunk(predicted, reference)

    assert result is not None
    assert result["steps"] == 2
    assert result["coverage"] == 0.6667


def test_action_chunk_skips_missing_or_non_finite_joints() -> None:
    predicted = [{"good": 0.0, "missing": 1.0}, {"good": 1.0, "missing": 2.0}]
    reference = [{"good": 1.0}, {"good": 1.0}]
    result = evaluate_action_chunk(predicted, reference)

    assert result is not None
    assert list(result["joints"]) == ["good"]
    assert result["mae"] == 0.5


def test_dtw_aligns_temporally_shifted_trajectories() -> None:
    assert dtw_distance([0.0, 1.0, 2.0], [0.0, 0.0, 1.0, 2.0]) == 0.0
