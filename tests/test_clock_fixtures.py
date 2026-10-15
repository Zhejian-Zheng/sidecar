"""Protect real evidence from accidental corruption and mislabeled switch state."""

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parent / "fixtures/google_clock/7.5"


@pytest.mark.parametrize("sample", sorted(ROOT.iterdir()), ids=lambda p: p.name)
def test_fixture_integrity(sample):
    meta = json.loads((sample / "metadata.json").read_text())
    for filename, key in [("hierarchy.xml", "xml_sha256"), ("screen.png", "image_sha256")]:
        assert hashlib.sha256((sample / filename).read_bytes()).hexdigest() == meta[key]
    root = ET.parse(sample / "hierarchy.xml")
    assert all(n.get("display-id") == str(meta["display"]["id"]) for n in root.iter("node"))


def switches(name):
    return [
        (n.get("content-desc"), n.get("checked"))
        for n in ET.parse(ROOT / name / "hierarchy.xml").iter("node")
        if n.get("resource-id") == "com.google.android.deskclock:id/onoff"
    ]


def test_original_alarm_states_restored():
    expected = [("8:00 AM alarm", "false"), ("11:58 PM alarm", "false")]
    assert switches("list-disabled") == expected
    assert switches("restored-disabled") == expected
    assert switches("target-enabled") == [("8:00 AM alarm", "false"), ("11:58 PM alarm", "true")]
    assert switches("expanded-enabled") == [("11:58 PM alarm", "true")]
    assert switches("time-picker") == []
    assert switches("label-dialog") == []
