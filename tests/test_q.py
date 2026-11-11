import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import q  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "turn.jsonl")


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


class Weight(unittest.TestCase):
    def test_weight_formula(self):
        u = {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 100000, "output_tokens": 200}
        self.assertAlmostEqual(q.weight(u), 2 + 1000 + 5 * 200 + 0.01 * 100000)


class Rate(unittest.TestCase):
    def test_table(self):
        self.assertEqual(q.rate("claude-opus-5-5"), 1.0)
        self.assertEqual(q.rate("claude-opus-4-8"), 1.25)
        self.assertEqual(q.rate("claude-sonnet-5-5"), 0.5)
        self.assertEqual(q.rate("claude-sonnet-4-6"), 0.75)
        self.assertEqual(q.rate("claude-haiku-5-5"), 0.025)
        self.assertEqual(q.rate("claude-haiku-4-5"), 0.25)
        self.assertEqual(q.rate("claude-fable-5-2"), 2.0)
        self.assertEqual(q.rate("claude-fable-5-1"), 2.5)

    def test_fast_doubles(self):
        self.assertEqual(q.rate("claude-opus-5-5", "fast"), 2.0)
        self.assertEqual(q.rate("claude-opus-5-5", "standard"), 1.0)

    def test_tail_is_taken_off(self):
        self.assertEqual(q.model_key("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(q.rate("claude-sonnet-5-5[1m]", "fast"), 1.0)

    def test_unknown_model(self):
        self.assertIsNone(q.rate("claude-opus-9"))
        self.assertIsNone(q.rate("<synthetic>"))
        self.assertIsNone(q.rate(None))

    def test_sum_usage_applies_r_and_counts_unrated(self):
        u = {"input_tokens": 1000, "output_tokens": 0}
        entries = [
            {"type": "assistant", "message": {"id": "a", "model": "claude-haiku-5-5", "usage": u}},
            {"type": "assistant", "message": {"id": "b", "model": "claude-opus-5-5", "usage": {**u, "speed": "fast"}}},
            {"type": "assistant", "message": {"id": "c", "model": "claude-new-1", "usage": u}},
            {"type": "assistant", "message": {"id": "d", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ]
        tot = q.sum_usage(entries)
        self.assertAlmostEqual(tot["w"], 1000 * 0.025 + 1000 * 2.0 + 1000 * 1.0)
        self.assertEqual(tot["unrated"], 1)  # claude-new-1; an empty response is not counted


class Label(unittest.TestCase):
    def test_ladder_and_inverse(self):
        self.assertEqual(q.label(82.1), "82.1µq · 12.2k/q")
        self.assertEqual(q.label(3200), "3.2mq · 312/q")
        self.assertEqual(q.label(140000), "140mq · 7.14/q")
        self.assertEqual(q.label(500000), "500mq · 2/q")
        self.assertEqual(q.label(2_000_000), "2q · 0.5/q")
        self.assertEqual(q.label(0), "0µq")

    def test_micro_of(self):
        self.assertAlmostEqual(q.micro_of(5.4e8, 5.4e8), 1_000_000)


class TurnSlicing(unittest.TestCase):
    def test_last_prompt_skips_tool_results_and_meta(self):
        entries = load(FIXTURE)
        self.assertEqual(q.last_prompt(entries), 2)

    def test_has_final(self):
        entries = load(FIXTURE)
        self.assertTrue(q.has_final(entries[3:]))
        self.assertFalse(q.has_final(entries[3:5]))  # only a tool call so far
        self.assertFalse(q.has_final(entries[1:3]))  # the previous turn's final, then a new prompt

    def test_turn_sum_dedupes_streamed_blocks(self):
        entries = load(FIXTURE)
        tot = q.sum_usage(entries[q.last_prompt(entries) + 1 :])
        self.assertEqual(tot["responses"], 2)
        self.assertEqual(tot["output"], 250)
        self.assertAlmostEqual(tot["w"], (2 + 1000 + 1000 + 1000) + (3 + 250 + 1010))

    def test_stop_counts_only_new_entries(self):
        with tempfile.TemporaryDirectory() as home:
            q.HOME = home
            q.WAIT_S = 0
            tp = os.path.join(home, "s.jsonl")
            with open(FIXTURE) as src, open(tp, "w") as dst:
                dst.write(src.read())
            first = q.turn({"session_id": "s", "transcript_path": tp})
            self.assertEqual(first["responses"], 2)  # since the last prompt only
            again = q.turn({"session_id": "s", "transcript_path": tp})
            self.assertEqual(again.get("responses", 0), 0)  # nothing new
            sub = os.path.join(home, "s", "subagents")
            os.makedirs(sub)
            with open(os.path.join(sub, "agent-a.jsonl"), "w") as f:
                f.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {"id": "x", "stop_reason": "end_turn", "usage": {"output_tokens": 10}}}) + "\n")
            third = q.turn({"session_id": "s", "transcript_path": tp})
            self.assertEqual(third["output"], 10)


class Calibrate(unittest.TestCase):
    def test_anchor_then_k(self):
        st, msg = q.calibrate({}, 30, 1e8, "t0")
        self.assertIn("anchored", msg)
        st, msg = q.calibrate(st, 39, 2e8, "t1")
        self.assertNotIn("k", st)  # Δ 9 points is too small (MIN_DELTA_PCT 10)
        st, msg = q.calibrate(st, 42, 1e8 + 6e7, "t2")
        self.assertAlmostEqual(st["k"], 6e7 / 0.12)
        self.assertEqual(st["basis"], "calibrated")
        self.assertEqual(st["anchor"]["pct"], 42)

    def test_anchor_from_before_r_is_replaced(self):
        # Its w was summed without r: a K from it would mix two measures.
        st, msg = q.calibrate({"anchor": {"pct": 10, "w": 1e8, "ts": "t"}}, 40, 3e8, "t1")
        self.assertNotIn("k", st)
        self.assertEqual(st["anchor"], {"pct": 40, "w": 3e8, "ts": "t1", "rated": True})

    def test_reset_reanchors(self):
        st, _ = q.calibrate({"anchor": {"pct": 80, "w": 5e8, "ts": "t"}}, 3, 6e8, "t1")
        self.assertEqual(st["anchor"]["pct"], 3)
        self.assertNotIn("k", st)


class LabelCommand(unittest.TestCase):
    def run_label(self, args, state=None):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            old = q.HOME
            q.HOME = d
            try:
                if state is not None:
                    q._save(os.path.join(d, "state.json"), state)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = q.main(["label", *args])
                return rc, buf.getvalue().strip()
            finally:
                q.HOME = old

    def test_seed_label_is_marked(self):
        # w = 1000 + 5*200 + 0.01*100000 = 3000; µq = 3000 / 5.4e8 * 1e6
        rc, out = self.run_label(["0", "1000", "100000", "200"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "q ~" + q.label(3000 / q.SEED_K * 1e6))

    def test_calibrated_label(self):
        rc, out = self.run_label(["1000", "0", "0", "0"], {"k": 1e8, "basis": "calibrated"})
        self.assertEqual(out, "q 10µq · 100k/q")

    def test_model_groups(self):
        # Sonnet 5.5 main turn + a Haiku 5.5 subagent: 1000·0.5 + 2000·0.025 = 550
        rc, out = self.run_label(["1000", "0", "0", "0", "claude-sonnet-5-5[1m]", "+", "2000", "0", "0", "0", "claude-haiku-5-5"],
                                 {"k": 1e8, "basis": "calibrated"})
        self.assertEqual(out, "q " + q.label(550 / 1e8 * 1e6))

    def test_unknown_model_counts_at_one(self):
        rc, out = self.run_label(["1000", "0", "0", "0", "claude-new-1"], {"k": 1e8, "basis": "calibrated"})
        self.assertEqual(out, "q 10µq · 100k/q")

    def test_bad_group(self):
        self.assertEqual(self.run_label(["1", "2", "3", "4", "m", "+", "5"])[0], 1)

    def test_nothing_for_zero(self):
        self.assertEqual(self.run_label(["0", "0", "0", "0"]), (0, ""))

    def test_bad_args(self):
        self.assertEqual(self.run_label(["x"])[0], 1)


if __name__ == "__main__":
    unittest.main()
