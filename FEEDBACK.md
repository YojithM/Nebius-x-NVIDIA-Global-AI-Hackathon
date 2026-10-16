# FEEDBACK.md

Specific notes about Nebius and NVIDIA tools as we build. Newest first.
Each entry: what we tried, what worked, what didn't, where the docs were unclear.

Project: personal voice study assistant for a college student.
Stack: Chrome MV3 side panel -> FastAPI/agent backend on Nebius -> NVIDIA Nemotron on Nebius Token Factory.

---

## 2026-10-16 — Step 11 (second slice): the agent drafts a skill, the student approves the file

**Verdict: the write path took an hour; the *prompt* took three live runs to get
right, and every one of the four defects below was found by driving the real model
rather than by a test. A feature whose whole trigger is a sentence the model has to
recognise is not finished when the code passes — it is finished when the sentence
works.**

### What didn't work

- **The tool description told the model the format, and the model got it wrong four
  times in a row.** Live, on the real account: frontmatter opened with `---` and
  never closed; `context:` filled with "Organic chemistry labs with emphasis on
  reaction mechanisms, spectroscopy" (it is a key holding one of four names, and
  the card said "optional context" and nothing else); and a `## Prompt` section with
  a closing fence and no opening one. Each refusal went back to the model as data,
  which is why it could fail four times without breaking the turn — but four
  refusals in a row is a prompt that has not been written yet. **The card now
  carries a complete worked SKILL.md** (frontmatter, both fences, a `{goal}`
  placeholder) as the value of its `when`, and the first draft after that change was
  valid on the first attempt. A model that has never seen a file copies a template;
  it does not reconstruct a specification. The card is the longest in the set and it
  is only paid for on general turns — about a ten-thousandth of a cent each.
