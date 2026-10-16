# The demo's sample data

This folder is what `/demo` runs on: one invented student's course load, written
in exactly the format the real thing uses — Markdown under `memory/`, one file
per quiz under `progress/`.

**Why it exists.** The panel is only interesting with a student in it. A visitor
who clicks a demo link has no memory files, no quizzes and no memory of ever
telling the assistant anything, so the Today tab would be empty and the Progress
tab would say *"nothing studied yet"* — the two screens that make the case for
the product would be the two empty ones. So the demo ships a course load.

**What it deliberately is not.** It is not a fixture the tests depend on for
behaviour: those build their own files in a temp directory. It is not
read-only-by-permission either — the demo *does* let you remember things and
take a quiz, because that is the interesting part, but it runs on a throwaway
copy of this folder made at startup (`server.demo_workspace()`) rather than on
these files. Nothing a visitor does reaches the checkout, and nothing survives a
restart.

**The invented student.** Second-year, 15 credits, organic chemistry and
statistics, a midterm on November 12, a lab report due in a few days, weak on
stoichiometry, studies in 25-minute blocks. One dated deadline is deliberately
vague (*"sometime next month"*) so the Today tab demonstrates the rule that
matters most: an undated note is reported as undated rather than guessed at.

`backend/tests/test_demo.py` loads every file here through the real
`MemoryStore` and `QuizStore` and asserts they parse with no errors, so this
data cannot silently rot into a broken demo.
