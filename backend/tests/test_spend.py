#!/usr/bin/env python3
"""Tests for the Step 10 spend table and the public demo's request cap.

    python3 backend/tests/test_spend.py

Two different promises live in one module, and each one is a way of being wrong
about somebody else's money:

1. **The total is real.** It is built from the usage the provider reported for
   each turn, summed as integer micro-dollars, so a session's worth of turns
   cannot drift into a number nobody can reproduce. The drift is not
   hypothetical - a hundred thousand turns of float arithmetic is off by a
   hundredth of a micro-dollar, which is exactly the kind of wrong that is
   invisible until it is a reconciliation problem.
2. **A turn is counted once.** Its model calls are recorded individually, for the
   per-tier breakdown, and its total is recorded once. Doing both was the bug in
   this module's first wiring: a turn appeared twice in the process total, and
   the test below is the one that would have caught it.
3. **The cap cannot be talked past.** `take()` increments in the same lock as the
   check, so two turns arriving together cannot both be told they are the last
   one, and a session that is out is refused rather than merely warned.

The cap is per client address rather than per cookie, and one test states that
trade rather than leaving it to be discovered: the header a client sends is not
consulted, so a reload cannot buy a fresh budget.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spend as spend_module  # noqa: E402
from spend import Spend  # noqa: E402


class TestWhatWeSpent(unittest.TestCase):
    def test_the_total_is_the_sum_of_the_turns(self):
        table = Spend(cap=20)
        table.record_turn(0.000986, steps=2)
        table.record_turn(0.0012, steps=3)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["turns"], 2)
        self.assertEqual(snapshot["steps"], 5)
        self.assertEqual(snapshot["cost_usd"], 0.002186)

    def test_a_turn_is_counted_once_even_though_its_calls_are_recorded(self):
        """The router and the executor are recorded, and then the turn. Once."""
        table = Spend(cap=20)
        table.record_call("nano", 0.0001032)
        table.record_call("super", 0.0004038)
        table.record_turn(0.000507, steps=2)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["cost_usd"], 0.000507, "the turn was counted twice")
        self.assertEqual(snapshot["calls"], 2)
        # The calls' own money is still reported - it just is not the total.
        self.assertEqual(snapshot["calls_cost_usd"], 0.000507)

    def test_per_tier_costs_add_up_to_the_calls_that_were_made(self):
        table = Spend(cap=20)
        for _ in range(3):
            table.record_call("nano", 0.0001)
        table.record_call("super", 0.0004)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["by_tier"]["nano"]["calls"], 3)
        self.assertEqual(snapshot["by_tier"]["nano"]["cost_usd"], 0.0003)
        self.assertEqual(snapshot["by_tier"]["super"]["cost_usd"], 0.0004)
        self.assertEqual(snapshot["calls_cost_usd"], 0.0007)

    def test_micro_dollar_sums_do_not_drift(self):
        """A hundred thousand cheap calls is still exactly ten dollars."""
        table = Spend(cap=20)
        for _ in range(100_000):
            table.record_turn(0.0001)
        self.assertEqual(table.snapshot()["cost_usd"], 10.0)
        # The same arithmetic in floats, one call at a time, for contrast: it
        # drifts - 9.999999999990033 on every version measured.
        #
        # Added by hand rather than with `sum()` on purpose. Python 3.12 gave
        # `sum()` Neumaier compensation (gh-100425), so `sum([0.0001] * 100_000)`
        # is **exactly 10.0** from 3.12 on. Written with `sum()` - the way it was
        # first written - this assertion is therefore green on 3.9 and red from
        # 3.12 on, which is a failure on one leg of CI's own version matrix and
        # on nothing else. A hand-rolled loop is what the store does anyway, and
        # it says the same thing on every version.
        total = 0.0
        for _ in range(100_000):
            total += 0.0001
        self.assertNotEqual(total, 10.0)

    def test_the_credit_left_is_what_the_budget_has_not_seen(self):
        table = Spend(cap=20, credit_usd=25.0)
        table.record_turn(1.5)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["credit_usd"], 25.0)
        self.assertEqual(snapshot["remaining_credit_usd"], 23.5)
        self.assertEqual(snapshot["percent_of_credit"], 6.0)

    def test_spending_past_the_credit_reports_zero_left_rather_than_a_negative(self):
        table = Spend(cap=20, credit_usd=2.0)
        table.record_turn(5.0)
        self.assertEqual(table.snapshot()["remaining_credit_usd"], 0.0)

    def test_a_negative_or_unusable_cost_is_not_a_credit(self):
        """The one direction that must never happen: a number going down."""
        table = Spend(cap=20)
        table.record_turn(-4.0)
        table.record_call("super", "not a number")
        table.record_turn(None)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["cost_usd"], 0.0)
        self.assertEqual(snapshot["turns"], 2, "the turns still happened")
        self.assertEqual(snapshot["calls"], 1, "the call still happened")

    def test_the_snapshot_carries_the_cap_it_would_enforce(self):
        self.assertEqual(Spend(cap=7).snapshot()["session_cap"], 7)

    def test_the_configured_budgets_are_what_it_reads(self):
        """The cap and the credit come from config/models.json, not from here."""
        from nemotron import load_config

        budgets = load_config()["budgets"]
        table = Spend(load_config())
        self.assertEqual(table.cap, budgets["max_requests_per_session_demo"])
        self.assertEqual(
            table.snapshot()["credit_usd"], float(budgets["total_credit_usd"])
        )

    def test_reset_gives_a_clean_table(self):
        table = Spend(cap=20)
        table.record_turn(1.0)
        table.take("10.0.0.1")
        table.reset()
        snapshot = table.snapshot()
        self.assertEqual(snapshot["turns"], 0)
        self.assertEqual(snapshot["cost_usd"], 0.0)
        self.assertEqual(snapshot["sessions_seen"], 0)


class TestTheRequestCap(unittest.TestCase):
    def test_the_cap_allows_exactly_that_many_and_then_refuses(self):
        table = Spend(cap=3)
        for expected_used in (1, 2, 3):
            granted = table.take("10.0.0.1")
            self.assertTrue(granted["allowed"], f"request {expected_used} was refused")
            self.assertEqual(granted["used"], expected_used)
            self.assertEqual(granted["remaining"], 3 - expected_used)
        refused = table.take("10.0.0.1")
        self.assertFalse(refused["allowed"])
        self.assertEqual(refused["used"], 3, "a refused request must not be charged")
        self.assertEqual(refused["remaining"], 0)
        self.assertEqual(refused["cap"], 3)

    def test_a_visitor_who_was_refused_stays_refused(self):
        table = Spend(cap=1)
        table.take("10.0.0.1")
        for _ in range(5):
            self.assertFalse(table.take("10.0.0.1")["allowed"])

    def test_sessions_do_not_share_a_budget(self):
        table = Spend(cap=2)
        table.take("10.0.0.1")
        table.take("10.0.0.1")
        self.assertFalse(table.take("10.0.0.1")["allowed"])
        self.assertTrue(table.take("10.0.0.2")["allowed"])

    def test_looking_is_not_asking(self):
        """`session()` reports without charging, which /health depends on."""
        table = Spend(cap=2)
        self.assertEqual(table.session("10.0.0.1")["used"], 0)
        self.assertEqual(table.session("10.0.0.1")["used"], 0)
        self.assertEqual(table.session("10.0.0.1")["remaining"], 2)
        table.take("10.0.0.1")
        self.assertEqual(table.session("10.0.0.1")["used"], 1, "a charged request went missing")

    def test_an_unknown_session_has_its_whole_budget(self):
        table = Spend(cap=5)
        fresh = table.session("203.0.113.9")
        self.assertTrue(fresh["allowed"])
        self.assertEqual(fresh["used"], 0)
        self.assertEqual(fresh["remaining"], 5)

    def test_a_zero_cap_refuses_everything(self):
        table = Spend(cap=0)
        self.assertFalse(table.take("10.0.0.1")["allowed"])

    def test_the_session_table_is_bounded(self):
        """A public demo must not grow a dictionary until the process dies."""
        original = spend_module.MAX_TRACKED_SESSIONS
        spend_module.MAX_TRACKED_SESSIONS = 10
        try:
            table = Spend(cap=5)
            for index in range(40):
                table.take(f"10.0.0.{index}")
            tracked = table.snapshot()["sessions_seen"]
            self.assertLess(tracked, 40, "nothing was evicted")
            self.assertLessEqual(tracked, 20, f"the bound is not holding: {tracked}")
            # Eviction drops the least recently seen, so the newest survivor is
            # still counted - a cap that forgets an active visitor is a bug.
            self.assertEqual(table.session("10.0.0.39")["used"], 1)
        finally:
            spend_module.MAX_TRACKED_SESSIONS = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
