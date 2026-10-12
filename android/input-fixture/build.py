"""Create the disposable acceptance APK; never contains user accounts or network permissions."""

import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

sdk = Path(os.environ["ANDROID_HOME"])
tools = sdk / "build-tools/35.0.0"
platform = sdk / "platforms/android-35/android.jar"
source = Path(__file__).resolve().parent
output = source.parents[1] / ".sidecar/acceptance/input-fixture.apk"
output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    work = Path(temp)

    def run(args):
        subprocess.run(list(map(str, args)), check=True, stdout=subprocess.DEVNULL)

    run(
        [
            "javac",
            "-source",
            "8",
            "-target",
            "8",
            "-classpath",
            platform,
            "-d",
            work,
            *source.glob("*.java"),
        ]
    )
    run([tools / "d8", "--min-api", "30", "--output", work, *work.rglob("*.class")])
    run(
        [
            tools / "aapt2",
            "link",
            "-I",
            platform,
            "--manifest",
            source / "AndroidManifest.xml",
            "-o",
            work / "base.apk",
        ]
    )
    with zipfile.ZipFile(work / "base.apk", "a") as archive:
        archive.write(work / "classes.dex", "classes.dex")
    run([tools / "zipalign", "-p", "4", work / "base.apk", work / "aligned.apk"])
    run(
        [
            "keytool",
            "-genkeypair",
            "-keystore",
            work / "debug.jks",
            "-storepass",
            "android",
            "-keypass",
            "android",
            "-alias",
            "debug",
            "-dname",
            "CN=Sidecar Test",
            "-keyalg",
            "RSA",
            "-validity",
            "365",
        ]
    )
    run(
        [
            tools / "apksigner",
            "sign",
            "--ks",
            work / "debug.jks",
            "--ks-pass",
            "pass:android",
            "--out",
            output,
            work / "aligned.apk",
        ]
    )
print(output)