- **The trigger lost to the task.** Three of the first four phrasings never reached
  the tool at all: *"I liked how you broke lab report 3 into study blocks — save
  that as a skill"* went to **`remember`** (which then wrote the sentence into
  [`memory/weak_topics.md`](memory/weak_topics.md) as a fact about the student);
  *"Write yourself a new skill for turning a lab report into study blocks"* and
  *"Create a new skill file named lab_blocks that breaks a piece of work into study
  blocks"* both went to **`plan`** / **`study_breakdown`**. That is the router doing
  its job badly on purpose: those skills' cards match the *task* described, and an
  authoring request describes one. Fixed in two places, both of them data: the
  router prompt now says a request to create/draft/save a **new** skill is always
  `general` ("*make yourself a skill for lab reports* is not a request about a lab
  report"), and the three skills that captured it name that case in their own
  `not_for`. Measured after: two of three phrasings reached `general` and called
  `propose_skill`.
- **The model answered with our own protocol, and the panel read it out loud.** One
  live turn replied with a sentence, the whole `{"tool": "propose_skill", ...}`
  object, and the entire skill file inside it. `parse_step` only treats a
  whole-reply object as a decision — deliberately, so that a model saying "I could
  reply with `{"tool": "web_search"}` but I already know this" does not get the
  search it just declined — and everything else is prose, so this was rendered and
  spoken as the reply. Not acting on it and *showing* it are different things: a
  reply containing a protocol object is now a step we cannot use, and the model is
  told to send the object and nothing else. The existing test that pinned prose as
  an answer was updated deliberately, with the live turn written into it.
- **A screenshot found a two-year-old-style bug in a line we had already shipped:**
  with a question open, the meta line read `this expires in 5 min10246ms` — JSX
  drops the newline between the approval note and the latency that follows it. The
  separator now lives inside the note. Nothing but looking at it would have found
  that.

### What worked

- **Draft, question, write, and usable on the very next request — with no
  restart.** The registry is re-read from the directory the running registry was
  reading, so "saved" means the next utterance can be routed to it, and a file that
  only becomes real after a restart has not been saved. Observed live: the drafted
  skill answered *"turn my lab report into study blocks"* on the next turn, with the
  skill's own name in the meta line.
- **What is written is what was shown.** The approval stores the exact text, the
  panel renders that same string in a `<pre>`, and the file on disk came back 1099
  bytes — the character count the log recorded for the draft. Two checks run, one
  when the draft is made and one when the file is written, because the student may
  take minutes to read it and the library can change underneath.
- **A file we wrote for ourselves may only say things.** `action` must be `prompt`,
  refused at both layers with the reason. The reviewed allowlist (`prompt`,
  `remember`, `quiz`) still decides what a *hand-written* skill file may select, and
  that remains right for files a person reviewed; a file drafted in the same turn
  and approved from a preview is not that.
- **The refusal to write is the feature, not a limitation.** The tool cannot write,
  the loop cannot write, and the only caller of `save()` is the approval path. The
  panel's Yes button is therefore the only thing in the system that can create a
  skill, and it can only create the file that was on screen.
- **A failed draft is never reported as saved.** Told its file was refused, the
  model's own reply was *"I have drafted a skill called …"* — a claim nobody can
  check. The loop carries the refusal out as `draft_error`, and the server, which is
  the only thing that writes, says the true sentence instead: *"I tried to write that
  skill file and it would not pass, so nothing has been saved."* Seen live twice.

### Measured, live, in the browser

- **The whole chain, on the real model, in the real panel** (the built panel served
  on :8791 talking to the backend on :8787, driven through the mic element with a
  stand-in Web Speech API so a turn is a real turn): a spoken authoring request →
  `general · 6961ms · $0.002063 · super · propose_skill · 3 steps`, with the draft
  rendered in monospace under the reply and `saved skills/turn_lab_report_into_study_blocks/SKILL.md`
  in the head line; clicking **Yes, save it** → *"Saved: turn_lab_report_into_study_blocks.
  It is in your library at skills/turn_lab_report_into_study_blocks/SKILL.md"*,
  `0ms · $0.000000 · super · saved skills/…`; then `/skills` listing 7 skills with
  `errors: []`, and the **Log** tab rendering `skill_written · chars 1099 · skills 7
  · added 1` and `skill_draft · chars 1099 · approval ap0001 · ttl seconds 300` —
  counts, never a word of the file.
- **A restart between the question and the answer gives a 409, and that is the
  right answer.** I caused this myself by restarting the backend while a draft was
  open: the click came back *"I am not waiting on that anymore"* and the log said
  `approval_unknown`. An approval is a conversation, not a setting; the honest thing
  to do with a stale yes is refuse it. It looks like a bug for about a minute.
- **Tests: 534 → 577, all green**, in a new [`backend/tests/test_skill_authoring.py`](backend/tests/test_skill_authoring.py)
  (41) plus one card assertion and one protocol assertion. Three existing
  assertions were updated because the behaviour they pinned was deliberately
  changed: the tool set, the `/health` tool list, and prose-that-quotes-the-protocol.
- **The CI artifact check still passes** after the panel change, reproduced from the
  workflow's own script.

### Smaller notes

- **The model is roughly one-in-two at drafting a valid file from scratch** — it
  answered one request in prose without calling the tool at all, and got the file
  wrong twice on the same phrasing that worked once. That is the honest state of a
  small model writing a structured file, and it is why the failure path says what
  happened instead of pretending; a second attempt usually lands.
- **Two artefacts of the live run were removed afterwards**: the skill it wrote into
  `skills/` (my library, not a real one) and the duplicate fact `remember` had
  written into `memory/weak_topics.md`. The first is the point of the feature; the
  second is the routing bug above, in the student's own file.
- **Reading a file is the first thing in this app that the screen does better.**
  Step 10's approvals could be answered by voice in principle; a forty-line
  Markdown file cannot be read aloud, so the sentence composed in code points at
  the screen twice (`the whole file is on screen under skills/… - read it, then yes`).
  If this ever gets a voice-only path, the draft has to be summarised into something
  a person can hold in their head, and that summary becomes the thing they approve —
  which is a different design, not the same one minus a button.

---

## 2026-10-16 — Step 11 (first slice): grounded citations

**Verdict: the plumbing was an afternoon; the finding was that the defect had
been invisible for two steps.** Nothing in the app looked broken. `web_search`
had returned titles and URLs since Step 9 and the reply had never named one of
them — so every searched fact was *unverifiable*, and no test, no cost line and
no log entry could have said so. It took writing the requirement down ("a
searched fact should be checkable") to see that it was missing at all.

### What didn't work

- **Two numbering schemes over the same list, one in the prompt and one in the
  reply.** `web_search` rendered its results as `1. title - url`, and the model
  was asked to cite `[1]`. With a single search those agree. With two searches in
  one turn they do not: the second search's `1.` and the first search's `[1]` are
  different pages, and neither layer had any way to notice. The tool no longer
  numbers anything — the loop is the single owner of citation numbers, and a test
  pins the tool's text against ever growing a number again.
- **The output budget could cut a result mid-block, which would have made a page
  citable that the model never fully read.** `MAX_RESULT_TEXT_CHARS` was 1200 and
  a full block runs to ~730 characters (a 120-char title, a 300-char URL, a
  300-char snippet and the separators), so a search with long titles or URLs was
  clipped in the middle of one. Harmless while that text was only *read*; not
  harmless once the model may *cite* it, because the citation list is built from
  what was rendered. Now blocks are admitted whole or omitted whole (with "N more
  result(s) omitted" said out loud), the list is built only from the blocks that
  fitted, and the budget is 2400 — with a test that three **maximum-size** results
  fit inside it, which is the largest a result can be because every field is
  clipped before it is rendered.
- **`run_tool`'s unknown-tool return was missing the key it was supposed to
  carry.** The first test written against the contract (`sources` on every return
  path) failed on the path nobody would think about: a tool name that does not
  exist. One line, found in seconds, because the test asserted the *shape* rather
  than the happy path.

### What worked

- **Links come from the tool's list, never from the reply's words.** This is the
  whole safety property, and it is one line in the panel: it renders anchors for
  `sources` and for nothing else. So a URL the model composes stays plain text,
  and a citation number it invents has nothing to point at. Two rules in the
  prompt (cite only numbers in a SOURCES list, never write the URL) are the
  ergonomics; this is the guarantee.
- **A marker with no page behind it is removed before the student sees it.**
  Counted, not guessed: the loop strips unresolvable `[n]`, logs the *count*
  (`1 citation(s) pointed at no source`) and never the marker. A citation a
  student can follow and find wanting teaches them to distrust the ones that are
  real, which is worse than no citation.
- **The reply is never emptied by that rule.** A model that answers with nothing
  but `[4]` keeps its own words back, brackets and all — the same "silence is the
  one reply a voice assistant cannot give" rule Step 10 established.
- **The speech path inherits it.** Markers point at links on screen; out loud
  there is nothing to point at, so the panel strips them before speaking and
  falls back to the raw text if stripping would leave nothing. Verified by
  recording what actually reached `speechSynthesis`: no brackets in the audio
  string.

### Measured, live, in the browser

- **A real turn, end to end.** *"what is the latest version of python?"* →
  `general`, **3 steps**, one `web_search`, **$0.001170** over 4.5s. The reply
  cited `[1]`; three source links rendered under it; clicking `[1]` opened
  `https://www.python.org/downloads/release/python-3130/`; the meta line read
  `general · super · web_search · 3 steps`; the **Log** tab read `cited 3`; and
  the spoken string was the sentence with the marker gone. Then a second turn
  from `curl` against the same server, to check the JSON contract rather than the
  rendering — same three `sources`, same shape, `$0.001421`.
- **The model cited one source out of three, which is the correct behaviour** and
  worth stating: it cited the page that supported the claim and left the other
  two as evidence the student can check, instead of spraying all three.
- **Tests: 502 → 534, all green.** The 32 new ones are
  [`backend/tests/test_citations.py`](backend/tests/test_citations.py), in three
  groups matching the three ways this can fail: a page in the list that was never
  really read, numbering that drifts across two searches, and a marker with
  nothing behind it reaching the student. Two existing assertions were updated in
  the same commit, because the behaviour they pinned was deliberately changed:
  the tool no longer numbers its results, and the loop now imports `re` — which
  the "no import that can reach the network" test in
  [`test_agent.py`](backend/tests/test_agent.py) caught for us, exactly as it was
  written to.
- **The CI artifact check still passes** after the panel change, reproduced
  locally from the workflow's own script: `dist/ is loadable: MV3 manifest, every
  referenced file present, no inline scripts`.

### Smaller notes

- **Checking this without a search key needs the provider stubbed at the HTTP
  seam** (`tools._post_json`), not at the `opener` argument, because the route
  builds its own `run_tool` and there is no opener to hand it. The first stub
  written took the opener's four arguments and failed with
  `opener() takes 4 positional arguments but 5 were given` — worth knowing before
  writing this twice. What that stub *cannot* verify is what a live provider
  returns; the plumbing is verified, the provider is not.
- **The model puts the marker in slightly different places each run**: `3.13.0
  [1].` in one turn and `3.13.0.[1]` in another, sometimes with no space. That is
  the model's prose and we left it alone — the panel makes the marker a link
  wherever it lands. A future prompt tweak that says "after the full stop" would
  be cosmetic, not correctness.
- **The citation count in the log is a count, not a list of URLs**, for the same
  reason a search snippet is not logged: the page belongs to whoever wrote it, and
  the student's log is not the place to file other people's pages.

---

## 2026-10-16 — Step 10: permissions, the action log, the cost display, and a demo mode

**Verdict: the step itself went smoothly and the *verification* is what found the
bugs — two of them in code we had already tested and shipped. Every one of the
three real defects below was found by opening the running panel in a browser and
looking at it, not by a failing test.**

### What didn't work

- **The panel talked to the wrong port, so a working demo read `backend: offline`.**
  The panel had always defaulted to `127.0.0.1:8787` unless a build-time
  `VITE_BACKEND_URL` said otherwise — fine for a local dev panel, wrong the moment
  the backend serves the panel itself. Opened at `/demo` on port 8791, every fetch
  died with `ERR_CONNECTION_REFUSED` against 8787, and the badge read *offline*
  while the backend was the thing that had just handed over the page. Fixed by
  inheriting `location.origin` when the panel is served over HTTP, keeping the
  build-time override first and the extension fallback last. Worth writing down
  because the tests were all green: the panel had no test that asserted *which*
  server it talks to, and a demo URL is exactly where that matters.

- **A skill answered a spoken question with 3,659 characters of its own
  deliberation, and the panel was about to read it aloud.** *"Email Professor
  Alvarez to ask for an extension"* routed to `plan` (Ultra, a thinking tier). What
  came back was not an answer: *"The student needs a schedule for lab report 3... I
  need to create a schedule using 25-min morning blocks... Let me work out the
  order of the pieces, then produce the"* — cut off mid-word because the output
  budget ran out. The persona asks for three sentences. The tell is
  `finish_reason: "length"`, and that is now the one signal we act on: a truncated
  reply gets **one** more call, asking for at most three sentences of spoken words
  with thinking off and a 300-token budget (room to deliberate again would
  reproduce the failure). Same utterance, after the fix: a **400-character real
  answer** — "You have October 13 through 19 to finish lab report 3 before the
  October 20 deadline..." — at **$0.0042** for the turn instead of $0.0034, so the
  guard costs about **$0.0008** when it fires. A long but *complete* reply is
  deliberately left alone and only logged: shortening an explanation the student
  asked for would be us being clever at their expense.

- **...and the same model will stop mid-sentence with `finish_reason: "stop"`.**
  The very next live turn missed the guard entirely. `study_breakdown` on Super
  ended *"...because visualizing electron flow builds the intuition needed for"* —
  complete according to the API, unusable as speech. `1/1` observed, so this is a
  suspicion rather than a measurement, and we are **not** fixing it with a
  clever-looking heuristic (trim to the last full stop? that deletes the answer's
  tail). It is written down as the open question it is.

- **The spend table counted every turn twice, in code I had just written.**
  `record_call()` per model call and `record_turn()` per completed turn both added
  to the process total, so a two-call turn landed in the running cost twice. Caught
  while wiring the panel, not by a test — the fix separates them (the total is the
  sum of completed turns; the per-tier table answers a different question) and the
  test that locks it down now exists *because* of the mistake.

- **The router sent "close all my other tabs and group them by course" to
  `study_breakdown`.** No visible harm, and a misroute is not new (Step 9 fixed the
  current-facts case with an explicit rule), but this one is interesting: the skill
  that would have handled it (`rearrange_tabs`) is a test fixture, never shipped, so
  the model picked the nearest thing that *did* exist. A rule to prefer `general`
  when no skill's description matches is the obvious next experiment.

### What worked

- **Storing the ask is what makes a "yes" mean the thing that was shown.** The
  confirmation gate has existed since Step 3 but had no way to be answered: the
  panel said *awaiting your go-ahead* and dead-ended. Now the decision is stored
  per session, and approving re-runs **that decision** rather than routing the
  word "yes" — verified by reading the prompt the executor received
  (`Group the tabs by course.`, the argument the student agreed to) and by asserting
  the router is not consulted again. Approvals are single-use, expire after five
  minutes, and are per session, so a double click cannot run an action twice and a
  stale id cannot run one at all.

- **Money as integer micro-dollars.** A float accumulator drifts: a hundred
  thousand $0.0001 turns sum to `9.999999999990033` in floats and **exactly $10.00**
  here. The test asserts both halves of that, because the drift is the reason the
  design exists.

- **A cap charged before anything is routed.** `take()` increments inside the same
  lock as the check, so the 21st question in a 20-question demo is refused **before**
  the router runs — the test asserts the stub's call count does not move on a 429.
  Keyed to the **client address**, not a cookie or a header, because an identifier
  the client supplies is one it can reset: a reload must not buy a fresh budget, and  the test sends a fresh `X-Cadence-Session` on every request to prove the header
  is ignored.

- **The demo runs on a copy, so the interesting half of a demo still works.**
  `/demo` lets a visitor *write* — remember something, take a quiz — into a
  throwaway `copytree` of `demo/` made at startup. Verified live: a remembered fact
  landed in `cadence-demo-<id>/memory/deadlines.md` and **not** in `demo/` and **not**
  in the checkout's real `memory/`. Read-only would have been easier and would have
  made the demo mute; this is the version that shows what the product does.

- **`CADENCE_DEMO=on` and `CADENCE_TOKEN` refuse to coexist.** A public demo cannot
  require a token (a visitor clicking a link has neither), and a token-gated server
  must not hand the panel to strangers. The server exits 2 with the reason. The
  `/demo` route is behind the token like every other route, which is what makes the
  two settings safe to keep in one process: a browser cannot send a bearer token
  from an address bar, so nobody can load the page against a gated backend.

### Measured, live, in the browser

- **The cost line, after one `general` turn:** `$0.001057 over 1 turn · $24.999 of
  the $25.00 budget left · 19 of 20 questions left in this demo`. The turn's own
  meta line said `general 5781ms · $0.001057 · super · 2 steps`, and `/health`
  agreed. The reply answered from the sample course load ("lab report 3 draft due
  Tuesday, October 20... problem set 6 due Friday, October 23").
- **The confirmation loop, end to end:** the ask cost **$0.000117** — the router
  deciding, and nothing else (the action is free: a memory write needs no model
  call). Clicking **Yes, go ahead** produced *"Noted - I saved that under
  deadlines."* at **$0.000000**, and the log recorded `approval_asked` (ttl 300) then
  `turn confirmed: true`.
- **A full 20-question demo visit costs about two cents** at Super's ~$0.001 a turn.
  The worst case we actually saw was the truncated Ultra plan with its retry:
  **$0.0042**.
- **Tests: 399 → 502, all green.** The 103 new ones are
  [`test_spend.py`](backend/tests/test_spend.py) (17),
  [`test_approvals.py`](backend/tests/test_approvals.py) (25),
  [`test_demo.py`](backend/tests/test_demo.py) (35),
  [`test_log.py`](backend/tests/test_log.py) (14),
  [`test_replies.py`](backend/tests/test_replies.py) (9, the truncation guard), and
  3 more over HTTP in [`test_smoke.py`](backend/tests/test_smoke.py).

### What worked about the docs

- **`enable_thinking` is the single most valuable knob in this API, and the docs we
  found describe it as an option rather than as a cost/behaviour switch.** It is
  both: on a small `max_tokens`, reasoning eats the whole budget and the answer
  never arrives (documented in `config/models.json` since Step 3), and when the
  budget runs out mid-reasoning the trace is what lands in `content`. Turning it off
  for the retry is what turned a 3,659-character scratchpad into a 400-character
  answer.
- **`usage.completion_tokens_details.reasoning_tokens` is reported and is worth
  reading.** A 143-character answer cost 110 completion tokens of which **75 were
  reasoning** — billed at the output rate. Verified against the live API on this
  account (2026-10-16).

### Where the docs were unclear

- **There is no documented way to tell "the model reasoned into `content`" from "the
  model wrote an answer".** Both arrive as `content` with a `finish_reason`; the
  reasoning field is absent on these models. We ended up using `finish_reason:
  "length"` as the only reliable signal, which is why the mid-sentence stop above is
  still open.

---

## 2026-10-15 — Step 9: the general agent loop, and four bugs only measurement found

**Verdict: the loop works, and every one of its bugs was a sentence we wrote, not a
call we couldn't make. Two tools ship, both read-only; the spec's "sandboxed code"
is deliberately *not* shipped, and the reasoning is written down. The part worth
reading is the numbers below — the first version of this feature looked complete and
was inert: the calculator existed, was wired, was tested, and was never once used.**

### What didn't work

- **A tool the model never touched, because our own prompt told it not to.** The
  first rule said "use a tool only when it earns a step; if you already know the
  answer, just answer." Asked *"what is 15 percent of 240"* through the panel three
  times, the model did the arithmetic in its head **3/3** and `calculate` was called
  **0 times** — with 23 passing unit tests and a green loop. The tests could not have
  caught it: they script the model, and the model was not complying. Reworded to name
  the two cases where memory is a defect (arithmetic the student will act on; facts
  that change over time), the same three questions called the calculator **3/3**, and
  a definition still costs exactly **one** answer call with no tools.

- **Our own error message caused the next bug.** With no search key, the tool asked
  the model to "answer from what you already know and say that you could not check
  the web" — which is precisely what it did: *"I couldn't check the web just now,
  but as of my last update the latest stable release is Python 3.12.4"*. A failed
  check that is then filled from memory defeats the check. The message now says "do
  not fill the gap from memory: tell the student you could not check this", and the
  same question is **5/5 clean** — no version number, no guess.

- **Asked how many days until a deadline, it answered 49 for a gap code puts at 28.**
  Twice, reproducibly, and once with the calculator in the loop — which cannot help,
  because its grammar is numbers and it has no dates at all. The brief has computed
  `days_left` in code since Step 8; the plan was simply never given it. The general
  path now receives a `Dates already worked out ... never recompute a date yourself`
  block built from the same `scheduler.deadlines()` the brief uses: **3/3 now answer
  28 days**, matching `/brief` exactly. The same session removed "date gaps" from the
  calculator's description — it had been advertising a job it cannot do.

- **One in three current-fact questions never reached the tools.** The router sent
  *"what is the latest version of Python right now"* to the `explain` skill, which
  answered from memory (and produced a tangent about Python variables). Tools live on
  the `general` path only, so a misroute means no tool at all. One explicit router
  rule — a request for something that changes over time is always `general` — took it
  to **5/5 general, 5/5 search attempted**.

### What worked

- **The envelope is opt-in, and that is what keeps simple answers cheap.** A JSON
  object with `tool` asks for a tool; `final` is an answer; anything else *is* the
  answer. Prose that merely mentions the protocol (`I could reply with {"tool": ...}`)
  does not buy a search — that needed the whole reply to be the object, and it has a
  test. Proven over HTTP by counting calls: a plain question still costs router + 1.

- **A failed tool is data.** "web_search is not configured" goes back to the model as
  a result, so the student gets "I couldn't check" instead of an error page. The tool
  runner never raises for anything the model did; `run_tool` returns a coarse `reason`
  code (`no_key`, `rejected_key`, `unreachable`) and the log records **counts and
  codes only** — never the query or a snippet, because one is the student's words and
  the other is a stranger's.

- **Injecting `ask` and `run` made the loop testable without a network**, and fault
  injection proved the tests bite: a ceiling off by one, an envelope accepted from
  mid-sentence, a raising tool, and an unlabelled tool block are each caught. The
  first pass caught only three of four — because the agent tests inject a *stub*
  runner, so a change to `run_tool`'s own error handling was invisible there. A
  real-dispatch test (the unconfigured-search path) closed that hole.

- **Stubbing `window.SpeechRecognition` in a real browser drove a real turn.** The
  panel is voice-only and `speech.js` resolves the API at call time — the same seam
  the README already recommends for testing the mic. Both meta lines rendered live:
  `general · 4443ms · $0.000986 · super · calculate · 3 steps` and
  `general · 4629ms · $0.001045 · super · web_search (failed) · 3 steps`.

### Where the docs were unclear

- **Both search providers' request shapes had to be read, not remembered.** Exa is
  `POST https://api.exa.ai/search` with an `x-api-key` header and `numResults`; Tavily
  is `POST https://api.tavily.com/search` with `Authorization: Bearer` and
  `max_results`. A guessed shape fails as a 4xx on the student's first real question,
  so both are recorded in `backend/tools.py` next to the code that sends them.

- **Not a docs problem, but a trap that cost a rebuild:** copying a project and then
  `ln -s`-ing `node_modules` into the copy creates a *nested* link inside the existing
  directory. The build then resolved two React copies and the panel died with
  `Cannot read properties of null (reading 'useState')` — which reads like a code bug
  and is not one. `npm ci` in the copy, or build in place.

- **A server backgrounded inside a one-shot shell dies with it.** `nohup ... &` in a
  timed-out command left no listener, and the next `curl` returned an empty body that
  the test script reported as a JSON parse error. Long-lived processes belong in a
  managed background job, checked afterwards with `lsof -iTCP:<port> -sTCP:LISTEN`.

---

## 2026-10-15 — Step 8: a brief composed in code, and the two bugs only the browser found

**Verdict: the scheduler half of this project needs no model at all, and that is
its whole design. `GET /brief` reads `memory/deadlines.md` and `progress/`, parses
one date out of the student's own sentence, and answers with `cost_usd: 0.0` and
`source: "code"` — it cannot invent a deadline, because nothing in that path can
write one.**

### What didn't work

- **A memory file that appears while the server is running is invisible until the
  next restart.** `MemoryStore._refresh` is what makes "edit the Markdown yourself
  and we'll pick it up" true, but it starts with
  `known = self.topics.get(topic); if known is None: return`, so it re-reads
  *known* files only — the directory is enumerated exactly once, in `load()` at
  startup. Hand-creating `classes.md` next to a running server therefore does
  nothing (a file the app itself creates is fine: `add()` writes it and registers
  it). It cost us a demo rebuild to notice, and it is written down rather than
  fixed: a rescan on a miss is a small function, but it changes the meaning of
  every read, and Step 8 is not the place to re-open that.

- **Two weak topics rendered as one run-on sentence.** The card printed
  `Called hard by you: I really struggle with stoichiometry, titration curves confuse me`,
  because weak topics are the student's *whole sentences* rather than labels and
  we joined them with `", "`. Every test passed and the first fixture had one of
  them, so it looked right until a second one existed. It is a list now.

- **A stale bundle made a working change look like a broken one.** After
  rebuilding the panel, the page kept rendering the previous version:
  `python3 -m http.server` sends no `Cache-Control`, so Chrome served the old
  `assets/sidepanel.js` from its own heuristic cache and a plain reload did not
  help. The same `dist/` on a *second port* gave a clean origin and the new build
  appeared immediately. Worth knowing before concluding a fix did not take — or,
  worse, concluding that it did.

### What worked

- **Composing the brief in code beat prompting for it.** No model call means no
  tokens (so the honest `cost_usd` is `0.0`), the same answer twice, and every
  date traceable to a sentence in a file the student can open. The one line a
  model would have written well — the headline — is also the line where inventing
  something would do the most damage.

- **The once-a-day tick was observed live, not only unit-tested.** The running
  server wrote its action-log line 55 seconds after boot:
  `{"ts": "2026-10-15T13:06:49", "event": "morning_brief", "horizon_days": 7, "dated": 2, "overdue": 1, "due_soon": 2, "undated": 1}`
  — and then nothing, through three more 60-second cycles. That is the guard
  doing its job: past the hour **and** not already logged today. It lands in
  `backend/logs/action-log.jsonl` (counts only — never the words of a deadline)
  and the file shows the documented trade across restarts: three boots on the same
  day, three lines.

- **`due_soon` counts the overdue item, and that is deliberate.** The log line
  above reads `dated: 2, overdue: 1, due_soon: 2`, which looks like double
  counting until you say the sentence out loud: an overdue thing *is* due soon.
  The field is "inside the horizon", not "inside the horizon and not already
  late".

- **A panel change can be checked by looking at it.** Serving `dist/` over HTTP
  renders the real side panel, so the two silences the component promises were
  verified as renders rather than assertions: backend killed → no card, header
  reads `backend: offline`; backend up with empty memory → no card, no gap. The
  card's every line was then matched against the Memory tab's view of the same
  files, which is the whole claim of the feature in one screenshot pair.

### Where the docs were unclear

- **Nothing external, which is itself the interesting part.** No Nebius or
  Nemotron surface is involved: finding a date in a sentence and subtracting days
  from it is arithmetic, and the stdlib docs were sufficient. The only question
  the docs could not settle was the product one — may a brief contain a sentence a
  model wrote? We said no, and `source: "code"` says so at runtime.

- **Still not verified live: the tick across a date boundary.** `last_logged`
  lives in the process, so a server booted at 06:59 and one booted at 07:01 both
  log the day's brief; only the unit tests cover the rollover, because waiting for
  midnight is not a verification method.

## 2026-10-14 — Step 7: the price is the blocker, and the pricing pages disagree with each other

**Verdict: the tooling is real and the engine works; what stopped us is arithmetic.
An always-on CPU instance costs several times this project's entire budget, and
the public pricing pages disagree about the cheapest one. Nothing here is a bug in
Nebius — it is the part of "deploy it" that a hackathon is most likely to skip.**

### What didn't work

- **"Always on" does not fit the budget, and the two official pages disagree about
  the entry price.** The detailed Compute table prices a **2 vCPU / 8 GiB** preset
  at **$0.066/hour on `cpu-d3`** (~$48 / 30 days) and $0.060/hour on `cpu-e2`
  (~$44). The public prices page instead summarizes CPU-only as **"from
  $0.13/hour"**. The $0.13 is consistent with a *4 vCPU / 16 GiB* machine at the
  detailed rates, so the summary line appears to quote a size above the documented
  minimum — read as "the cheap tier", it nearly triples the entry price. For
  budgeting, compute from the per-resource rates; the one-line summary is not the
  minimum.

  Either way, ~$44–48/month against a **~$25 total** budget means an always-on
  deployment was never viable, and the root README had already flagged the price as
  an unverified blocker. It is verified now, and it is still a blocker — which is
  more useful than quietly shipping something that burns the budget in a week.

- **There is no free tier to experiment on any more.** The free-trial page says the
  trial was **suspended 2026-07-13 and new sign-ups are not accepted**. Token
  Factory separately documents **$1 of credit for 30 days**, which is plenty for
  inference (a turn is ~$0.0007) and irrelevant to hosting. The upshot: there is no
  "spin it up and see" step, so the price has to be settled *before* creating
  anything.

- **Serverless Endpoints are not the cheap idle option they sound like.** There is
  no documented scale-to-zero, and the **container disk does not survive a stop or
  restart** — durable `memory/` and `progress/` need a mounted bucket or shared
  filesystem, which is billed separately. Convenient to deploy, and a trap if you
  assume "serverless" means "free while quiet".

### What worked

- **Two environment variables are the entire deployment delta.**
  `CADENCE_HOST` (bind address) and `CADENCE_TOKEN` (auth). No code fork, no build
  mode, no second server. With the token unset the process is byte-for-byte the one
  Step 6 shipped, so the local panel and all **279** tests were unaffected by adding
  a security gate — which is the only reason we could add one this late without
  breaking the demo.

- **A managed endpoint can enforce the bearer token at the edge.**
  `nebius ai endpoint create --auth token --token ...` makes Nebius reject
  unauthenticated requests at the managed HTTPS URL, and the *same* token value
  satisfies our own in-process gate. Two independent layers, one secret to rotate.

- **A stopped VM is not billed for compute.** That single billing detail turns an
  impossible budget into a workable one: create the VM, start it for the demo, stop
  it after. The honest deployment for a hackathon is ~$0.07 per *demo hour*, not
  $44 per month.

- **The container is trivial, and we proved it by running it.** Because the backend
  is stdlib-only there is no `pip install` in the image at all. Built and started
  the real image locally: **all six skills loaded** inside it, the token gate active,
  a no-token turn returned **401** without spending anything, and one real turn
  routed to `explain` for **$0.0006** through the container. Deployment artifacts
  are the kind of thing that "looks right"; running it is what makes it a fact.

- **The gate is tested by counting model calls, not by trusting the status code.**
  Same trick as the quiz escapes: a refused request asserts `client.calls == []`, so
  "refused" provably cost nothing and deleted nothing. A 401 that still made a paid
  call, or still deleted a quiz, would pass a status-only test.

### Where the docs were unclear

- **The endpoint's public hostname.** The docs describe a managed HTTPS URL but do
  not show the domain without creating one, so the extension's `host_permissions`
  uses `https://*.nebius.cloud/*` and the runbook says to replace it with the exact
  origin once you have it.
- **Attaching durable storage to an endpoint.** The docs say you *can* mount a
  bucket or filesystem, but the exact `--volume` flags are not pinned down here. The
  runbook marks that mount as unverified rather than guessing.
- **Health checks.** No platform-mandated path is documented; our `/health` is ours,
  not a Nebius contract.

### The account could read everything and create nothing

This is where the deploy actually stopped, and it is the finding that matters
most for anyone else trying this on a hackathon account.

Every read passed on `default-project-us-north1`: `iam whoami`, `iam v2 project
get`, `compute platform list` (2 platforms, 13 presets), `vpc subnet list`,
`vpc security-group list`, `quotas quota-allowance list`, and the billing
calculator priced the exact VM at **$0.066/hour, $48.18/month** and its 30 GiB
`network_ssd` disk at **$2.13/month**. Everything looked ready to go.

Then every single write was denied:

- `vpc security-group create` → `PermissionDenied: Permission denied to
  project-u02dnv0gig006rqtndf08g` (service VPC API)
- `registry create` → `PermissionDenied: permission denied` (service registry)
- `compute instance create` → `PermissionDenied` (service compute)

The account is **read-only on the project**. No runbook, flag or id can fix that;
it needs a role that can write, or a service-account key that has one. What makes
it worth writing down is how well the failure hides: `nebius profile list`,
`iam whoami`, `compute platform list` and even the price calculator all succeed,
so an hour of work goes into a deploy that can never complete, and the first
signal is a permission error at the first *write* — which in the runbook is the
step that creates the registry.

- **A network's default security group refuses rules, on purpose.**
  `security-rule create` against it answers `It is not allowed to create or delete
  security rules in Network's default security group`. A working deploy needs its
  own group — and rules are created through a separate `nebius vpc security-rule`
  service (with `--access allow` and either `--ingress-*` or `--egress-*` flags),
  not as a field on the group. The default group also ships with **zero rules in
  both directions**, so a VM attached to it is unreachable and cannot even
  `apt-get install` during cloud-init.
- **`deploy/preflight.py --probe-writes` exists because of this.** It does one
  cheap write — creates the security group the deploy needs anyway — so a
  read-only account is caught before the runbook rather than halfway through. On
  the account above it reports 8 passes and this failure, and exits 1.

### Logging in worked; every id the console handed us was the wrong kind

Authentication itself was clean: `nebius profile create`, browser OAuth against
auth.nebius.com, profile `cadence` created and activated, `nebius iam whoami`
returning the right account (GitHub federation, `ACTIVE`). The trouble started the
moment a project id was needed, and all three failures were silent-ish:

- **The Token Factory console's project id is not an AI Cloud project id.**
  Copying the id from the rate-limits page — where it is displayed prominently as
  `aiproject-e00a2wpqk70kchfzjn … ⧉` with a copy button — gives a value the Compute
  API rejects: `parent_id is invalid … expected id types: project, got "aiproject"`.
  IAM calls scoped to it answer `PermissionDenied: service iam, resource ID:
aiproject-…`. Same trailing characters, different namespace, and the console that
  shows it is the one a person building an *inference* app is already logged into.
  Removing the `ai` prefix is not the fix either: `project-e00a2wpqk70kchfzjn`
  returns `Resource not found` while traversing up to the tenant.
- **`profile create` accepts an empty tenant id without a word.** The interactive
  flow announced "discovering available tenants" and then took an empty answer for
  `Set Tenant ID` and moved on, writing a `config.yaml` with **no tenant** — while
  the docs say a single-tenant account is selected by default. The cost lands two
  calls later as `get tenant ID for "project-…": iam traverse up … Resource not
  found`, which reads like a wrong project id and sends you looking in the wrong
  place. `--tenant-id` on the non-interactive form avoids it entirely.
- **The profile gives no way to notice either mistake at creation time.**
  `nebius profile list` says `cadence [default]` and exits 0 for a profile that
  cannot list a single platform. `nebius iam whoami` passes too. The first honest
  signal is `compute platform list`, which is why the runbook now runs it as a
  *prerequisite* rather than as step one of a much longer procedure.

None of this is fatal — it is five minutes of confusion at the exact moment you
are about to spend money, which is the worst five minutes to have it in. In this
case it was also a symptom rather than the disease, which took another hour to
establish and is the real finding:

- **The login is valid and the account has no AI Cloud tenant.** `iam v2 tenant
  list` returns **0 items** with exit 0 — no error, just nothing — and the
  interactive profile flow's "discovering available tenants" was presumably the
  same empty result, which is *why* it accepted an empty `Set Tenant ID` without
  complaining. Everything downstream then failed in a different, more confusing
  way: `project-…` came back `NotFound` and `aiproject-…` came back
  `InvalidArgument`. `nebius iam v2 project get --id project-e00a2wpqk70kchfzjn`
  answering `Cannot get entity ... entity not found` is also informative — a
  syntactically valid NID that simply is not in this account, as opposed to
  `project-0000…` which fails earlier with `id has invalid NID format`.
- **Token Factory and AI Cloud are separate namespaces, and only one of them is
  reachable from this account.** The id on the Token Factory rate-limits page is
  `aiproject-…`; the Compute API wants `project-…`; the AI Cloud IAM service does
  not know the former at all (`id must be a project id with 'project-' prefix`).
  An account can have a working Token Factory key — ours does, the app has been
  calling it all along — and still have no Compute project to deploy into. That is
  a distinction the docs never draw, and it is the difference between "your id is
  wrong" and "this account cannot host anything".
- **None of the discovery routes the CLI offers can tell you which case you are
  in.** The access token is opaque (not a JWT, no readable tenant claim); there is
  no project-listing command outside `iam v2`; `iam v2 tenant list` reports zero
  rather than "you have no tenant"; and `compute platform list` — the first call
  that would actually say so — is only reachable *after* you already have a
  project id. A one-line "this account has no tenant" from `profile create` would
  have saved the entire hour.

### The CLI contradicts its own help, so two documented flags never existed

Once the CLI was actually installed (0.12.291) the runbook was checked against the
binary rather than against the docs it was written from. Three of the commands in
it would have failed on first run:

- **`--cloud-init-user-data-file` does not exist.** The docs' workflow implies a
  file is passed; the real flag is `--cloud-init-user-data` and it takes the YAML
  string, so the file goes through `$(cat ...)`. A user following the original
  runbook gets an unknown-flag error at the moment they are spending money.
- **`--boot-disk-attach-mode` is documented twice, differently, in the same help
  screen.** The EXAMPLES block uses `READ_WRITE`; the OPTIONS block says the value
  must be `read_only` or `read_write`. Both cannot be right, and the enum list is
  the one that is enforced.
- **`nebius compute instance stop` takes `--id`, not a name.**
  `nebius compute instance stop cadence-backend` reads naturally and fails; the id
  has to be resolved through `get-by-name` first.

The general lesson is not "the docs are bad" — it is that **the artifact that runs
is the help text of the binary you installed, and it can be generated from a
schema that disagrees with the hand-written examples next to it.** Reading help is
free; discovering this during a deploy is not. Two useful things also turned up in
the same pass: `--volume SOURCE:CONTAINER_PATH[:MODE]` is real (which downgrades
that runbook claim from "unknown" to "known syntax, unverified source"), and
`--env-secret KEY=SECRET_SELECTOR` exists, so the endpoint path can keep the API
key out of the endpoint spec after all.

### Two bugs found after the deploy work "passed"

Both were found by continuing to pull the thread after the suite was green, and
neither was caused by the deployment. Both are written down because the failure
*shape* is the reusable part.

- **A test that failed 1 run in 8 was not noise — it was a real data bug.**
  `TestWriting.test_an_unreadable_file_is_reported_and_the_rest_still_load` failed
  intermittently, and the assertion said the session id was **`inf`**. Session ids
  are `uuid4().hex[:8]`, which is hex: `582e6104` is a perfectly valid id, but the
  frontmatter parser coerces numeric-looking scalars, `float("582e6104")` reads it
  as scientific notation, and Python returns **`inf` instead of raising**. The quiz
  then came back under an id that could never be found, answered or deleted — and
  `01234567` had the quieter version of the same bug, coming back as `1234567`. The
  odds are ~1 in a few hundred quizzes per case, which is exactly the rate that
  looks like flakiness and gets re-run until it passes. The rule is now the obvious
  one: **a scalar is only a number when reading it back gives the same text.** The
  failure is also why random ids in tests are a liability — the two regression tests
  use a fixed id instead of hoping the generator hits the bug again.

- **`CADENCE_TOKEN` in `.env` was read *after* `.env` was loaded — so the deployed
  server would have come up with auth off.** The deployment settings are read at
  import time; `load_env()` was called ~60 lines further down. Exporting the token
  worked, so the runbook was satisfied and every test passed. The failing case is
  the one a tired person actually hits: put the token in `.env` next to the API key,
  restart, and get a **public backend with no gate and no error message**. Fixed by
  loading `.env` first, and tested by *reloading the module* the way
  `python3 backend/server.py` does rather than by poking globals, so the test covers
  the real import order. With the fix reverted, that test fails — checked, not
  assumed.

### Still not done, and why

**The deployment is not live.** It needs a Nebius project and an explicit decision
about spending, neither of which belongs to the agent. Everything up to that line is
built and verified; the runbook ([`deploy/README.md`](deploy/README.md)) is the last
mile.

---

## 2026-10-14 — Step 6: the grader has to run before the router, and the tests can't see the panel

**Verdict: two failures, both ours, and both caught by looking at the live thing
rather than by the suite — which is the whole reason we ran it live. Neither
failure had anything to do with Nemotron or Nebius, and both looked correct in the
source.**

### What didn't work

- **Router-first grading explains the student's own answer back to them.** The
  first build of the quiz loop routed *first*: while a quiz was open, a turn became
  an answer only when the router found no skill for it. Live, on the very first
  answer, the student said *"plants use light to make sugar"* and the router filed
  it as `explain` — because a sentence **about a topic** is exactly what a router
  sees when it is asked *which skill is this*. So their own answer came back
  explained to them, and the question never advanced.

  The router is good at *what is this about* and cannot be good at *is this an
  attempt at question 3*. The component that can judge that is the grader, so the
  order is inverted: the grader runs first and may **decline** (`off_topic`), which
  is also how a real question asked mid-quiz still gets a real answer. Both
  orderings read as plausible in the source; only one survives contact with a real
  utterance. It is also the **cheaper** order — one Super call and no Nano router
  call per answer.

- **The store's default contract is not the panel's contract, and every unit test
  passed anyway.** `QuizStore.describe()` defaults to *summaries*
  (`include_questions=False`) — the right default for a list — so the first build of
  `/progress` returned sessions with counts and **no question list**. All the store
  tests were green. The **Progress tab rendered blank with a `TypeError`** because it
  mapped over `session.questions`. The plainest reminder we've had that a green
  suite is not a working product: the fix was one argument at the endpoint, plus a
  test class (`TestProgressEndpoint`) that drives the real `do_GET` and asserts the
  question text is in the payload. The panel is the interface; the store is not.

### What worked

- **A per-answer cost that is *lower* than a normal turn.** Graded answers need no
  router call, so one is a single Super call (~$0.0005) against ~$0.0007 for an
  ordinary turn. The intuition runs the other way — "assessment sounds expensive" —
  so it is worth stating plainly: the study feature gets *cheaper* the more it is
  used, and the meta line under each reply shows it.

- **Four words answered in code, at zero cost — and the zero is asserted, not
  claimed.** *"stop"*, *"repeat that"*, *"how am I doing"* and *"I don't know"* are
  regexes in `backend/server.py`, and the tests assert `client.calls == []` for each.
  They are precisely the utterances where a model would be slowest and least
  predictable, and the 80-character bound is what keeps *"stop"* inside *"I want to
  stop by the library before my lab"* from ending a quiz by accident.

- **The expected answer on disk makes a restart a non-event.** The progress file
  keeps `expected` alongside each question, so an open quiz survives a server
  restart and stays gradeable instead of dying on reload. The student can read the
  answer key — it is their quiz — and that is a better trade than a session that
  cannot be resumed.

- **"I don't know" is recorded as wrong, on purpose.** `skipped` is not a wrong
  answer (they didn't try), but *"I don't know"* is the single clearest signal of a
  weak topic, so it is scored `incorrect`, the right answer is read back, and it
  feeds the accuracy ranking that *"quiz me on what I'm weak at"* consults.

### Where the docs were unclear

- **No structured-output mode we can rely on.** The generation and grading calls
  want JSON, and we could not find a documented JSON-schema / `response_format`
  guarantee for these tiers, so we kept the pattern that already works for the
  router: `temperature=0.0`, ask for JSON only, one corrective retry, then a
  sentence the student can act on rather than a half-written quiz. The store also
  tolerates a model that returns the set under a slightly different shape —
  `_questions_from()` drops items without question text rather than rendering blank
  questions — because we have not yet seen the same field name twice from the same
  prompt.

---

## 2026-10-14 — Step 5: three ways a reasoning model fails a *prompt*, and one socket bug

**Verdict: the tooling was fine. Every failure here was ours — and three of them are
the kind you only find by reading the actual reply text, not by watching the tests
go green. One *is* a real property of these models, and it silently produces an
empty answer.**

### What didn't work

- **A prompt placeholder is not a data field.** `rephrase` shipped with a template
  that interpolated the passage into the instruction:
  ```
  Restate this in {style} words: {text}
  ```
  When the router did not supply a passage, that rendered as
  `Restate this in simpler words: (not specified)` — and the model did the only
  sensible thing with the words it had: it restated **the instruction**. The reply
  came back as a paragraph of our own rules, in the student's voice, read aloud.
  A peculiar own-goal, and one a test asserting non-empty output would have passed.

  The fix was structural, not a wording tweak: a passage the student quoted now
  travels as a **labelled data block** (`<<< quoted passage … >>>`) through a declared
  `context: [quoted]` source, exactly like the page and the previous reply. The
  template no longer mentions the passage at all — it names the block. Same rewrite
  for the SECOND source of the same bug: the router returned the *request itself* as
  the passage (`"rephrase this paragraph in my own words"` -> `text` = that whole
  sentence), which is not a passage; a matter of deciding in code, not in the prompt.
  **Anything the model must not confuse with instructions does not belong inside one.**

- **The reasoning trace can eat the entire output budget, and the answer arrives
  empty.** These Nemotron tiers reason before answering, and the trace is billed as
  output against the same `max_tokens`. `config/models.json` already documented this
  for the router at `max_tokens=300`; at our **900-token** output budget with a page
  in the prompt it happened anyway — one live `explain` turn returned an empty
  string, so the panel showed a reply box with nothing in it and the voice loop
  would have said nothing at all. Intermittent, which is the worst kind.

  We did not raise the budget (that just moves the cliff) and we are not going to
  strip reasoning from explanation-quality calls. An empty reply is now **retried
  once with `enable_thinking: false`** — a call with no trace to eat the budget, and
  the cheaper of the two — and if that is empty too the student gets a sentence they
  can act on instead of silence. Every occurrence is logged as `empty_reply` with
  the recovered flag, so "how often does this happen" is a query rather than a
  guess. It is the one failure here that is about the model, not about us.

- **A `ConnectionResetError` took out a whole turn.** `nemotron.py` wrapped
  `HTTPError` and `URLError`; a reset *mid-read* is neither (it is an `OSError`), so
  it escaped the client entirely. The client saw `RemoteDisconnected`, the server
  closed the connection with no response, and the panel showed a dead connection
  instead of an error it could explain. Now every network-level failure folds into
  `NebiusError`, which the request handler already knows how to turn into a 502.
  The rule we keep re-learning: **enumerate the failure modes of the transport, not
  just the ones the docs mention.**

### What worked

- **Putting the privacy policy in the handler made it a test, not a promise.**
  "Never read a page the student did not ask for" is four lines in the request path,
  so `backend/tests/test_pages.py` drives the **real** `do_POST` with in-memory
  socket stubs and asserts the refused turn made **zero model calls** and wrote
  nothing to the log. Page text that arrives uninvited is a 400 with a sentence
  telling the client what to do instead — a client bug becomes a visible failed
  request rather than quiet reading.
- **Declared context scales to a new source in one line.** `rephrase` needed a third
  kind of input, so `context:` grew `quoted` and the block assembler grew one branch;
  no skill can see it unless it names it, and a typo in the source list is still a
  load-time error. The same mechanism now carries memory, the page, the previous
  reply, and the quoted passage — four sources, one rule.
- **`plan` on Ultra used memory dates and said which one it used.** With a saved
  midterm, the plan named the date ("your midterm is November 12, so the next four
  evenings are…") and left the last block as slack — which is what makes a wrong
  date visible and correctable instead of baked in. That is the Ultra fence in
  `config/models.json` doing exactly what it was written for.
- **Escalation can be verified live, cheaply.** A single `curl` with a page and no
  invocation is the whole gate; a turn costs ~$0.0007, so the policy, the skills, and
  the empty-reply retry were all exercised against the real API in one sitting.

---

## 2026-10-14 — Step 4: a schema a model can't violate, and a policy that isn't a prompt

**Verdict: two of our own design mistakes, both found by testing the real thing. Neither
was a bug in Nebius or Nemotron — but one of them is a trap anyone routing on a model will
hit, so it's worth writing down.**

### What didn't work

- **A field *description* is never sent to the model that has to obey it.** Our `remember`
  skill declared its argument like this:
  ```json
  "topic": {"type": "string", "default": "general",
            "description": "Must be one of: deadlines, classes, weak_topics, ..."}
  ```
  The prompt we send the router contains only the **shape** of the arguments —
  `{"topic": "string", "text": "string (required)"}` — because the routing card is paid for
  on every single request and we keep it small. So the instruction "must be one of" was
  prose the router never read.

  Result: it invented a topic on every utterance. *"remember my midterm is on November 12"*
  → `topic: "organic chemistry midterm"`; *"remember I struggle with stoichiometry"* →
  `topic: "stoichiometry"`. One new file per utterance — a memory feature turning into a
  junk drawer, and the code doing exactly what the prompt asked.

  **If a value must come from a fixed set, put the set where the model can see it.** We added
  a real `enum` to the skill schema which renders inline in the routing card as
  `one of: deadlines | classes | general`, *and* is enforced by our own validator — so an
  out-of-set value now triggers the corrective retry instead of being silently accepted.
  All six test utterances then routed correctly, single-attempt.

- **A privacy policy expressed only as a prompt is not a policy.** We asked the model to
  route only *explicit* memory requests, and wrote it a careful `not_for`. Then we tested it:
  *"my midterm is on November 12"* — a remark, with no ask in it — was routed to `remember`
  and **written to disk**. Nothing in the design prevented it; the model was simply being
  helpful.

  This is the same failure we'd already fixed once for confirmations, and we failed to apply
  the lesson: **the model decides what a sentence is *about*; code decides what we are
  *allowed to do*.** `_action_blocked()` now requires the utterance itself to contain an
  explicit ask (`remember`, `note that`, `don't forget`, …) or the write does not happen and
  the turn is answered normally instead. We chose the failure direction deliberately: a false
  negative costs one clarifying sentence, a false positive keeps something the student never
  handed over.

- **Asking a model to extract a fact produces fragments.** Not a Nebius issue, but it shaped
  the design: *"don't forget my advisor is Dr. Chen"* came back as the fact `"Dr. Chen"`, and
  *"remember that I really struggle with stoichiometry"* as `"stoichiometry"`. Both are
  useless in three months, which is exactly when a memory is supposed to pay off. We stopped
  fighting it with prompt wording and made it deterministic: when the model's text is a
  **substring** of the utterance, we store the utterance with its request framing stripped.
  Same meaning, no loss, no judgement call. A genuinely richer extraction (a resolved date)
  isn't a substring, so it survives.

### What worked

- **Constraint-as-schema beats instruction-as-prose, everywhere.** The `enum` fix is the
  general lesson from Step 3's skill-name validation too: whenever the router must pick from a
  closed set, the set belongs in the prompt *and* the validator. Prose instructions are
  invisible unless you pay to send them on every request.
- **`/v1/models`-style preflight discipline applied to our own files.** Memory is loaded with
  per-file error isolation, so one hand-edited file with a typo shows up as a named problem in
  `/health` instead of taking down the student's whole memory.
- **The "no Python to add a skill" property held.** `remember` needed a real side effect, and
  the skill system still didn't grow a code path: the SKILL.md selects a handler from a
  reviewed allowlist. It cannot supply code — which matters, because Step 9 lets the model
  author skill files.

### Where the docs were unclear

Nothing, this time. The lesson was entirely about our own prompt discipline — and it is the
kind of mistake that only a live test against the real model catches, because every unit test
we had written passed while the feature quietly violated its own stated promise to the user.

---

## 2026-10-14 — Step 3: Nemotron 3 Nano is a reasoning model, and it silently ate our whole output budget

**Verdict: five lines of config turned a 40% fallback rate into 0%. But nothing in the docs
said the parameter existed.**

### What didn't work

- **A JSON-shaped routing call to Nano returned `content: None`, not JSON.** We asked for a
tiny classification — `{"skill": ..., "args": {...}, "needs_confirmation": bool}` — with
`max_tokens: 300`. Three of five test utterances came back with **no content at all**.
  We only found the cause by printing `finish_reason`:
  ```
  finish_reason='length'  content=None  completion_tokens=300
  ```
  **Nano emits a visible `reasoning` trace before `content`**, and the reasoning consumed the
  entire budget. Same request, same model, same prompt — the only variable was how long it
  chose to think. A terse utterance came back in 38 tokens; a slightly ambiguous one spent all
  300 reasoning and returned nothing.
- **Our retry made it worse.** The first failure was reported as "reply was not a JSON object"
  — technically true, completely useless. We retried at the *same* 300-token budget, so it
  truncated in exactly the same way. Retrying without changing anything is not a retry.
- **`reasoning_effort` is the wrong lever here.** Setting `reasoning_effort: "low"` *increased*
  output tokens, and `reasoning_effort: "none"` returned **no content at all** — it appears to
  suppress `content` rather than the reasoning. Do not reach for it to solve truncation.

### What worked

- **`chat_template_kwargs: {"enable_thinking": false}` turns reasoning off,** and it is
  decisive. Same prompt, same model:
  | | output tokens | latency |
  |---|---|---|
  | default | 229 | 2.5s |
  | `enable_thinking: false` | **38** | **1.1s** |

  Routing is a classification task. Nothing about *"pick the skill that fits this sentence"*
  improves with a reasoning trace, so this is pure waste — and at 300 tokens it was waste that
  broke the feature.
- **Keeping thinking ON where it earns its keep.** We made it per-tier rather than global in
  `config/models.json`: routing runs with thinking off, explanatory replies keep it on. One
  config value, switched at the call site.
- **Treating truncation as its own error class, not as malformed JSON.** `finish_reason ==
  "length"` now reads as "the model was cut off", which is a different bug with a different
  fix: the retry **raises the budget** instead of repeating the failing attempt. Fallbacks went
  from 3-in-5 to 0-in-6, all single-attempt, with latency roughly halved.

### Where the docs were unclear

- **Neither `chat_template_kwargs` nor `enable_thinking` appears anywhere in the Nemotron or
  Token Factory docs we could find.** We found it by testing what the endpoint accepts. For a
  model family marketed on reasoning, "how do I turn reasoning off, and what does it cost me
  when I forget?" is the first question a latency-sensitive integrator asks.
- **`content: None` with HTTP 200 is a trap.** The request succeeds. Nothing in the response
  *shape* says something went wrong except `finish_reason`, which sits next to fields you might
  not print. A response with `finish_reason='length'` and empty `content` deserves a warning in
  the docs — it looks like a model failure rather than a budget failure.
- **The reasoning trace arrives in a separate field from `content`.** If you only read
  `choices[0].message.content`, reasoning-heavy responses look like the model said nothing.

### One more `nvidia/` prefix gotcha

`nvidia/nemotron-3-super-120b-a12b` and `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` are both
**live IDs on the same account, with different casing conventions.** Nano is capitalized and
carries the vendor prefix twice; Super is lowercase and single-prefixed. There is no rule to
derive one from the other — we validate every ID against `GET /v1/models` before spending a
token, and that check caught nothing here only because Step 0 already pinned these two.

---

## 2026-10-14 — Step 0 COMPLETE: Hermes Agent running on Token Factory

**Verdict: Hermes works, and it's the path we're taking.** Not the fallback. Reasoning and
evidence below.

### What worked

- **The container boots fully headless with no setup wizard.** This was our open question from
  the docs gap. `docker run nousresearch/hermes-agent:latest` with a mounted `/opt/data` ran
  its whole bootstrap (`stage2-hook.sh`), generated a complete `config.yaml` and a commented
  `.env` template, synced skills, registered the `default` profile, and started services — all
  unattended. The documented interactive `setup` wizard is **not** required.
- **One-shot prompting works and is scriptable.** `hermes -z "<prompt>"` prints only the final
  reply, no banner or spinner, so it drops into a shell script cleanly.
- **Token Factory as a custom provider works**, via the documented `hermes config set`
  interface rather than hand-editing YAML:
  ```
  hermes config set model.provider custom
  hermes config set model.default nvidia/nemotron-3-super-120b-a12b
  hermes config set model.base_url https://api.tokenfactory.nebius.com/v1
  hermes config set model.context_length 262144
  ```
  Then `hermes -z "Reply with exactly: HERMES OK"` → `HERMES OK`, exit 0. `hermes status`
  confirms `Model: nvidia/nemotron-3-super-120b-a12b`, `Provider: custom`.
- **Persistent memory works across separate sessions.** Session A: *"Remember that my organic
  chemistry midterm is on November 12, 2026."* A **brand-new** session B, with no shared
  context, was asked *"When is my organic chemistry midterm?"* and answered *"Your organic
  chemistry midterm is on November 12, 2026."*  
  It stored it as plain Markdown in `memories/USER.md` — inspectable and deletable, which is
  exactly the requirement.
- **It authored a real skill unprompted by us.** Asked for an `assignment-triage` skill, it
  generated `skills/productivity/assignment-triage/` containing a `SKILL.md` with proper
  frontmatter (`name`, `description`, `version`, `metadata.hermes.tags`), a `When to Use`
  section, prerequisites, an input schema, **plus a working helper script and a sample
  `assignments.json` fixture**. Skill count went 58 → 59.
- **The `-z` flag is real.** Worth stating explicitly: we verified it against the installed
  binary rather than trusting the docs page, given the Nano model-ID incident.

### Traps that cost us time

**1. The `nvidia/` model prefix collides with Hermes's built-in NVIDIA provider.**
Running `hermes -z "..." -m nvidia/nemotron-3-super-120b-a12b` (no explicit provider) makes
Hermes infer provider `nvidia` — its bundled **NVIDIA NIM / build.nvidia.com** integration —
and fail with:

```
No usable credentials found for provider 'nvidia'. Set NVIDIA_API_KEY.
```

This is a genuinely nasty trap: every Nemotron model ID on Nebius starts with `nvidia/`, so
the natural thing to type resolves to the wrong provider by name. The fix is to set
`model.provider: custom` explicitly. Worth calling out in our README so nobody else loses time
on it.

**2. `--provider custom` alone is not enough.** It fails with *"provider 'custom' resolved
without credentials (no endpoint or API key configured)"* even when `OPENAI_BASE_URL` and
`OPENAI_API_KEY` are set in the environment. The `custom` provider needs `base_url` set in
`config.yaml` itself. The error message is unusually good — it points at `providers:` in
config.yaml — but the docs' env-var guidance led us to expect the env vars alone would do it.

**3. Nebius `/v1/models` returns no metadata.** Only `id`, `created`, `object`, `owned_by` —
no context window, no pricing. Two consequences: Hermes' documented "auto-detect the correct
context length from the provider" cannot work against Token Factory (we set
`context_length: 262144` by hand), and our cost-per-request display has to be driven by a
hand-maintained price table we must re-check in the console whenever prices move.

### Smaller notes

- The generated `SKILL.md` shipped with a placeholder `author: Your Name (github-handle)` in
  its frontmatter. Cosmetic, but it means agent-authored skills need a review pass before
  they'd be shown to a user.
- Memory landed in `memories/USER.md` (user-profile facts) rather than `MEMORY.md` (agent
  notes). Sensible, but it means our Memory tab must read **both** files to show
  "everything it knows".
- `hermes status` reports `Platforms: api_server` and `Gateway: stopped` — an HTTP surface
  exists, which is the natural hook for the extension in Step 7.
- Hermes' `state.db` already tracks `input_tokens`, `output_tokens`, `estimated_cost_usd`,
  `actual_cost_usd`, `cost_status`, `api_call_count`, `tool_call_count` and `tool_names` per
  session. That is **most of our Log tab and cost-per-request requirement, already built** —
  a strong argument for Hermes over hand-rolling the FastAPI fallback.
- It ships 58 bundled skills, including `grounded-citations` (overlaps our stretch goal 13),
  `spike`, `systematic-debugging` and `architecture-diagram`.

---

## 2026-10-14 — Step 0: spike research and API surface

**Status:** docs + model catalog verified, first real call still pending an API key.

### Worked

- **Base URL is unambiguous in the current docs.** `https://api.tokenfactory.nebius.com/v1/`
  for OpenAI-compatible clients, with `POST /v1/chat/completions` and
  `GET /v1/models`. Source: <https://docs.tokenfactory.nebius.com/quickstart> and
  <https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion>
- **Auth is plain Bearer.** `Authorization: Bearer $NEBIUS_API_KEY`; the quickstart's own
  examples read the key from an env var named `NEBIUS_API_KEY`, which is what we use.
  Nice: nothing custom to implement, the OpenAI SDK works with only a base_url swap.
- **OpenAI-compatibility is genuine, not partial.** Chat completions return normal
  `choices[].message.content` and a `usage` block with `prompt_tokens` /
  `completion_tokens`, which is exactly what our cost-per-request display needs.

### Confusing / needs care

- **Product rename.** "Nebius AI Studio" is now "Nebius Token Factory" — same platform,
  new name (<https://nebius.com/newsroom/nebius-launches-nebius-token-factory-to-deliver-production-ai-inference-at-scale>).
  Search results still mix both names, and an old blog shows `https://api.studio.nebius.ai`
  (`.ai`, not `.com`). We are **not** using that host. Docs do not clearly state whether the
  old hostname still works, which cost us time establishing.
- **Model ID casing is inconsistent across official sources** — this is the real trap:
  - Nano: `nvidia/nvidia-nemotron-3-nano-30b-a3b` (note the doubled `nvidia/`)
  - Super: `nvidia/nemotron-3-super-120b-a12b`
  - Ultra: `nvidia/Nemotron-3-Ultra-550b-a55b` (capital `N`, unlike the other two)
  Casing like this is easy to get wrong and painful to debug, so
  `spike/step0_spike.py` calls `GET /v1/models` and validates every configured ID against
  the live catalog **before** spending a token. The catalog on the account is the source
  of truth, not the doc table.
- **Context-window discrepancy for Super.** The hosted model page and the cookbook say
  256K, while the launch blog says "up to 1M". We record 256K in `config/models.json`
  until the catalog says otherwise.
- **Prices are not machine-readable.** The self-service page
  <https://tokenfactory.nebius.com/organization/prices> did not yield per-model numbers to
  a page fetch, so `config/models.json` prices come from the public model page
  (<https://nebius.com/services/token-factory/models/nvidia-nemotron-models-inference>:
  Nano $0.06/$0.24, Super $0.30/$0.90, Ultra $1.00/$3.00 per 1M in/out). **Re-check in the
  console** — a wrong price means a wrong cost display, which is a stated requirement.

### Useful discoveries

- **Nano is startlingly cheap at $0.06/$0.24 per 1M tokens.** Routing every utterance
  through Nano costs effectively nothing, so there is no reason to hand-roll a rule-based
  router for our ~$25 budget. Also means we can afford to let the router do a
  validate-and-retry-once loop on malformed JSON without thinking about cost.
- Ultra at $1.00/$3.00 is ~12x Super's output price, so the tier config matters: it must
  only fire for readiness estimates and study-time allocation, never for chat.

### Verified offline, before spending anything (fake server, $0.00)

Ran `spike/step0_spike.py` against a local server mimicking the documented response
shapes. This proves the whole pipeline except Nebius itself:

- The client sends exactly `GET /v1/models` and `POST /v1/chat/completions`, with
  `Authorization: Bearer <key>` — matching the docs character for character, including the
  `/v1` prefix. Nice not to have to guess this one.
- Cost math checks out against the published prices: 42 in / 7 out = **$0.000004** on Nano
  and **$0.000019** on Super. Ratio of ~4.5x matches the tier price ratio.
- The model-ID guardrail works: one deliberately mis-cased ID printed
  `[MISSING] nano nvidia/nemotron-3-nano-30b-a3b` and the script exited **1 without making
  a single paid call**. It refuses to guess.
- The budgets in `config/models.json` are wired through: `max_tokens=900` was present on
  every outgoing request.

### Hermes via Docker (chosen over the installer)

- `nousresearch/hermes-agent:latest` pulled fine, 3.17 GB, native **arm64** (Apple Silicon),
  digest `sha256:c69536a39e885b8943329bda68cdf44492458b96aa74a212239bde9d2aeb406a`.
  Multi-arch manifest, so the same image works on the Linux CPU instance later.
- Config keys confirmed from the docs so we don't invent them: `model.provider: custom`,
  `model.default`, `model.base_url`, `model.context_length` (not `context_window`).
- `hermes -z "<prompt>"` is the documented scripted one-shot — this matters a lot, because
  it means we can drive Hermes from a script instead of hand-operating a TUI.
- **Docs gap worth reporting:** the official Docker guide's only first-run path is an
  interactive `docker run -it ... setup` wizard. The docs never state whether that wizard
  can be skipped when the mounted data dir is already populated, so a fully headless Hermes
  container boot is undocumented. We will pre-populate `/opt/data` and find out.

### Confirmed against the live account — Step 0 LLM path PASSED

Two real calls, **$0.000060 total**:

| Tier | Reply | Tokens | Latency | Cost |
|---|---|---|---|---|
| Nano | `ROUTER OK` | 30 in / 44 out | 1.19s | $0.000012 |
| Super | a correct one-sentence explanation of spaced repetition | 31 in / 43 out | 0.82s | $0.000048 |

At ~$0.00006 per two-call exchange, the $25 budget buys roughly **16,500 such exchanges**.
Latency under 1.2s is comfortably fast enough that a spoken-answer UX will feel responsive.

**The account catalog contains exactly 4 Nemotron models:**

```
nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
nvidia/nemotron-3-super-120b-a12b
nvidia/Nemotron-3-Ultra-550b-a55b
nvidia/Nemotron-3_5-Lightning
```

### The docs are wrong about the Nano model ID (cost us one wasted call)

Nebius's own published model ID, `nvidia/nvidia-nemotron-3-nano-30b-a3b`, **does not exist**.
The first real call returned:

```
HTTP 404: {"detail":"The model `nvidia/nvidia-nemotron-3-nano-30b-a3b` does not exist."}
```

The live catalog uses **`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`** — capitalized, and with a
single `nvidia/` prefix, not the doubled one the docs table shows. This is the exact failure
the "confirm model IDs in the console before coding" rule is meant to catch, and it would
have been genuinely nasty to debug later through an agent loop, where a 404 surfaces as a
vague "the assistant didn't answer" instead of an HTTP status.

**Model IDs are case-sensitive.** That 404 is decisive evidence, not a guess.

### A bug in our own guardrail, caught by the same failure

The first version of the catalog check compared IDs **case-insensitively**, printed
`OK (exact ID is ...)`, and then let the run continue into a paid call that 404'd. A near
match is not a match. The check now requires an exact match and fails hard, distinguishing:

- `[WRONG CASE]` — the ID exists but with different capitalization, and it prints the exact
  string to use instead.
- `[MISSING]` — no such model at all.

Both block the run before spending anything. Verified offline in all three modes (exact /
wrong-case / nonexistent) with exit codes 0, 1, 1.

Worth noting: with a case-insensitive check, the docs' wrong ID had been reported as *fine*.
A guardrail that only half-guards is worse than none, because it buys false confidence.

### Not yet verified

- The Nebius CPU-instance / Serverless Endpoint hourly price for Step 7 hosting.
  Required before we deploy anything that costs money.
- Whether a Hermes container can boot fully headless (see the docs gap above).
- `nvidia/Nemotron-3_5-Lightning` is available on this account and may be cheaper/faster
  than Nano for routing. Unpriced in our config so far; worth a look later, but the three
  required tiers already work and we are not adding a fourth tier now.
