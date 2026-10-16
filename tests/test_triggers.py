"""Trigger attribution: what woke a session (human prompt, peer message, Monitor event, ...).

    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import scan  # noqa: E402


def note(summary, event="x"):
    return ("<task-notification>\n<task-id>t1</task-id>\n<summary>%s</summary>\n<event>%s</event>\n"
            "</task-notification>" % (summary, event))


class NotificationTrigger(unittest.TestCase):
    def test_tagged_monitor(self):
        text = note('Monitor event: "MON:queue-watch queue27: progress"', "[MON:queue-watch] stage")
        self.assertEqual(scan.notification_trigger(text), "monitor:queue-watch")

    def test_tag_in_event_lines_only(self):
        self.assertEqual(scan.notification_trigger(note('Monitor event: "x"', "[MON:build] done")), "monitor:build")

    def test_untagged_monitor(self):
        self.assertEqual(scan.notification_trigger(note('Monitor event: "queue26 watch"')), "monitor:untagged")

    def test_monitor_stream_ended(self):
        self.assertEqual(scan.notification_trigger(note('Monitor "MON:a x" stream ended')), "monitor:a")

    def test_other_notifications(self):
        self.assertEqual(scan.notification_trigger(note('Agent "Dashboard" finished')), "agent")
        self.assertEqual(scan.notification_trigger(note('Background command "build" completed (exit code 0)')),
                         "background-command")
        self.assertEqual(scan.notification_trigger(note("something else")), "notification")

    def test_monitor_wins_in_a_batch(self):
        text = note('Agent "a" finished') + note('Monitor event: "MON:m d"')
        self.assertEqual(scan.notification_trigger(text), "monitor:m")


class TriggerEvents(unittest.TestCase):
    def read(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w", encoding="utf8") as handle:
                for r in records:
                    handle.write(json.dumps(r) + "\n")
            state = scan.FileState(path)
            state.update()
        return [(e[1], e[2]) for e in state.events if e[0] == "tg"]

    def test_idle_delivery_and_human(self):
        events = self.read([
            {"type": "user", "timestamp": "2026-10-12T10:00:00Z", "message": {"role": "user", "content": "hello"}},
            {"type": "user", "timestamp": "2026-10-12T10:05:00Z", "origin": {"kind": "task-notification"},
             "message": {"role": "user", "content": note('Monitor event: "MON:w d"')}},
        ])
        self.assertEqual([t for _, t in events], ["human", "monitor:w"])

    def test_queued_during_a_turn_wakes_at_the_next_record(self):
        queued = scan.parse_ts("2026-10-12T10:00:10Z")
        later = scan.parse_ts("2026-10-12T10:00:50Z")
        events = self.read([
            {"type": "attachment", "timestamp": "2026-10-12T10:00:10Z",
             "attachment": {"type": "queued_command", "prompt": note('Monitor event: "MON:w d"')}},
            {"type": "attachment", "timestamp": "2026-10-12T10:00:50Z", "attachment": {"type": "output_style"}},
        ])
        self.assertEqual(events, [(later, "monitor:w")])
        self.assertLess(queued, later)

    def test_meta_messages_are_not_triggers(self):
        events = self.read([
            {"type": "user", "timestamp": "2026-10-12T10:00:00Z", "isMeta": True,
             "message": {"role": "user", "content": "caveat"}},
        ])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
