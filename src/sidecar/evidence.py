"""Export a local task snapshot for demo review; never infer task success."""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from sidecar.jobs import JobQueue
from sidecar.store import Store


def export_evidence(home: Path, task_id: str, output: Path) -> dict:
    if not task_id.isalnum():
        raise ValueError("invalid task id")
    home, output = Path(home), Path(output)
    if output.exists():
        raise ValueError("导出目录已存在，请使用新目录，以免覆盖已有证据")
    job = None
    if (home / "queue.sqlite").exists():
        queue = JobQueue(home)
        try:
            job = queue.get(task_id)
        except KeyError:
            pass
    database_home = home / "engine" if job else home
    state = job.get("state") if job else None
    events = []
    if (database_home / "tasks.sqlite").exists():
        store = Store(database_home)
        try:
            if state is None and not job:
                state = store.get(task_id)
            if state:
                events = store.events(task_id)
        finally:
            store.close()
    if not job and state is None:
        raise ValueError("任务不存在")
    if job:
        mode = "android" if queue.settings().mode == "android" else "offline_demo"
        # Persisted device identity survives later queue configuration changes.
        device_id = (state or {}).get("device_id", "")
        if device_id.startswith("offline-demo:"):
            mode = "offline_demo"
        elif device_id.startswith("android:"):
            mode = "android"
    else:
        metadata = home / "runs" / f"{task_id}.json"
        mode = (
            json.loads(metadata.read_text()).get("mode", "unknown")
            if metadata.exists()
            else "unknown"
        )
    state = state or {}
    observation = dict(state.get("observation") or {})
    image = observation.pop("image", None)
    screenshot, image_bytes = None, None
    if image:
        prefix, separator, data = image.partition(",")
        extension = {"data:image/png;base64": "png", "data:image/jpeg;base64": "jpg"}.get(prefix)
        if not separator or not extension:
            raise ValueError("观察图片格式无效，未导出")
        image_bytes = base64.b64decode(data, validate=True)
        screenshot = "observation." + extension
    report = {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "mode": mode,
        "status": job["status"] if job else state["status"],
        "reason": job["reason"] if job else state.get("reason", ""),
        "engine_status": state.get("status"),
        "task": state.get("task") or (job or {}).get("request"),
        "verification": state.get("verification"),
        "observation": observation or None,
        "screenshot": screenshot,
        "notice": "任务快照与最近观察，不是全程录屏；模拟任务不代表真实 App 结果。",
    }
    output.mkdir(parents=True, mode=0o700)
    for name, value in (("report.json", report), ("events.json", events)):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if screenshot:
        (output / screenshot).write_bytes(image_bytes)
    return {
        "task_id": task_id,
        "status": report["status"],
        "mode": mode,
        "output": str(output.resolve()),
    }
