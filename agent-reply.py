#!/usr/bin/env python3
"""Append a local-agent reply for the 3DS to read: agent-reply "text of reply" """
import json
import sys
import time
from pathlib import Path

path = Path(__file__).resolve().parent / "replies.jsonl"
text = " ".join(sys.argv[1:]).strip()
if not text:
    sys.exit("usage: agent-reply <text>")
ids = []
if path.exists():
    ids = [json.loads(l).get("id", 0) for l in path.read_text().splitlines() if l.strip()]
rid = (max(ids) + 1) if ids else 1
with open(path, "a") as f:
    f.write(json.dumps({"id": rid, "ts": time.time(), "text": text}) + "\n")
print(f"reply {rid} queued")
