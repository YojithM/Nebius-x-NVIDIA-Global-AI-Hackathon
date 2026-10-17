# Cadence

**A voice-first study assistant for college students.**
Click the mic, ask out loud, get a short spoken answer that actually knows your classes, deadlines, and weak topics.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
**[MIT Licensed](LICENSE)** · Built in public for the Nebius × NVIDIA Global AI Hackathon.

> **Name status:** `Cadence` is a working title, not a decision. It appears in the README title and in one config value (the assistant's name in its system prompt), so it's a two-line change later.

---

## What it is

A calm, concise study assistant that lives in your browser, talks back, and remembers what you're studying. It is deliberately built so it can handle requests it wasn't explicitly built for: a skill library for the things we hand-tune, and a general agent loop with real tools for everything else.

Ask it:

- *"What's due this week and what should I start first?"*
- *"Explain this page to me like I'm new to it."*
- *"Quiz me on chapter 4, ten questions."*
- *"Am I ready for Friday's exam?"*
- *"Tabs everywhere — group these by course."* ← a skill it wrote itself, after we taught it once

**It will not do your graded work.** It teaches, hints, and checks. That's a product rule, not a warning label: the system prompt forbids it, and there is no anti-cheating or proctoring feature anywhere in this repo.

---

## Architecture

Browser → backend on Nebius → Nemotron on Token Factory. That's the whole shape of it, and the split is deliberate: the API key never leaves the server, so a curious user with devtools open finds nothing worth stealing.

```
┌──────────────────────────────────────────────────────────────┐
│  YOUR BROWSER — Chrome extension (Manifest V3)               │
│                                                              │
│   React side panel                                           │
│    • click-to-talk mic  (Web Speech API, speech-to-text)     │
│    • spoken replies     (TTS voice)                          │
│    • reads page text / screenshots ONLY when you invoke it   │
│    • tabs: Today · Progress · Memory · Skills · Log          │
│             Permissions                                      │
│                                                              │
│   ✗ NO API KEY LIVES HERE.  Ever.                            │
│   ✓ Mic + UI logic stay in the side panel. MV3 service        │
│     workers sleep, so nothing important lives in one.        │
└───────────────────────────┬──────────────────────────────────┘
                            │  HTTPS  (session token, not a key)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  AGENT BACKEND — on Nebius (small CPU instance)              │
│                                                              │
│   • persistent memory  (classes, deadlines, scores, weak     │
│     topics, preferences — Markdown/SQLite, yours to read,    │
│     edit, and delete)                                        │
│   • skills library     (SKILL.md modules)                    │
│   • router             (picks the one best skill)            │
│   • general agent loop (plans, uses tools, max N steps)      │
│   • scheduler          (morning brief, deadline nudges)      │
│   • action log         (every tool call + cost per request)  │
│                                                              │
│   ✓ NEBIUS_API_KEY lives here, in env vars only — never in   │
│     the extension, the repo, or a log line.                  │
└───────────────────────────┬──────────────────────────────────┘
                            │  OpenAI-compatible HTTPS + Bearer
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  NVIDIA NEMOTRON — on Nebius Token Factory                   │
│                                                              │
│   Nano  → routing, tab actions, short replies                │
│   Super → explanations, quizzes, planning  (default)         │
│   Ultra → hard reasoning: readiness, study-time allocation   │
└──────────────────────────────────────────────────────────────┘
```

Every tier lives in exactly one file, [`config/models.json`](config/models.json). Change a model there and the whole app follows.

> The diagram above is the **target** architecture. The voice loop, the four tabs (Today, Memory, Progress, Log), the router, memory, page reading, the quiz loop, the scheduler and the general agent loop all exist today; the Skills tab is still to come, and so is any kind of code sandbox — see the note on `calculate` below for why that one is deliberate.

---

## Try it in 60 seconds

> **Honest status:** Steps 0–6 and 8–12 are done. **Step 12** added a read-only **Google Calendar** section to the brief: Cadence can see what is coming up and can never change an event, because `calendar.readonly` is the only permission it asks for — the [setup is below](#let-cadence-see-your-calendar). Step 11 finished with grounded citations, the agent writing its own skill, and the per-request cost warning, all described below. The voice loop, the router, persistent memory, page reading, the quiz loop, the daily brief and the general agent loop all work — ask *"break lab report 3 into study blocks"* and Nemotron Nano picks the `study_breakdown` skill, which then runs on Super; say *"remember my midterm is on November 12"* and it is written to a Markdown file you own, visible in the Memory tab; say *"quiz me on this page"* and it asks you questions, grades your spoken answers, and records the result in the **Progress** tab. The brief at the top of **Today** says what is due without spending anything, the **Log** tab shows what the backend just did, and every reply carries what it cost. A searched answer now names the pages it came from: the reply cites them by number, the links under it open the real pages, and a citation with no page behind it is dropped rather than shown. There is still **no hosted demo URL** — Step 7's deploy is blocked on the Nebius account, not on code — but there is a `/demo` mode you can run yourself, with a bundled sample course load and no extension install. And if the machine you are on has no microphone, the panel does not leave you with a dead button: press **Type instead**, or just watch the text box open by itself when the browser has no speech recognition. There are exactly **six** hand-written skills plus the router — and the agent can now draft a *new* one for you to read and approve (say *"write yourself a skill for turning a lab report into study blocks"*): the file is shown in full before it exists, nothing is written until you say yes, and the next request can use it without a restart. Everything below is real, and the fast parts take about two minutes.

**1. Clone and set up the key**

```bash
git clone https://github.com/YojithM/Nebius-x-NVIDIA-Global-AI-Hackathon.git
cd Nebius-x-NVIDIA-Global-AI-Hackathon
cp .env.example .env        # then paste your key into .env
```

Get a key at **[tokenfactory.nebius.com](https://tokenfactory.nebius.com)** (sign in with Google or GitHub). Put it in `.env` as `NEBIUS_API_KEY=...`. `.env` is gitignored and never leaves your machine.

**2. Run the spike** — no dependencies to install, it's pure stdlib Python:

```bash
python3 spike/step0_spike.py
```

It verifies the three model IDs against your live Nebius model catalog **before** spending anything, then makes one routing-shaped call on Nano and one explanation-shaped call on Super, printing tokens, latency, and cost per request. If a model ID is wrong it stops and tells you instead of guessing.

**3. Want to see it without spending a token?**

```bash
python3 spike/step0_spike.py --list-models
```

That's the free catalog check.

**4. Talk to it.** The voice loop runs end to end. In one terminal:

```bash
python3 backend/server.py
```

In another, build the extension and load it:

```bash
cd extension && npm install && npm run build
```

1. Open `chrome://extensions` and turn on **Developer mode**
2. **Load unpacked** → choose the `extension/dist` folder
3. Click the Cadence toolbar icon to open the side panel, hit the mic, and say
   *"what's due this week"* — or press **Type instead** and type it, which is the
   same turn by a different door (and the one that opens by itself on a machine
   with no microphone)

Your words appear as you speak, then the reply is spoken back. The header badge
shows the backend mode — if it reads `offline`, the backend isn't running. Full
details in [extension/README.md](extension/README.md).

Two model calls happen per turn: **Nano** routes the utterance to a skill, then
that skill runs on the tier *it* declares. (A memory write is the exception — it
needs no second call, so it costs about $0.00005 instead of $0.0007.) The line
under each reply shows what the router decided and what it cost:

```
study_breakdown  4717ms · $0.000733 · super · page: Canvas — Organic Chemistry Midterm
```

Ask a factual question — *"what is a ketone?"* — and the same line reads
`general`, because over-routing is the failure mode that matters here. A real
turn costs about **$0.0007**, so testing this is effectively free; the router
itself is roughly $0.00001.

**It remembers — but only what you ask it to.** Say *"remember that my organic
chemistry midterm is on November 12"* and it lands in `memory/deadlines.md` as a
plain line you can read, edit, or delete; the **Memory** tab lists everything
with a × on each note. Say *"my midterm is on November 12"* and nothing is stored,
because that is a remark, not a request.

That distinction is enforced in **code, not in the prompt** — and it is enforced
because the prompt-only version demonstrably failed. Our first live run saved the
statement, not just the request. `_action_blocked()` in [`backend/server.py`](backend/server.py)
now requires the utterance itself to contain an explicit ask before any write
happens, and blocks it otherwise. A model's judgement about what to keep is a
suggestion; a privacy promise has to be a rule.

**It reads the page you're on — only when you say so.** Say *"explain this page"*
or *"rephrase this paragraph"*, or press **Read this page** in the panel (a
one-shot arm, spent on the next question). Then the visible text of the tab goes
to the backend alongside your words. Say anything else and the page body never
leaves the browser: *"what is a ketone?"* on the same tab sends the title and
nothing more. The header of that policy is in [`extension/src/pageContext.js`](extension/src/pageContext.js)
and the rule itself is enforced server-side in [`backend/server.py`](backend/server.py) — text
that arrives without an invocation is refused with a 400 rather than quietly used.

The page is *data*, never instructions: it is fenced in `<<< page text (untrusted) >>>`
markers with the model told to ignore anything in it that looks like a command
(and to mention it to you instead), and it only ever reaches a skill that declared
`context: [page]`. A skill that didn't declare it cannot see it, however the turn
is routed.

**Three skills came out of Step 5**, each with its own shape of answer:

| | what it does | tier | why |
|---|---|---|---|
| `explain` | unpacks a concept, or the page you asked it to read | super | understanding |
| `rephrase` | restates a passage without changing the meaning | super | language |
| `plan` | allocates the time you actually have before a deadline | **ultra** | wrong answers here cost real hours |

Want to ask *"explain this page"* without leaving the terminal? The gate is an
HTTP contract, so one `curl` exercises it:

```bash
curl -s localhost:8787/turn -H 'Content-Type: application/json' \
  -d '{"transcript":"explain this page","page_text":"Photosynthesis converts light into sugar."}'
# omitted page_text with a bare utterance -> 400, and no model call at all
```

**Step 6 adds the one skill that talks back.** Say *"quiz me on this page"* and
`quiz` writes a set of questions, reads question 1 aloud, and grades your spoken
answers one at a time until the last one — then speaks the score and offers one
next step. The questions are written **once** (a single Super call); from then on
the order is deterministic, because *which* question comes next is the next
unanswered one in the file, not a model's guess. A model asked to "ask question
3" can ask 2 again or invent 6, and a student who is counting will hear that as a
bug.

What decides whether a turn is an *answer* is **code, not the prompt**. While a
quiz is open, the grader runs **before** the router, and the grader is allowed to
**decline**: it can return `off_topic` for an utterance that is not an attempt at
the question at all, and only then does the router run. So *"explain that again"*
mid-quiz gets an explanation and leaves the question open, while *"the stroma"*
gets graded.

We got this ordering wrong first, and it failed live and immediately:
*"plants use light to make sugar"* is a sentence **about a topic**, so a
router-first design filed it as `explain` and explained the student's own answer
back to them instead of grading it. The router is good at *which skill is this
about* and blind to *is this an answer*; the grader is the component that can
judge that, so it goes first. Grading first also makes an answer **cheaper** than
a normal turn: one Super call, and no router call at all. The rule lives next to
the page gate and the memory gate in [`backend/server.py`](backend/server.py) for the
same reason: it is a policy about the student's work, so it cannot live only in
text the model might ignore.

The four things a student says during a quiz that aren't material — *"stop"*,
*"repeat that"*, *"how am I doing"*, *"I don't know"* — are answered in code with
**zero model calls**. *"I don't know"* is worth calling out: it is scored
**incorrect** (the clearest possible signal of a weak topic, which is what the
Progress tab exists to collect), the right answer is read back, and the next
question follows.

**The same trick answers the one question the brief already answers.** *"When is
my next deadline?"*, *"what's due this week"*, *"how long until the midterm"* are
answered from `memory/deadlines.md` **in code, before the router is called**: a
plain sentence naming the date and the countdown, no model, no latency worth
measuring, $0.000000. And because that sentence is built by the same function that
composes the brief, the spoken answer and the panel above it cannot disagree —
which is the real reason it is computed rather than generated.

This exists because of a measured bug: live, *"when is my next deadline?"* was
routed to **`plan` on Ultra** and came back with *"I have your organic chemistry
midterm on November 12 from memory, but you didn't tell me how many days or hours
you can actually study before then"* — **1.9 s and $0.00098 to ask a question
nobody had asked**, about a date the student had already given us. Two layers fix
it: the code path above answers the phrasings it recognises, and the router is
told in its prompt and in `plan`'s own `not_for` that a date lookup is not a plan.
It is deliberately narrow — a request that also asks for work (*"when should I
study for the midterm"*, *"plan my week, I have six hours"*) still routes to the
skill that does that work, and a subject with no matching note falls through to
the router instead of being told "nothing is due" about something we were never
told. Both halves are pinned in [`backend/tests/test_recall.py`](backend/tests/test_recall.py).

The **Progress tab** is a view over `progress/*.md` — the same bet as memory, for
the same reason. A study record is *more* sensitive than a note; it is a list of
the things you got wrong. So it is one plain Markdown file per quiz, named for
when it happened, that you can open, read, or delete by hand:

```
progress/2026-10-14-organic-chemistry-8f2a1c3d.md
```

The file body is a worked quiz, and the frontmatter carries the score:

```
1. What is the rate-determining step of an SN1 reaction?
   - expected: loss of the leaving group to form a carbocation
   - answer: losing the chloride
   - verdict: correct
```

Two things there are deliberate. The **expected answer stays on disk** (a restart
mid-quiz must not make the open session ungradeable), and `verdict` is a closed
set that includes **`unclear`** — because grading can legitimately fail on our side,
and scoring that as wrong would punish the student for our bug while scoring it
as right would inflate their progress. `unclear` is neither; it is visible in the
file and does not count toward the score. `skipped` is the same idea: they did
not try, so it is not a wrong answer.

**Weak topics come from the evidence, not a vibe.** `/progress` ranks topics by
accuracy, worst first, and requires at least one graded question before a topic
appears at all. Ask *"quiz me on what I'm weak at"* and the topic argument is
filled from that ranking. The ranking is two numbers in a file, not an opaque
model judgement, so you can check it.

```bash
curl -s localhost:8787/progress           # the whole record: sessions, scores, weakest first
curl -s -X DELETE localhost:8787/progress # delete every quiz (what the Clear button calls)
```

Deleting is idempotent — forgetting something already gone is a 200 with
`removed: false`, not a red box, because the likely causes are a double click or
a hand-edit, and neither is an error.

**Deploying it (Step 7).** The backend is one process and it is env-driven, so
hosting it is a URL and a token, not a rewrite. Two things are worth knowing
before you try:

```bash
CADENCE_HOST=0.0.0.0 \
CADENCE_PORT=8787 \
CADENCE_TOKEN=$(openssl rand -hex 32) \
  python3 backend/server.py
```

With `CADENCE_TOKEN` set, **every route except `GET /health` requires
`Authorization: Bearer <token>`** — the backend spends your Nebius credit, so an
open URL is an open wallet. With it unset the server behaves exactly as it does
locally, which is why the panel and the test suite are unchanged. The panel gets
the URL and token at build time:

```bash
cd extension
VITE_BACKEND_URL="https://<your-host>" VITE_BACKEND_TOKEN="$CADENCE_TOKEN" npm run build
```

A `Dockerfile`, a systemd unit, a Caddy TLS config and a full runbook live in
[`deploy/`](deploy/README.md). **Read the cost section of that runbook first**:
the cheapest always-on CPU instance on Nebius is about **$44–48/month**, which is
more than this project's entire ~$25 budget, so the recommended deployment is one
you stop when you are not demoing.

**Until the deploy unblocks:** there is a demo you can run locally with no cloud
account at all, in two commands:

```bash
npm --prefix extension run build                 # the panel, as a page
CADENCE_DEMO=on python3 backend/server.py        # then open 127.0.0.1:8787/demo
```

That serves the same React panel with no extension installed, on a thrown-away
copy of [`demo/`](demo/README.md)'s sample course load, and gives each visitor 20
questions. What it is **not** is a hosted URL — see the deploy notes above for why
that one is waiting on Nebius.

---

## Let Cadence see your calendar

The brief already knows what is due, because you told it in a sentence. A calendar
knows what is *scheduled*, which is the other half of the same week — so the brief
has a second section that reads it live.

**The promise is one sentence: Cadence may look at your calendar and may never
change it.** That is not a policy written into a prompt, it is the credential. The
only scope requested anywhere in this project is

```
https://www.googleapis.com/auth/calendar.readonly
```

so a bug in the backend, holding the token, could still not create, move or delete
an event. The action log records `calendar_connected read_only=true` from the scope
Google actually granted — a fact read back from the token response, not a claim we
make in a comment.

**Setting it up** (once, about three minutes):

1. [Google Cloud console](https://console.cloud.google.com) → pick a project →
   **APIs & Services → Library → Google Calendar API → Enable**.
2. **OAuth consent screen** → *External* → name the app → add your own Google
   account under **Test users**. An app in testing mode only works for the accounts
   listed there; this is the step that produces `access_denied` when skipped.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy the
   client ID into `.env`:

   ```bash
   GOOGLE_CLIENT_ID=....apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...        # optional for a desktop client
   ```

4. Restart the backend and press **Connect Google Calendar** in the brief (or open
   `http://127.0.0.1:8787/oauth/google/start`).

**What happens on the way back.** Google redirects to the loopback redirect its docs
specify for a desktop client — `http://127.0.0.1:8787`, no path — so the answer
lands on `/`, which is also where the panel is served. The two are told apart by
the query and nothing else: a `code` or an `error` is Google, anything else is the
panel. That route is the one door answered *before* the bearer-token check, because
a browser redirect cannot carry a header; what guards it instead is a `state` we
generated (single use, ten minutes, 32 of them at most) and PKCE, whose verifier
lives only in this process's memory. The reply is a plain page that says what was
granted, in words: read-only.

**Where it shows up.** Under the deadlines, labelled `From your calendar`, one line
per event — `tomorrow — Chemistry lab (14:00, Cavendish 2.14)` — read fresh, capped
at ten, looking seven days ahead like the deadlines beside it. It is a separate
request (`GET /calendar`) so a slow or unreachable Google cannot delay them, reused
for a minute, and **never written into memory**: a moved event cannot leave a stale
note behind. `/health` reports `calendar.configured` and `calendar.connected` as
two separate booleans, because "no OAuth client on this machine" is a setup problem
and "not connected yet" is a button.

**What it costs:** nothing. The lines are composed in code from Google's JSON, the
same way the deadline lines are — no model call, `cost_usd: 0`.

**The honest limits:**

- **It is local-only, by construction.** The redirect is `127.0.0.1`, so the consent
  flow has to run on the machine running the backend. A server can *hold* a token
  connected locally (copy `data/google-token.json` plus the same client ID), but it
  cannot run the flow itself — see [`deploy/README.md`](deploy/README.md).
- **It reads one calendar** — the primary one — and no other Google product.
- **It shows, it does not notify.** There is no push, no desktop notification, no
  alarm. The nudge is a brief you have to open, which is the half of "remind me"
  this step did not build.
- **A read can be up to a minute stale**, deliberately, because the panel asks on
  every open and after every turn.
- **Disconnect** with `curl -X DELETE http://127.0.0.1:8787/calendar`, or by deleting
  `data/google-token.json`. The file is written `0600`, is gitignored, and never
  leaves the machine.

---

## How we use NVIDIA Nemotron

All AI inference in this project goes to NVIDIA Nemotron models on Nebius Token Factory. No other LLM provider is used for any core feature. (Browser speech-to-text and a text-to-speech voice are allowed helpers — those are browser capabilities, not model providers.)

The interesting part isn't that we call Nemotron. It's *which* Nemotron, and why.

| Tier | Model ID | Used for | Price / 1M tokens (in → out) |
|---|---|---|---|
| **Nano** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Routing, tab actions, short replies, JSON-shaped classification | $0.06 → $0.24 |
| **Super** | `nvidia/nemotron-3-super-120b-a12b` | Explanations, quizzes, planning — **the default** | $0.30 → $0.90 |
| **Ultra** | `nvidia/Nemotron-3-Ultra-550b-a55b` | Hard reasoning only: readiness estimates, study-time allocation, genuinely tough concepts | $1.00 → $3.00 |

Three things this table is doing:

**1. Nano makes routing free, so the router is a real model.** At $0.06/$0.24 per million tokens, classifying every single utterance costs a rounding error. That means the router can be a genuine model call with a strict JSON contract — `{"skill": ..., "args": {...}, "needs_confirmation": true|false}` — with validate-and-retry-once on malformed output, instead of brittle keyword matching. We would not have made that trade at 20x the price.

**2. Ultra is ~12x Super on output, so it's fenced in.** It fires only for readiness estimates and study-time allocation, where a wrong answer is worse than a slow one. Chat never touches it. The fence is a config value, not a convention — a tier is named at every call site, and the default is Super.

**3. Super is the default because most of the product is explaining and quizzing.** Which is exactly what a 120B model is good at, and what the budget can afford.

**Model IDs are a trap we already fell into, which is why they're verified rather than trusted.** The ID Nebius publishes for Nano, `nvidia/nvidia-nemotron-3-nano-30b-a3b`, **does not exist.** Our first call returned `HTTP 404: The model ... does not exist`. The live catalog uses `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` — capitalized, with a single `nvidia/` prefix rather than the doubled one the docs table shows. The three IDs above are the ones actually serving requests on our account.

**And model IDs are case-sensitive** — that 404 is decisive evidence, not a guess. So the spike validates every configured ID against `GET /v1/models` on your account *before* it spends a single token, requires an **exact** match, and refuses to run if one is off — reporting `[WRONG CASE]` with the exact string to use, or `[MISSING]` if no such model exists. The live catalog is the source of truth, not the docs.

More of this in [`FEEDBACK.md`](FEEDBACK.md).

---

## How we use Nebius

Two separate jobs, and it's worth keeping them straight:

**1. Nebius Token Factory — all inference.**
OpenAI-compatible, which matters: it means the standard OpenAI client works with nothing but a `base_url` swap, so there's no bespoke HTTP layer to maintain.

```python
base_url = "https://api.tokenfactory.nebius.com/v1"
# POST /v1/chat/completions, GET /v1/models
# Authorization: Bearer $NEBIUS_API_KEY
```

`GET /v1/models` is used as a **capability check**, not just model discovery: the spike calls it before every run, and the backend will call it at startup, so a renamed or deprecated ID fails loudly and cheaply up front instead of mid-conversation while a student waits on an answer.

**2. Nebius compute — the always-on backend.**
The agent backend needs persistent memory and a scheduler, so it needs to actually stay running. Target is a small CPU instance or a Serverless Endpoint. This is a deliberate architectural constraint: **the backend does not need a GPU.** All model compute happens at Token Factory, so we're not renting silicon to sit idle between requests.

**Confirmed, and it is a blocker.** The cheapest always-on CPU instance is a 2 vCPU / 8 GiB preset at **$0.066/hour on `cpu-d3` (~$48/30 days)** or **$0.060/hour on `cpu-e2` (~$44/30 days)**; a Serverless Endpoint on the same preset costs the same per hour and has no documented scale-to-zero. That is more than the entire ~$25 budget, so an always-on deployment does not fit and we are not going to pretend it does.

The way out is a deployment you switch off: **a stopped VM is not charged for compute** (storage keeps costing a little), so the honest demo deployment is a VM you start before a demo and stop after — roughly **$0.07 per demo hour**. We verified these numbers against the official pricing pages, and the runbook still says to confirm the live figure in the console before creating anything. See [`deploy/README.md`](deploy/README.md).

**Secrets discipline.** The key exists in exactly one place: the backend's environment. It is not in the extension, not in the repo, not in a log line, and not in this README. `.env` is gitignored at the repo root, and the spike prints `(value never printed)` rather than echoing it. Error messages carry the URL and HTTP status only — never headers.

**Budget discipline.** Roughly $25 total, so the guardrail values are declared up front in `config/models.json`: a max-steps ceiling per request, a token budget per request (900 output tokens / 24k input by default), a per-session request cap on the public demo, and a per-request cost ceiling that triggers a warning. The spike already applies the output-token budget on every call and prints cost per request.

> **Now enforced (Step 9):** the step ceiling. `max_steps_per_request` bounds the whole request, tool steps included, and the general agent loop reserves its **last** step for a tool-free wrap-up call, so a run-on plan can overshoot it by exactly nothing. The meta line under each reply reports what the plan spent: `general · 4443ms · $0.000986 · super · calculate · 3 steps`.
>
> **Now enforced (Step 10):** the per-session request cap and the cost display. In `/demo` mode each client address gets `max_requests_per_session_demo` questions (20), charged *before* anything is routed, so a refused question costs nothing; the panel shows what the backend has spent, from the provider's own token counts, on every tab. Measured live: one `general` turn came to **$0.001057**, and the line read `$0.001057 over 1 turn · $24.999 of the $25.00 budget left · 19 of 20 questions left in this demo`.
>
> **Now enforced (Step 11):** the per-request cost warning, the last number in `config/models.json` that nothing read. A turn that costs more than `warn_over_usd_per_request` (5 cents) says so **in words the student hears** — the sentence is appended to the reply, because the panel speaks the reply and nothing else: *"One thing worth saying: that turn cost $0.0612, over the $0.0500 I watch for. Nothing was blocked - I would rather you heard it here than on a bill."* It warns and stops there; the meta line carries a red `over the cost ceiling` badge with both numbers on hover, and the log gets one `cost_warning` line with the amounts. A missing or misspelled ceiling falls back to the default rather than switching itself off, and an explicit `0` turns it off deliberately. What is still **not** built, in `config/models.json`'s own words now: `total_credit_usd` is reported and not enforced (nothing stops spending when it runs out), `max_input_tokens_per_request` is read by nothing at all (the prompt is bounded in pieces rather than as a total), and the request cap bounds *casual* use rather than an adversary — a visitor who changes address gets a new budget, which is why the deploy runbook still says to leave a public demo switched off when you are not watching it. The **Log tab is real** (`GET /log`).

**Nebius AI Studio is now Nebius Token Factory** — same platform, new name. Older search results and blog posts still use the old name and an old `api.studio.nebius.ai` hostname which we do not use.

---

## Setup (full)

**Prerequisites**

- Python 3.9+ (developed on 3.9.6 — the spike is pure stdlib, no `pip install` needed)
- Node.js 20+ and npm (for the extension, from Step 2 onward)
- Docker Desktop (optional, only if you want to run the Hermes Agent spike)
- A Nebius Token Factory account with credit

**Environment**

```bash
cp .env.example .env
```

| Variable | Required | What it's for |
|---|---|---|
| `NEBIUS_API_KEY` | yes | All Nemotron inference. Backend only. |
| `CADENCE_DEMO` | no | `on` serves the panel at `/demo` on a bundled sample course load, capped per visitor. Refuses to start if `CADENCE_TOKEN` is also set — a public demo cannot require a token. |
| `EXA_API_KEY` | no | Web search for the general agent loop (Step 9). **The one we'd pick** — [dashboard.exa.ai](https://dashboard.exa.ai/api-keys), free monthly credits. |
| `TAVILY_API_KEY` | no | The alternative search provider: [app.tavily.com](https://app.tavily.com/home). Exa wins if both are set. |
| `GOOGLE_CLIENT_ID` | no | Read-only Google Calendar in the brief. A **Desktop app** OAuth client; without it the calendar section is simply absent and no request is made. See [Let Cadence see your calendar](#let-cadence-see-your-calendar). |
| `GOOGLE_CLIENT_SECRET` | no | Only if your OAuth client issued one — a desktop client is a public client, and requiring a secret here would break a correct setup. |
| `CADENCE_GOOGLE_TOKEN` | no | Where the refresh token is kept. Default `data/google-token.json` (gitignored, `0600`). |

Without either key the loop still answers — it just reports what it could not check, and `/health` says so under `tools.search.configured` rather than pretending otherwise.

**Run the spike**

```bash
python3 spike/step0_spike.py              # full check: verify IDs, then two real calls
python3 spike/step0_spike.py --list-models  # free: just verify model IDs
```

**Run Hermes Agent (Step 0 experiment, optional)**

Hermes is the open-source agent from Nous Research, and it's the backend we're building on — it gives us persistent memory and agent-authored skills out of the box. It runs in Docker with its data directory mounted at `/opt/data`, pointed at Token Factory as a custom OpenAI-compatible endpoint.

Unlike the official Docker guide, **no interactive setup wizard is needed.** The container bootstraps itself headlessly on first run and generates its own `config.yaml`:

```bash
mkdir -p spike/hermes/data

# one-time: point it at Token Factory (config lives in the mounted data dir)
docker run --rm -v "$PWD/spike/hermes/data:/opt/data" \
  --entrypoint sh nousresearch/hermes-agent:latest -c '
    hermes config set model.provider custom
    hermes config set model.default nvidia/nemotron-3-super-120b-a12b
    hermes config set model.base_url https://api.tokenfactory.nebius.com/v1
    hermes config set model.context_length 262144'
```

The repo ships a runner that reads your Nebius key from `.env` and hands it to the container through the environment (never a command line):

```bash
./spike/hermes/hermes.sh -z "Reply with exactly: HERMES OK"
```

`memories/USER.md`, `memories/MEMORY.md` and `skills/` all live under `/opt/data` as plain Markdown you can read, edit, or delete.

> **Gotcha worth knowing if you use Nemotron anywhere else:** every Nebius Nemotron model ID starts with `nvidia/`, and Hermes ships a built-in provider literally named `nvidia` (NVIDIA NIM / build.nvidia.com). So `hermes -m nvidia/nemotron-3-super-120b-a12b` without an explicit provider resolves to the *wrong* provider and fails asking for `NVIDIA_API_KEY`. Set `model.provider: custom` explicitly — and note that `--provider custom` on its own also fails unless `base_url` is in `config.yaml`, since the endpoint is not taken from environment variables alone.

**Repo layout**

```
config/models.json   the one place model tiers, prices, and budgets are configured

spike/               Step 0: prove the Nemotron connection before building on it
  nemotron.py        stdlib-only Token Factory client (also computes cost per request)
  step0_spike.py     runnable spike: verify model IDs, then make real calls
  hermes/            Hermes Agent spike (runs in Docker against Token Factory)

backend/             the agent backend (Steps 2+). Pure stdlib, no pip install needed    server.py          HTTP server: /health, /skills, /memory, /turn, /progress, /brief
  router.py          picks one skill (Nano), validates the JSON, retries once, falls back
  skills.py          loads SKILL.md files into validated skills — no Python to add one
  memory.py          the Markdown memory store: one file per topic, atomic writes
  quiz.py            the quiz store: one Markdown file per quiz, atomic writes,
                     with the score in the frontmatter and the talk in the body
  frontmatter.py     strict dependency-free YAML-subset parser for SKILL.md headers
  persona.py         the system prompt, kept in one readable string    nemotron.py        Token Factory client + cost per request
    scheduler.py       what is due: dates read from your notes, the morning brief
  tests/             one script per module, run one at a time, e.g.
                     python3 backend/tests/test_quiz.py
                     test_smoke.py starts the real server against a stub model

skills/              skills as SKILL.md files, not code
  explain/           explains a concept, or the page you asked it to read
  rephrase/          restates a passage: yours, the page's, or its own last reply
  plan/              fits work into the time you have before a deadline (Ultra)
  study_breakdown/   the reference skill: proves router -> args -> prompt end to end
  remember/          the only skill that writes: files an explicit request to memory/
  quiz/              asks questions over several turns, grades each answer

memory/              YOUR data, plain Markdown, one file per topic. Read and edit it.
  README.md          the file format, and how to delete things by hand

progress/            YOUR quiz record, plain Markdown, one file per quiz. Read it.
                     The Progress tab is just a view over these files.

extension/           Chrome MV3 side panel (React) — see extension/README.md
  src/               mic, live transcript, spoken replies: all in the panel
  public/manifest.json
  scripts/make_icons.py   regenerates the icons from math, no binaries in git
  dist/              build output. THIS is the folder you load unpacked.

deploy/              Step 7: how to host the backend. Read the cost section first
  README.md          the runbook: VM and Serverless Endpoint, verify, tear down
  cloud-init.yaml    VM bootstrap (Docker + systemd unit)
  cadence.service    systemd unit; secrets come from /etc/cadence.env
  Caddyfile          automatic TLS in front of 127.0.0.1:8787
Dockerfile           the backend in a container. Pure stdlib, so no pip install

FEEDBACK.md          our running notes on the Nebius and NVIDIA tools
LICENSE              MIT
```

**Build order** — we build in public, one step at a time, and stop to test each one:

`0` spike · `1` repo setup · `2` voice loop · `3` router + skills registry · `4` memory + Memory tab · `5` explain / rephrase / plan · `6` quiz + Progress · `7` deploy to Nebius · `8` scheduler · `9` general agent loop + sandboxed code exec + skill creation · `10` permissions, action log, cost display, demo mode · `11` stretch skills. Feature freeze Oct 26.

**Done so far: 0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11.** Step 7's code is done and verified locally — the token gate, the container image, the remote-mode checks and the cloud-init all pass — but the deployment itself is **not live**, and the reason is now known precisely: the Nebius account authenticated as its own **owner** can read everything and **create nothing**. VPC, Container Registry and Compute all return `PermissionDenied` on write while reads succeed, on a fresh token, on a tenant created minutes earlier. That is an account activation problem (billing or provisioning state), not a role, an id or a runbook problem — so it is waiting on Nebius, not on code.

Everything needed to finish the moment that unblocks is committed and verified: [`Dockerfile`](Dockerfile), [`deploy/cloud-init.yaml`](deploy/cloud-init.yaml), the runbook in [`deploy/README.md`](deploy/README.md), and [`deploy/preflight.py --probe-writes`](deploy/preflight.py) which reproduces the exact failure in one command and exits 1. Measured cost, from Nebius's own price calculator rather than the pricing pages: **$0.066/hour**, **$48.18/month** if left running, and **$2.13/month** for the 30 GiB disk — so a stopped-when-idle VM is cents per demo. Step 8, the scheduler, is **done**: `GET /brief` and the card at the top of the Today tab report what is due and how soon, composed in code from `memory/deadlines.md` — free, identical twice, and unable to invent a deadline, with undated notes reported as undated rather than guessed at. The other half is the wake-up: the server logs the day's brief into `backend/logs/action-log.jsonl` on its own, once, at 07:00 — observed live, one line then silence (`ts`, `event`, and counts — `dated`, `overdue`, `due_soon`, `undated` — never the words of a deadline). Step 9, the general agent loop, is **done and measured**: `POST /turn` on the `general` path can now plan, call a tool, and answer from the result. Two tools ship, both read-only — `web_search` (Exa or Tavily) and `calculate`, a whitelist arithmetic evaluator that is deliberately *not* a code sandbox, because a loop that reads untrusted web text must never be able to execute a string a model composed from it; the reasoning is in [`backend/tools.py`](backend/tools.py). **Two thirds of this step's own label are not built, and we would rather say so than let "Step 9 done" imply them:** there is no code sandbox at all (so nothing executes model-authored code), and the model cannot yet author a skill file — the reviewed-allowlist machinery that makes that safe is in place and every action goes through it, but there is no write path for it. What *is* built and measured is the loop, the two tools, the enforced ceiling, and the cost/step reporting. The first version of the prompt was inert — "what is 15 percent of 240" was answered from memory 3 times out of 3 and the calculator was never used — so the rules were rewritten and re-measured (3/3 tool use, while a definition still costs one answer call). Two more real bugs came out of running it: a failed search that the model filled from memory, and a deadline answered as **49 days** where code says **28** — the plan is now handed the count that [`scheduler.deadlines()`](backend/scheduler.py) computes, and answers 28 three times out of three. Step 10 is **done and measured**, and it is the step where the app stopped being a demo and started being accountable. Four things shipped: **the confirmation loop** (a skill that declares `requires_confirmation`, or a general request the router flags, now asks and can be *answered* — the panel renders Yes/No, the backend stores the decision so a "yes" runs the action that was shown rather than re-routing the word, approvals are single-use and expire in five minutes); **the action log** (`GET /log`, a bounded tail read, shown in a new **Log** tab); **the cost display** (`$0.001057 over 1 turn · $24.999 of the $25.00 budget left`, summed in integer micro-dollars so it cannot drift); and **`/demo` mode** (the panel served as a plain page on a thrown-away copy of [`demo/`](demo/README.md), 20 questions per visitor, no token by construction). Three defects came out of driving it in a real browser rather than trusting the tests. The panel asked `127.0.0.1:8787` no matter which port served it, so a demo page read `backend: offline` while the backend was right there — it now inherits the origin that served it. The `plan` skill answered a spoken question with **3,659 characters of its own deliberation**, cut off mid-word at the output budget, and the panel was about to read it aloud; a reply that hits `finish_reason: length` is now asked again briefly with thinking off, and if both attempts run out of budget the student is *told* rather than read a scratchpad. And the demo showed the operator's own log, which is safe only because of the Step 8 rule that the log holds counts and never the student's words — read back from the endpoint by a test. Step 11 is **in progress**, and its first slice is **grounded citations** — the stretch goal that was missing without anyone noticing, because nothing in the app ever looked broken. `web_search` had always returned titles and URLs; the reply never named them, so a searched fact was *unverifiable*: the student got a number, our word for it, and no way to tell a page we actually read from a sentence the model produced from memory. Now the tool returns the pages it read, the loop numbers them once per turn (so two searches do not produce two lists of "1."), the model is told which numbers exist and asked to cite them, and the panel turns `[1]` into a link to `sources[0]`. The rule that makes it safe is that **links come from the tool's list and never from the reply's words**: a URL the model types stays text, and a marker pointing at no page is removed before the student sees it. Measured against the running panel rather than only the tests — with the search provider's *response* stubbed at the HTTP seam, because this machine has no search key — a live *"what is the latest version of python?"* answered `general`, 3 steps, one `web_search`, **$0.001170** in 4.5s, with the reply citing `[1]`, all three links under it, the citation opening the real page, and the **spoken** text carrying no markers at all. Step 11's **second slice is the agent writing its own skill** — the other third of Step 9's own label. The loop gains a third tool, `propose_skill`, read-only like the other two: it checks the file the model wrote against the registry's own rules and hands it back. The write happens nowhere near it — [`skillwriter.save()`](backend/skillwriter.py) has exactly one caller, the Step 10 approval path — so the student sees the file's exact text in the panel, in monospace, and the Yes button is the only thing in the system that can create a skill. What gets written is what was shown, byte for byte (1099 characters drafted, 1099 bytes on disk), and the registry is re-read from the same directory, so *saved* means the next request can be routed to it rather than the next restart. **A file written this way may only say things**: `action` must be `prompt`, refused at both layers, because the thing the student approved is a text preview rather than a reviewed frontmatter — the tasks with side effects stay hand-written. Four defects came out of running the real model rather than the tests: the tool card *described* the format and the model got it wrong four times in a row (the card now carries a complete worked SKILL.md, and the first draft after that change was valid), three of the first four phrasings were captured by `remember`, `plan` or `study_breakdown` before they could reach the tool (the router prompt and three `not_for` fields now say authoring is always `general`), one reply wrapped our own protocol object in prose and the panel read it out loud (`parse_step` refuses it without ever acting on it), and a screenshot found a meta line reading `this expires in 5 min10246ms`. Measured live in the panel on :8791 against the backend on :8787: a spoken authoring request → `general · 6961ms · $0.002063 · super · propose_skill · 3 steps` with the draft rendered under the reply; **Yes, save it** → *Saved: turn_lab_report_into_study_blocks … $0.000000 · super · saved skills/…*; `/skills` listing 7 skills with `errors: []`; the Log tab reading `skill_written · chars 1099 · skills 7 · added 1`; and the new skill answering *"turn my lab report into study blocks"* on the very next turn.

Next up, and there is no step left to name: what remains is the three things that are blocked or deliberate rather than unfinished. Step 7's deploy is **written, verified and not live** because the Nebius account can read and cannot create — that is waiting on Nebius, not on code. The code sandbox stays **deliberately unbuilt**: nothing here executes model-authored code, and [`backend/tools.py`](backend/tools.py) explains why `calculate` is a whitelist evaluator rather than a sandbox. And `max_input_tokens_per_request` is the one budget number left that nothing reads — found while wiring the warning next to it, and now stated in the config rather than waiting to be discovered a third time.

Tests are plain `unittest` and each module runs on its own:

```bash
for f in backend/tests/test_*.py; do python3 "$f" || break; done   # 534 tests
```

The last of those is different in kind: every other suite imports the backend and
drives the handler directly, so none of them proves that
`python3 backend/server.py` actually starts, loads `skills/`, binds a port and
answers. `test_smoke.py` copies the app into a temp tree, points the model at a
local stub, starts the real process and talks to it over HTTP — a 401 there is
checked against the stub having received no call at all.

---

## Feedback

We're keeping a running, honest log of what it's actually like to build on these tools — what worked, what the docs got wrong, and where we lost time — in **[`FEEDBACK.md`](FEEDBACK.md)**.

It's the most useful thing in this repo for anyone else building on Nebius or Nemotron. It's organized newest-first by date, and it does not flatter anyone.

Already logged: **the model ID Nebius publishes for Nemotron 3 Nano does not exist** — the live catalog uses a differently-cased one, and the wrong ID returns HTTP 404 at call time; model IDs are case-sensitive; `/v1/models` returns no context-window or pricing metadata, so both have to be maintained by hand; a context-window discrepancy between two official pages; the AI Studio → Token Factory rename; the `nvidia/` prefix colliding with Hermes' built-in NVIDIA provider; a bug in our own validation that let a bad ID through to a paid call; from the quiz, that we still have no documented JSON-schema output mode to lean on, so multi-field JSON stays a retry-and-validate problem; and, from the deploy step, that the doc pages and the public prices page **disagree about the cheapest CPU instance** ($0.13/hour vs $0.06/hour) and that the always-on price is several times the budget—plus a free-trial page that says the trial was suspended and new sign-ups are closed. That entry also records two bugs found *after* the deploy work looked finished and green: a session id like `582e6104` was silently read as the number `inf` (a test that failed one run in eight was telling the truth, not flaking), and `CADENCE_TOKEN` in `.env` was read before `.env` was loaded, which would have brought the deployed backend up with the gate switched off.

Found something we got wrong, or hit a wall we haven't? Open an issue, or add a dated entry to `FEEDBACK.md`.

---

## Built with

Chrome Extensions (Manifest V3) · React · Web Speech API · Python · NVIDIA Nemotron on Nebius Token Factory · Nebius compute · MIT licensed
