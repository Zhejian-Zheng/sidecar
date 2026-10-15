"""Trusted, packaged policy; external retrieval is never promoted to instructions."""

import hashlib
from pathlib import Path

POLICY = (Path(__file__).parent / "assets" / "verification-policy.md").read_text(encoding="utf-8")
POLICY_SHA256 = hashlib.sha256(POLICY.encode()).hexdigest()
