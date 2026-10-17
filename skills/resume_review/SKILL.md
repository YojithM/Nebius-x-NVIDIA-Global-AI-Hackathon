---
name: resume_review
description: "Review a resume the student is looking at: the strongest thing on it, the biggest gap for the role they want, and the wording fixes worth making."
version: 0.1.0
author: Cadence
license: MIT
tier: super
requires_confirmation: false
context: [page]
# The reply rewrites the student's own lines, so the server checks that no number in
# a rewritten quote is missing from the document and says so in the reply if one is.
# A prompt rule alone leaked "30+ students" into a rewrite of a resume without it.
verify_source_numbers: true
when_to_use: "the student asks for their resume, CV or application document to be judged or improved — 'review my resume', 'look over my CV', 'what's wrong with my resume', 'critique my resume for a software internship', 'is my resume good enough for this role', 'make my resume better'"
not_for: "a request to read the document out or summarise what is in it ('what does this page say', 'analyze this page' - that is page_summary), to explain a concept, to plan or schedule work, to be quizzed, or to remember something. Also not for work that is not an application document: a reading, a worksheet or a transcript is not a resume, and a review of one is a summary with an opinion attached"
tools: []
metadata:
  hermes:
    tags: [career, writing, education]
---

# resume_review

Reads the resume the student is looking at and answers the only three questions
they actually have: what is working, what is missing for the job they want, and
which sentences to change.

The temptation in this skill is to be agreeable — resume advice from a model tends
to come back as praise with a list of generic suggestions ("quantify your impact",
"use action verbs") that the student could have guessed. So the prompt is written
against that: name something specific from *their* document, name the gaps that
matter for *their* target (the biggest first, the next only if it is nearly as
serious), and give one or two rewrites as exact text — the old wording and the new.

Being useful also means not lying about what is there. Nothing is invented: if the
resume has no dates or no roles, that absence is the finding, and the prompt says so.

## When to Use

Use it when the student hands over an application document and asks for judgement
on it — review, critique, improve, "what's wrong with this", "is this good for X".

Do not use it to *describe* a document (that is `page_summary`) or to explain one
(`explain`). A resume review is an opinion; a summary is not. And do not use it on
anything that is not an application: a transcript, a worksheet or a chapter is not
a resume, and reviewing one is inventing a genre the document is not in.

## Input

```json
{
  "role": {
    "type": "string",
    "required": false,
    "default": "not specified - work it out from the document itself",
    "description": "The job, internship or program they are aiming at, in their own words: 'software engineering internship', 'data analyst roles at a bank', 'med school'. Omit it when they did not say."
  }
}
```

## Prompt

```
Review this resume for the student.

They are aiming at: {role}

If that target is not specified, do not stop to ask which role they want: review the document on its own terms, name the kind of role it reads as, and judge it against that. A student who wanted a target named would have said one.

If a PAGE section is below, that IS their document: review what it actually says, using its own names for employers, dates, schools and projects. Never add an experience, a number or a title that is not in it - and that applies to your suggested replacements too. A resume is read by a recruiter who may ask about every line, so a metric you invented becomes a lie the student has to defend; if a line would be stronger with a number, say which number to add and leave the adding to them.

If there is a MISSING PAGE note instead, say in one sentence that you could not read it and ask them to attach it - the Read a PDF button or Read this page takes it - and do not guess at what is on it.

Answer in at most 4 sentences of plain spoken prose, plus at most two replacements written as: "the current wording" becomes "the better wording". Flowing sentences only - no numbered list, no bullets, no headings, no markdown: this is read aloud, and a list read aloud is unusable. Say it the way you would say it out loud to a friend over coffee - sentences, not section names, and no label in front of a part to say which part it is. Cover three things, in this order. First, the strongest thing on it, named specifically - the actual line, number, title or project. Second, the biggest gap for that target, and a second gap only if it is as serious as the first: each with why it matters to a reader who is deciding in seconds. Third, the replacements, as changed words rather than a description of the change. Keep the whole answer under 120 words, and stop after the replacements: no closing summary of what you changed, no offer to help further, no next-step suggestion unless the document has no role or date on it at all. Measured against the live model, the version without a word budget ran to 230 words with two sentences of nothing at the end.

A replacement may only use facts, titles and numbers that are already in the document, and should be no longer than the line it replaces. This is the rule most often broken, so here is what it looks like when it is kept:

  from: "Helped run the fall hackathon. Worked with a team on logistics and did some web work for the site."
  to:   "Ran logistics for the fall HackRU hackathon and built the event site with React."

No participant counts, no percentages, no "within 24 hours", no "30+ students": every one of those is a claim the student would have to defend in an interview, and inventing one is the worst thing this review could do. If a line would be stronger with a number, ask for it instead of supplying it: "tell me how many people came and I will put that in".

If the document is too thin to judge - no roles, no dates, no detail - say what is missing instead of reviewing a blank page. Do not do their work for them: suggest the fix, do not write the whole resume, and do not invent achievements to fill the gaps.
```

## Steps

1. The router picks this skill when the student asks for judgement on an
   application document, and extracts `role` when they named one.
2. `context: [page]` is what attaches the document — the tab they asked us to read,
   or a PDF they picked with **Read a PDF**. Both arrive as page text, and the
   backend refuses page text that no request asked for, so a review without an
   attached document is answered with "I could not read it" rather than a review of
   nothing.
3. The prompt is rendered and sent to the declared tier (`super`).

## Verification

Ask *"review my resume for a software engineering internship"* with a resume
attached and the reply must: name something that is actually on the document,
identify the gap for that target (and a second only if it is as serious), and
contain at least one explicit replacement (*"X" becomes "Y"*), with **no bullet
characters and no more than four sentences**.

Measured on the live model. Character counts, which is what the panel's spoken
ceiling (`MAX_SPOKEN_CHARS`, 900) is expressed in, over the measured runs on this
text: **661, 687, 748, 803, 830, 868, 896, 958, 981** — so most runs are read aloud
in under a minute and a few creep past the ceiling, which the server notes in the
action log as `reply_long` and otherwise ignores. The first version of this prompt,
with no length instruction at all, produced **1,213 characters / 230 words** and was
logged as `reply_long`. Expect to land slightly over the 120-word instruction about
one run in three; the budget moved the answer from an essay to a spoken review, not
to a hard limit.

Two things to know before tuning this file again. An earlier version that *named*
the labels it did not want ("no prefixes like `Strength:`") made the model print
those labels in every run; describing the shape instead — *"sentences, not
section names"* — got prose back. And the model still supplies an invented metric in
about one measured turn in four — 9 `reply_number_unbacked` entries against 36
logged `resume_review` turns on 2026-10-17 — which is why the check is in code
rather than in this text.

With nothing attached it must say it could not read the document instead of
reviewing from memory. The meta line under the reply shows the page title and how
many characters were read; zero means the document never arrived.
