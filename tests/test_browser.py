"""Behavior tests for the shipped browser controller, without installing JS packages."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_submission_survives_lost_response_without_duplicate_tasks():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser controller tests")
    result = subprocess.run(
        [node, "tests/browser/submission.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_preview_does_not_misattribute_delayed_frames_to_another_task():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser controller tests")
    result = subprocess.run(
        [node, "tests/browser/video.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
