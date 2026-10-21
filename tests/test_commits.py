"""Commit / submit detection (overview.COMMIT) and the "source control" kind of the shipped rules.

    python -m unittest tests.test_commits
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import config, overview  # noqa: E402


def counted(label):
    return bool(overview.COMMIT.search(label)) and not overview.NOT_COMMIT.search(label)


class Commits(unittest.TestCase):
    def test_counted(self):
        for label in ("git commit -m 'x'", "git -C D:/proj commit -m x", "git add -A && git commit -m x",
                      "p4 submit -d 'x'", "p4 -c my_ws submit -d x", "p4.exe submit -c 123", "p4 -q submit -f",
                      "p4 -p host:1666 -u bob -c ws submit -d x", "svn commit -m x", "svn ci -m x", "hg commit -m x"):
            self.assertTrue(counted(label), label)

    def test_not_counted(self):
        for label in ("git log --oneline", "git status", "git diff", "p4 changes -m 5", "p4 opened", "p4 edit foo.cpp",
                      "p4 describe 123", "svn status", "hg log", "echo submit"):
            self.assertFalse(counted(label), label)


class Kind(unittest.TestCase):
    def test_source_control_rule(self):
        cfg = config.Config()
        for cmd in ("git status", "gh pr list", "p4 submit -d x", "p4 -c ws edit a.cpp", "p4.exe sync", "svn update",
                    "hg pull"):
            self.assertEqual(cfg.classify(None, "Bash", cmd), "source control", cmd)
        self.assertNotEqual(cfg.classify(None, "Bash", "ls -la"), "source control")


if __name__ == "__main__":
    unittest.main()
