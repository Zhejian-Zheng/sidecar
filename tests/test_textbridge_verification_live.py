"""Opt-in Android node tests; no UI automation connection or user-app changes."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    not os.environ.get("SIDECAR_BRIDGE_TEST_SERIAL"), reason="requires Android runtime and SDK"
)
def test_textbridge_verifies_only_unique_original_target(tmp_path):
    sdk = Path(os.environ["ANDROID_HOME"])
    serial = os.environ["SIDECAR_BRIDGE_TEST_SERIAL"]
    source = Path(__file__).resolve().parents[1] / "android/textbridge"

    def run(args):
        result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run(
        [
            "javac",
            "-source",
            "8",
            "-target",
            "8",
            "-classpath",
            sdk / "platforms/android-35/android.jar",
            "-d",
            tmp_path,
            source / "Main.java",
            source / "VerificationTest.java",
        ]
    )
    artifact = tmp_path / "verification.jar"
    run(
        [
            sdk / "build-tools/35.0.0/d8",
            "--min-api",
            "30",
            "--output",
            artifact,
            *tmp_path.rglob("*.class"),
        ]
    )
    remote = f"/data/local/tmp/sidecar-verification-{uuid4().hex}.jar"
    adb = [sdk / "platform-tools/adb", "-s", serial]
    try:
        run([*adb, "push", artifact, remote])
        output = run(
            [
                *adb,
                "shell",
                f"CLASSPATH={remote}",
                "app_process",
                "/",
                "dev.sidecar.textbridge.VerificationTest",
            ]
        )
        assert "6 bridge verification checks passed" in output
    finally:
        run([*adb, "shell", "rm", "-f", remote])
