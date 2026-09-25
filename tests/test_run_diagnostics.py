"""Run records are written asynchronously and retain rate segments."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from lerobot_monitor.run_diagnostics import RunDiagnostics


def test_run_summary_events_and_trace_exports(tmp_path: Path) -> None:
    run = RunDiagnostics(tmp_path, "playback", {"effective_hz": 30, "source": {"episode": 2}}, trace=True)
    run.tick({"sent": "a", "feedback": "b"})
    run.event("missed_output_slots", {"count": 1})
    run.change_rate(60, 3)
    run.tick({"sent": "c", "feedback": "d"})
    run.finish({"sent": 5}, 5)
    run._thread.join(timeout=5)
    assert not run._thread.is_alive()
    summary = json.loads((run.root / "summary.json").read_text(encoding="utf-8"))
    assert [segment["target_hz"] for segment in summary["segments"]] == [30, 60]
    assert [segment["sent"] for segment in summary["segments"]] == [3, 2]
    events = [json.loads(line) for line in (run.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == ["missed_output_slots", "rate_changed"]
    with (run.root / "ticks.csv").open(encoding="utf-8", newline="") as stream:
        ticks = list(csv.DictReader(stream))
    assert [tick["sent"] for tick in ticks] == ["a", "c"]
