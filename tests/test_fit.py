"""The weekly-allowance fit: its uncertainty and the per-model-family fit (synthetic requests and readings only).

    python -m unittest discover -s tests
"""

import random
import types
import unittest
from unittest import mock

import isolate  # noqa: F401  (before devtime: a throw-away home)
from devtime import fit, overview

H = 3600
WEEK = overview.WEEK
END = 1_800_000_000.0 - (1_800_000_000.0 % WEEK) + WEEK  # a weekly reset instant
MODELS = ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"]


class Family(unittest.TestCase):
    def test_family(self):
        self.assertEqual([fit.family(m) for m in ("claude-opus-4-5", "claude-sonnet-4-5", "claude-3-haiku", "gpt", None)],
                         ["opus", "sonnet", "haiku", "other", "other"])


class Regress(unittest.TestCase):
    def test_exact_line_has_no_error(self):
        pts = [(0.1 + 0.001 * x, (float(x),)) for x in (0, 10, 25, 40, 70)]
        r = fit.regress([pts], 1)
        self.assertAlmostEqual(r["coef"][0], 0.001, places=9)
        self.assertLess(r["sigma"], 1e-9)

    def test_separate_intercepts_per_group(self):
        a = [(0.5 + 0.002 * x, (float(x),)) for x in (0, 5, 9, 20)]
        b = [(0.0 + 0.002 * x, (float(x),)) for x in (1, 7, 12, 30)]
        self.assertAlmostEqual(fit.regress([a, b], 1)["coef"][0], 0.002, places=9)

    def test_collinear_or_too_few_points(self):
        pts = [(0.01 * x, (float(x), 2.0 * x)) for x in range(6)]
        self.assertIsNone(fit.regress([pts], 2))
        self.assertIsNone(fit.regress([[(0.0, (0.0,)), (1.0, (1.0,))]], 1))

    def test_by_family_rejects_negative_coefficient(self):
        rng = random.Random(1)
        g, o, s = [], 0.0, 0.0
        for _ in range(14):
            o += rng.uniform(5, 40)
            s += rng.uniform(5, 40)
            g.append((0.001 * o - 0.0005 * s, {"opus": o, "sonnet": s, "haiku": 0.0, "other": 0.0}))
        self.assertIsNone(fit.by_family([g]))


def synthetic(opus_allowance, sonnet_allowance, seed=3, sonnet_scale=1.0, noise=0.0008, hours=100, now_hours=100):
    """Requests every 20 minutes (alternating families, random costs) and a reading every 2 hours of a week that started
    `hours` before the last reading; the reported share is weighted per family."""
    rng = random.Random(seed)
    start = END - WEEK
    rows, cost_o, cost_s, samples = [], 0.0, 0.0, []
    for step in range(hours * 3):
        t = start + step * 1200.0
        opus = step % 3 != 2
        c = rng.uniform(0.5, 6.0) * (1.0 if opus else sonnet_scale)
        rows.append((t, c, 0, 1000, 0, 0 if opus else 1, 0))
        cost_o += c if opus else 0.0
        cost_s += 0.0 if opus else c
        if step % 6 == 5:
            share = 0.04 + cost_o / opus_allowance + cost_s / sonnet_allowance + rng.gauss(0, noise)
            samples.append((t + 60, 0.1, share, t + 4 * H, END, "o"))
    idx = types.SimpleNamespace(reqs={0: rows}, starts={0: [r[0] for r in rows]})
    data = {"models": MODELS}
    return data, idx, samples, start + now_hours * H


class Limits(unittest.TestCase):
    def setUp(self):
        for name, value in (("large_work", []), ("changes", [])):
            patcher = mock.patch.object(overview, name, lambda *a, **k: value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def limits(self, *args, **kw):
        data, idx, samples, now = synthetic(*args, **kw)
        return overview.limits(data, idx, {}, now, samples=samples)

    def test_allowance_uncertainty_is_reported(self):
        plan = self.limits(3000.0, 3000.0)
        self.assertAlmostEqual(plan["allowance"], 3000, delta=150)
        self.assertEqual(plan["allowance"], plan["budget"])
        self.assertGreater(plan["allowanceError"], 0)
        self.assertLess(plan["allowanceError"], 200)
        self.assertLess(plan["fit"]["sigma"], 0.003)
        by = plan["fit"]["byFamily"]  # equal weights: both families come out near the same allowance
        self.assertAlmostEqual(by["opus"]["allowance"], 3000, delta=500)
        self.assertAlmostEqual(by["sonnet"]["allowance"], 3000, delta=800)

    def test_families_with_different_weights_are_separated(self):
        plan = self.limits(4000.0, 1500.0)
        by = plan["fit"]["byFamily"]
        self.assertEqual(sorted(by), ["opus", "sonnet"])
        self.assertAlmostEqual(by["opus"]["allowance"], 4000, delta=600)
        self.assertAlmostEqual(by["sonnet"]["allowance"], 1500, delta=300)
        self.assertGreater(by["sonnet"]["error"], 0)
        self.assertGreaterEqual(by["sonnet"]["share"], 0.1)
        # the displayed allowance is still the single fit
        self.assertTrue(1500 < plan["allowance"] < 4000)

    def test_a_family_below_ten_percent_means_no_family_fit(self):
        plan = self.limits(3000.0, 3000.0, sonnet_scale=0.02)
        self.assertNotIn("byFamily", plan["fit"])

    def test_too_few_samples_means_no_family_fit(self):
        plan = self.limits(4000.0, 1500.0, hours=14, now_hours=14)  # 7 readings
        self.assertEqual(plan["fit"]["samples"], 7)
        self.assertNotIn("byFamily", plan["fit"])
        self.assertIn("allowanceError", plan["fit"])


if __name__ == "__main__":
    unittest.main()
