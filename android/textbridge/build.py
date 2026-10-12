"""Build the bundled bridge with JDK 17 and Android SDK platform/build-tools 35."""

import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

sdk = Path(os.environ["ANDROID_HOME"])
root = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as temp:
    work = Path(temp)
    subprocess.run(
        [
            "javac",
            "-source",
            "8",
            "-target",
            "8",
            "-classpath",
            str(sdk / "platforms/android-35/android.jar"),
            "-d",
            temp,
            str(Path(__file__).with_name("Main.java")),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(sdk / "build-tools/35.0.0/d8"),
            "--min-api",
            "30",
            "--output",
            temp,
            *map(str, work.rglob("*.class")),
        ],
        check=True,
    )
    with zipfile.ZipFile(
        root / "src/sidecar/assets/textbridge.jar", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        archive.write(work / "classes.dex", "classes.dex")
