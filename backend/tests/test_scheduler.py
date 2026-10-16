#!/usr/bin/env python3
"""Tests for the scheduler: date parsing, what is due, and the brief.

    python3 backend/tests/test_scheduler.py

Hermetic and clock-free: every function here takes the date it should believe it
is, so these run the same in 2026 and in 2036, and "the midterm is next week"
means something exact in a test instead of something different on each machine.

Four properties, each of which is a way a scheduled brief goes wrong:

1. **A date is only a date.** Ten forms of "November 12" are read, and "the
   12th" is not - with no month it is not a date, and guessing one is how a
   student ends up planning around a deadline we invented.
2. **A yearless date means the next time it comes round.** November 12 read in
   December is next November, not eleven months ago.
3. **The next date is the one that matters.** A sentence naming two days is due
   on the sooner one; a sentence where every day has passed is overdue by the
   most recent of them.
4. **The brief cannot say more than it knows.** Composited from the facts it was
   handed, it reports undated notes as undated, and reports its cost as nothing.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import (  # noqa: E402
    DEFAULT_HORIZON_DAYS,
    brief_due,
    compose_brief,
    deadlines,
    due_date,
    dates_in,
    headline,
    nudges,
)

TODAY = date(2026, 11, 1)
NOVEMBER_12 = date(2026, 11, 12)


def fact(text, fact_id="f1"):
    return {"id": fact_id, "text": text}


class ReadingDates(unittest.TestCase):
    def test_an_iso_date_is_read_exactly(self):
        self.assertEqual(dates_in("the report is due 2026-11-12", TODAY), [NOVEMBER_12])

    def test_a_month_name_and_a_day(self):
        for text in (
            "my midterm is on November 12",
            "my midterm is on Nov 12",
            "my midterm is on Nov. 12",
            "my midterm is on november 12th",
            "my midterm is on NOVEMBER 12",
        ):
            with self.subTest(text=text):
                self.assertEqual(dates_in(text, TODAY), [NOVEMBER_12])

    def test_day_first_works_too(self):
        for text in ("the midterm is on 12 November", "the midterm is on 12th Nov"):
            with self.subTest(text=text):
                self.assertEqual(dates_in(text, TODAY), [NOVEMBER_12])

    def test_a_bare_day_number_is_not_a_date(self):
        """With no month there is nothing to resolve, so we refuse to resolve it."""
        for text in ("it is due on the 12th", "due sometime around the 12th"):
            with self.subTest(text=text):
                self.assertEqual(dates_in(text, TODAY), [])

    def test_an_impossible_date_is_dropped_not_normalised(self):
        self.assertEqual(dates_in("due 2026-02-30 (typo)", TODAY), [])
        self.assertEqual(dates_in("due February 30", TODAY), [])

    def test_the_same_day_written_twice_is_listed_once(self):
        self.assertEqual(dates_in("due Nov 12, i.e. 2026-11-12", TODAY), [NOVEMBER_12])

    def test_several_dates_come_back_in_the_order_written(self):
        self.assertEqual(
            dates_in("labs end 2026-12-01, the report is due 2026-11-05", TODAY),
            [date(2026, 12, 1), date(2026, 11, 5)],
        )

    def test_a_word_that_only_looks_like_a_month_is_not_a_month(self):
        self.assertEqual(dates_in("due in Marching band season 42", TODAY), [])

    def test_a_yearless_date_in_the_past_rolls_forward(self):
        # "May 3rd" read on November 1st is next May, not seven months ago.
        self.assertEqual(dates_in("May 3rd", TODAY), [date(2027, 5, 3)])


class ResolvingTheYear(unittest.TestCase):
    def test_a_day_still_to_come_is_this_year(self):
        self.assertEqual(due_date("midterm November 12", TODAY), NOVEMBER_12)

    def test_a_day_that_has_passed_is_next_year(self):
        # Asked in December, a November date means the November ahead.
        december = date(2026, 12, 20)
        self.assertEqual(due_date("midterm November 12", december), date(2027, 11, 12))

    def test_today_counts_as_today_not_next_year(self):
        self.assertEqual(due_date("the quiz is on November 1", TODAY), TODAY)


class WhichDeadlineItIs(unittest.TestCase):
    def test_the_soonest_date_that_has_not_passed_wins(self):
        self.assertEqual(
            due_date("labs end 2026-12-01 but the report is due 2026-11-05", TODAY),
            date(2026, 11, 5),
        )

    def test_when_every_date_has_passed_the_most_recent_one_is_the_overdue_one(self):
        self.assertEqual(
            due_date("draft was due 2026-10-01, final was due 2026-10-20", TODAY),
            date(2026, 10, 20),
        )

    def test_no_date_at_all(self):
        self.assertEqual(due_date("my advisor is Dr. Chen", TODAY), None)


class SplittingFacts(unittest.TestCase):
    def test_dated_and_undated_facts_are_both_kept(self):
        dated, undated = deadlines(
            [
                fact("midterm is on November 12", "a"),
                fact("my advisor is Dr. Chen", "b"),
            ],
            TODAY,
        )
        self.assertEqual([d.text for d in dated], ["midterm is on November 12"])
        self.assertEqual([u["id"] for u in undated], ["b"])

    def test_days_left_is_relative_to_today(self):
        dated, _ = deadlines([fact("midterm is on November 12")], TODAY)
        self.assertEqual(dated[0].days_left, 11)
        self.assertEqual(dated[0].due, "2026-11-12")
        self.assertFalse(dated[0].overdue)

    def test_an_overdue_fact_has_a_negative_count(self):
        dated, _ = deadlines([fact("the draft was due 2026-10-25")], TODAY)
        self.assertEqual(dated[0].days_left, -7)
        self.assertTrue(dated[0].overdue)

    def test_the_most_overdue_is_first(self):
        dated, _ = deadlines(
            [
                fact("report due 2026-11-20", "a"),
                fact("draft due 2026-10-25", "b"),
                fact("quiz due 2026-11-01", "c"),
            ],
            TODAY,
        )
        self.assertEqual([d.id for d in dated], ["b", "c", "a"])
        self.assertEqual([d.days_left for d in dated], [-7, 0, 19])

    def test_a_blank_fact_is_skipped_entirely(self):
        dated, undated = deadlines([fact("   ", "a"), fact("", "b")], TODAY)
        self.assertEqual(dated, [])
        self.assertEqual(undated, [])

    def test_a_very_long_fact_is_trimmed_for_reading(self):
        dated, _ = deadlines([fact("midterm on November 12 " + "x" * 400)], TODAY)
        self.assertTrue(dated[0].text.endswith("…"))
        self.assertLessEqual(len(dated[0].text), 120)

    def test_internal_whitespace_is_collapsed(self):
        dated, _ = deadlines([fact("midterm\n is   on November 12")], TODAY)
        self.assertEqual(dated[0].text, "midterm is on November 12")


class TheNudges(unittest.TestCase):
    def setUp(self):
        self.dated, _ = deadlines(
            [
                fact("draft was due 2026-10-25", "a"),
                fact("quiz is on 2026-11-01", "b"),
                fact("midterm is on 2026-11-02", "c"),
                fact("essay is due 2026-11-08", "d"),
                fact("final is on 2026-12-15", "e"),
            ],
            TODAY,
        )

    def test_only_what_is_close_enough_to_act_on(self):
        lines = nudges(self.dated, DEFAULT_HORIZON_DAYS)
        self.assertEqual(len(lines), 4, "four of the five are inside the horizon")
        self.assertFalse(any("final is on" in line for line in lines), "day 44 is not")

    def test_the_horizon_boundary_is_inclusive(self):
        inside = nudges(self.dated, DEFAULT_HORIZON_DAYS)
        self.assertTrue(any(line.startswith("in 7 days ") for line in inside))
        shorter = nudges(self.dated, DEFAULT_HORIZON_DAYS - 1)
        self.assertFalse(any("essay is due" in line for line in shorter))

    def test_the_phrasing_answers_how_long(self):
        lines = nudges(self.dated, DEFAULT_HORIZON_DAYS)
        self.assertTrue(lines[0].startswith("overdue by 7 days — "))
        self.assertIn("due today — quiz is on 2026-11-01", lines)
        self.assertIn("due tomorrow — midterm is on 2026-11-02", lines)
        self.assertIn("in 7 days — essay is due 2026-11-08", lines)

    def test_one_day_is_not_plural(self):
        dated, _ = deadlines([fact("draft was due 2026-10-31")], TODAY)
        self.assertEqual(nudges(dated)[0], "overdue by 1 day — draft was due 2026-10-31")


class TheHeadline(unittest.TestCase):
    def line(self, *texts):
        dated, _ = deadlines([fact(text, str(i)) for i, text in enumerate(texts)], TODAY)
        return headline(dated, DEFAULT_HORIZON_DAYS)

    def test_nothing_at_all(self):
        self.assertEqual(self.line(), "Nothing due that I know about.")

    def test_something_overdue_says_so_first(self):
        self.assertEqual(
            self.line("draft was due 2026-10-25", "midterm is on 2026-11-12"),
            "Overdue: 7 days past 2026-10-25 (1 more dated item).",
        )

    def test_one_overdue_item_does_not_claim_more(self):
        self.assertEqual(
            self.line("draft was due 2026-10-25"), "Overdue: 7 days past 2026-10-25."
        )

    def test_overdue_by_one_day_reads_correctly(self):
        self.assertEqual(
            self.line("draft was due 2026-10-31"), "Overdue: 1 day past 2026-10-31."
        )

    def test_due_today_and_tomorrow(self):
        self.assertEqual(self.line("quiz is on 2026-11-01"), "Something is due today.")
        self.assertEqual(self.line("midterm is on 2026-11-02"), "Something is due tomorrow.")

    def test_within_the_horizon_gives_the_count(self):
        self.assertEqual(
            self.line("essay is due 2026-11-06"),
            "Next deadline is in 5 days, on 2026-11-06.",
        )

    def test_beyond_the_horizon_says_nothing_is_close(self):
        self.assertEqual(
            self.line("final is on 2026-12-15"),
            "Nothing due in the next 7 days. Next is 2026-12-15.",
        )


class ComposingTheBrief(unittest.TestCase):
    def compose(self, **kwargs):
        options = dict(
            facts=[
                fact("draft was due 2026-10-25", "a"),
                fact("my advisor is Dr. Chen", "b"),
            ],
            today=TODAY,
            weak_topics=["stoichiometry", "titration", "organic", "extra one"],
            open_quiz={"topic": "organic chemistry", "question": 2, "total": 5},
            generated_at="07:00:00",
        )
        options.update(kwargs)
        return compose_brief(**options)

    def test_the_shape_the_panel_renders(self):
        brief = self.compose()
        self.assertEqual(
            sorted(brief),
            sorted(
                [
                    "counts", "cost_usd", "date", "deadlines", "generated_at",
                    "headline", "horizon_days", "nudges", "open_quiz", "source",
                    "undated", "weak_topics",
                ]
            ),
        )

    def test_it_reports_itself_as_free(self):
        """The one number every other reply from this backend also reports."""
        brief = self.compose()
        self.assertEqual(brief["cost_usd"], 0.0)
        self.assertEqual(brief["source"], "code")

    def test_counts_add_up(self):
        brief = self.compose()
        self.assertEqual(
            brief["counts"], {"dated": 1, "overdue": 1, "due_soon": 1, "undated": 1}
        )

    def test_due_soon_counts_only_inside_the_horizon(self):
        brief = self.compose(facts=[fact("final is on 2026-12-15")])
        self.assertEqual(brief["counts"]["dated"], 1)
        self.assertEqual(brief["counts"]["due_soon"], 0)
        self.assertEqual(brief["nudges"], [])

    def test_undated_notes_are_reported_rather_than_dropped_or_dated(self):
        brief = self.compose()
        self.assertEqual(brief["undated"], [{"id": "b", "text": "my advisor is Dr. Chen"}])

    def test_weak_topics_are_capped_so_the_brief_stays_a_brief(self):
        brief = self.compose()
        self.assertEqual(brief["weak_topics"], ["stoichiometry", "titration", "organic"])

    def test_an_open_quiz_is_passed_through(self):
        self.assertEqual(self.compose()["open_quiz"]["topic"], "organic chemistry")
        self.assertIsNone(self.compose(open_quiz=None)["open_quiz"])

    def test_no_facts_is_an_honest_empty_brief(self):
        brief = self.compose(facts=[], weak_topics=[], open_quiz=None)
        self.assertEqual(brief["headline"], "Nothing due that I know about.")
        self.assertEqual(brief["deadlines"], [])
        self.assertEqual(brief["nudges"], [])

    def test_the_same_input_gives_the_same_brief(self):
        self.assertEqual(self.compose(), self.compose())

    def test_the_date_is_the_one_it_was_handed(self):
        self.assertEqual(self.compose()["date"], "2026-11-01")


class TheDailyTick(unittest.TestCase):
    def test_nothing_before_the_hour(self):
        self.assertFalse(brief_due(6, "2026-11-01", "", 7))

    def test_it_fires_at_the_hour(self):
        self.assertTrue(brief_due(7, "2026-11-01", "", 7))

    def test_it_does_not_fire_twice_in_one_day(self):
        self.assertFalse(brief_due(9, "2026-11-01", "2026-11-01", 7))

    def test_it_fires_again_the_next_day(self):
        self.assertTrue(brief_due(7, "2026-11-02", "2026-11-01", 7))


if __name__ == "__main__":
    unittest.main(verbosity=2)
