---
name: page_summary
description: "Summarise the page the student is looking at — what it says, and what matters in it."
version: 0.1.0
author: Cadence
license: MIT
tier: super
requires_confirmation: false
context: [page]
when_to_use: "the student points at the page in front of them and asks what it says or what is in it — 'analyze this page', 'summarise this', 'summarize this article', 'what does this page say', 'give me the gist of this', 'skim this and tell me the point', 'tl;dr this page'"
not_for: "a request to explain a concept in depth (that is explain), to restate wording without changing the meaning (rephrase), to plan, to remember something, or to be tested ("quiz me on this page" is quiz, however page-shaped it sounds). Not for a question about the student's own schedule or deadlines ("what's due this week") — a page being open does not make that a request about the page, and the answer is in their files. And not for a request to *judge* an application document — 'review my resume', 'critique my CV', 'is my resume good for this role' is resume_review, which gives an opinion on it rather than describing what is in it. Also not for summarising something they have not pointed at — a book, a chapter, or a topic in the abstract has no page behind it, and this skill summarises a page it was given"
tools: []
metadata:
  hermes:
    tags: [reading, summary, education]
---

# page_summary

Reads the page the student is on and gives back what it says, in their ears, in
the time it takes to walk to the next lecture.

The whole point of this skill is the shape of the answer. `explain` teaches a
concept, and `rephrase` keeps the student's own words; neither of them answers
*"I'm looking at fifteen hundred words of reading — what is it actually saying?"*
That is a summary: the claim, the two or three things under it, and nothing else.
It is spoken, so it is prose, not bullets.

## When to Use

Use it when the student points at a page and asks for the content: a summary, the
gist, the main point, a skim, an analysis, "what does this say".

Do not use it for a page they want *explained* — "I don't get this page, walk me
through it" is `explain`, which goes deeper on one idea instead of covering the
whole thing. Do not use it to shorten a passage's wording (`rephrase`), and do not
use it on nothing: if the student names a page and we could not read it, this
skill says so rather than inventing a summary.

## Input

```json
{
  "level": {
    "type": "string",
    "default": "some background",
    "enum": ["new to it", "some background", "confident"],
    "description": "How much they already know about the subject. 'new to it' unpacks the terms the page assumes; 'confident' skips the setup and goes to the findings."
  }
}
```

## Prompt

```
Summarise the page the student is reading.

Assume this level of background: {level}.

If a PAGE section is below, that is the page: say what it actually says, using its own names for things. Never summarise a page from its title alone, and never add a fact that is not in it.

If there is a MISSING PAGE note instead, say in one sentence that you could not read it and ask them to paste the passage — do not guess at what it says.

Answer in at most 4 sentences of plain spoken prose: first the one thing the page is saying, then the two or three points that carry it. No bullets, no headings, no markdown, because this is read aloud. Quote at most a few words from the page. If they asked about one part, give that part and one sentence of what surrounds it.

Do not do their graded work: summarise the reading, do not write the essay it is asking for.
```

## Steps

1. The router picks this skill for a page-shaped summary request and extracts
   `focus` and `level` where the student gave them.
2. `context: [page]` is what lets the backend attach the page's text — fenced,
   labelled as untrusted data, and only when the student asked for the page this
   turn. A summary request without that flag is answered with the "could not
   read it" note instead of an invented summary.
3. The prompt is rendered and sent to the declared tier (`super`).

## Verification

Ask *"analyze this page"* on a long article and the reply must be **at most four
sentences, with no bullet characters**, naming what the page actually argues. The
meta line under the reply shows the page title and how many characters were read;
zero characters means the page never arrived, and the answer is a refusal rather
than a summary.
