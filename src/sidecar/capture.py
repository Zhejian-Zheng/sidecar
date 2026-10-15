"""Capture display-scoped XML and PNG for offline verifier development.

No model call or App input; connecting starts the UiAutomator service.
The XML retains the device-reported tree;
primary-display nodes are excluded, never reconstructed from Observation text.
"""

import hashlib
import io
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sidecar import displays
from sidecar.android_rpc import connect_device


def capture(serial, binding, output, label):
    """Capture a stable scoped hierarchy around a screenshot, or fail without a fixture."""
    output = Path(output)
    if output.exists():
        raise FileExistsError("Choose a new sample directory")
    displays.verify(serial, binding)
    foreground = displays.current(serial, binding.id)
    device = connect_device(serial)
    try:
        before = displays.filter_hierarchy(device.dump_hierarchy(), binding.id)
        image = displays.screenshot(serial, binding)
        after = displays.filter_hierarchy(device.dump_hierarchy(), binding.id)
        if before != after or foreground != displays.current(serial, binding.id):
            raise RuntimeError("Page changed during capture; no sample saved")
        info = displays.verify(serial, binding)
    finally:
        with suppress(Exception):
            device.stop_uiautomator()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    xml, png = after.encode(), buffer.getvalue()
    package_info = displays.shell(serial, ["dumpsys", "package", foreground[0]])

    version = re.search(r"versionName=([^\s]+)", package_info)
    code = re.search(r"versionCode=(\d+)", package_info)
    time_format = displays.shell(serial, ["settings", "get", "system", "time_12_24"])
    meta = {
        "schema_version": 1,
        "id": uuid4().hex,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "source": "device_hierarchy_filtered_to_bound_display",
        "package": foreground[0],
        "activity": foreground[1],
        "version_name": version[1] if version else None,
        "version_code": code[1] if code else None,
        "display": binding.model_dump(),
        "viewport": [info["width"], info["height"]],
        "sdk": displays.shell(serial, ["getprop", "ro.build.version.sdk"]),
        "locale": displays.shell(serial, ["getprop", "persist.sys.locale"]) or None,
        "time_12_24": time_format if time_format in {"12", "24"} else None,
        "hierarchy_stable_around_screenshot": True,
        "xml_sha256": hashlib.sha256(xml).hexdigest(),
        "image_sha256": hashlib.sha256(png).hexdigest(),
        "limitations": [
            "Hierarchy equality does not prove absence of overlays or offscreen rows",
            "Label is collector-supplied, not a verifier verdict",
        ],
    }
    output.mkdir(parents=True, mode=0o700)
    (output / "hierarchy.xml").write_bytes(xml)
    (output / "screen.png").write_bytes(png)
    (output / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta
