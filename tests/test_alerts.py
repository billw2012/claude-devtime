"""alerts.check: the read-only-on-a-big-model alert.

    python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import alerts  # noqa: E402

KINDS = ["read/search code", "edit code", "model"]
NOW = 1_800_000_000.0


class Config:
    settings = {}


def dataset(agent_type, desc, minutes, edit_at=None, calls=40):
    """One live subagent on Sonnet making `calls` reads spread over `minutes` (an edit at minute `edit_at`)."""
    iv = {k: [] for k in ("t", "s", "e", "k", "l")}
    tok = {k: [] for k in ("i", "m", "in", "out", "cr", "cw5", "cw1", "th", "ms", "cost")}
    start = NOW - minutes * 60
    for n in range(calls):
        s = start + n * minutes * 60 / max(calls - 1, 1)
        kind = 1 if edit_at is not None and n == int(edit_at * (calls - 1) / minutes) else 0
        iv["t"].append(0); iv["s"].append(s); iv["e"].append(s + 1); iv["k"].append(kind); iv["l"].append(0)
        for key, value in (("i", len(iv["t"]) - 1), ("m", 0), ("in", 10), ("out", 10), ("cr", 1000), ("cw5", 0),
                           ("cw1", 0), ("th", 0), ("ms", 0), ("cost", 0.05)):
            tok[key].append(value)
    tr = {"idx": 0, "id": "agent-1", "role": "subagent", "session": "s", "org": "o", "project": "p", "title": "",
          "description": desc, "agentType": agent_type, "start": start, "end": NOW}
    return {"iv": iv, "tok": tok, "transcripts": [tr], "models": ["claude-sonnet-5-5"], "kinds": KINDS,
            "labels": [""], "org": "o"}


def readonly(data):
    return [a for a in alerts.check(data, Config(), NOW) if a["kind"] == "read-only on a big model"]


class ReadOnlyAlert(unittest.TestCase):
    def test_coder_reading_first_is_not_flagged(self):
        self.assertEqual(readonly(dataset("coder", "SP1: track splay core", 4)), [])
        self.assertEqual(readonly(dataset("devtime:coder", "SP1: track splay core", 30)), [])

    def test_fresh_reader_on_big_model_waits(self):
        self.assertEqual(readonly(dataset("general-purpose", "look around the track code", 5)), [])

    def test_long_read_only_agent_is_flagged(self):
        self.assertEqual(len(readonly(dataset("general-purpose", "look around the track code", 20))), 1)

    def test_editing_agent_is_not_flagged(self):
        self.assertEqual(readonly(dataset("general-purpose", "look around the track code", 20, edit_at=10)), [])


if __name__ == "__main__":
    unittest.main()
