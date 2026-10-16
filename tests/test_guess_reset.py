import unittest

import isolate  # noqa: F401  (before devtime: a throw-away home)
from devtime import overview

H, WEEK = 3600, overview.WEEK


class GuessReset(unittest.TestCase):
    def test_most_drops_agree_and_one_off_ignored(self):
        reset = 1_000_000 * 1.0  # a reset instant; weeks repeat from it
        s = []
        for w, (before, after) in enumerate([(2, 7), (1.5, 0.5), (3, 30)]):  # hours before / after each reset
            r = reset + w * WEEK
            s += [(r - before * H, 0, 0.8), (r + after * H, 0, 0.0)]
        s += [(reset + 3 * WEEK - 2 * 86400, 0, 0.7), (reset + 3 * WEEK - 86400, 0, 0.1)]  # a one-off early reset
        g = overview.guess_reset(s)
        self.assertLessEqual((g - reset) % WEEK, 0.5 * H + 1)  # within the narrowest agreeing interval

    def test_no_drop(self):
        self.assertIsNone(overview.guess_reset([(0, 0, 0.1), (H, 0, 0.2)]))


if __name__ == "__main__":
    unittest.main()
