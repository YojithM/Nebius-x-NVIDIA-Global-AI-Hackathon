# Cadence — Chrome extension

Manifest V3 side panel. Click the mic, talk, get a short spoken reply.

> **Where the API key is: nowhere in here.** This extension holds no model
> credentials and makes no calls to Nebius. It talks only to a backend over
> HTTP, and that backend holds the key in its own environment. That is the whole
> reason for the split — you can read `dist/` with devtools open and find
> nothing worth stealing.

## Build

```bash
cd extension
npm install
npm run build      # writes dist/
```

### Pointing it at a deployed backend

The panel points at `127.0.0.1:8787` unless you tell it otherwise. To talk to a
hosted backend (see [`deploy/README.md`](../deploy/README.md)) set two Vite
variables at build time:

```bash
VITE_BACKEND_URL="https://<your-host>" \
VITE_BACKEND_TOKEN="<the backend's CADENCE_TOKEN>" \
  npm run build
```

- `VITE_BACKEND_URL` is the only change needed for the URL. The panel still holds
  no **Nebius** key — that stays in the backend's environment.
- `VITE_BACKEND_TOKEN` is a token for *our* backend, a different secret. It is
  compiled into `dist/`, so **treat the built extension as a secret** and do not
  publish it. A 401 tells you the build has no token or the wrong one.
- `public/manifest.json` allows `https://*.nebius.cloud/*` in addition to
  localhost. If you used your own domain, add it to `host_permissions` or Chrome
  blocks the request before it leaves the panel.

Leave both unset and everything behaves exactly as it did before Step 7.

## Load it unpacked

1. Start the backend first (it's the other half of the loop):
   ```bash
   python3 backend/server.py
   ```
   You should see:
   ```
   Cadence backend (router mode) listening on http://127.0.0.1:8787
     skills loaded: explain, page_summary, plan, quiz, remember, rephrase, study_breakdown
   ```
   `skills loaded` is the check that matters — if it says `none`, or lists a
   `SKILL PROBLEM`, the panel will still run but every turn takes the `general`
   path.

2. Open `chrome://extensions`
3. Turn on **Developer mode** (top right)
4. **Load unpacked** → select the `extension/dist` folder
   (Yes, `dist`, not `extension`. Chrome loads built output.)
5. Click the Cadence toolbar icon — the side panel opens. Pin it if you like.

## The tabs

**Today** is the voice loop and nothing else above it. There was a **brief** box at
the top — what is due, the weak topics you named, then the next two weeks of
events — and it has been removed: it answered a question nobody had asked, on
every open, and took the top of a screen whose whole shape is a conversation. The
calendar is now something you *ask about* (see [Connecting your
calendar](#connecting-your-calendar)).

The one thing left in that space is a single **Connect Google Calendar** line,
shown only until a calendar is connected — a connection that cannot be made from
the UI is not a feature, it is a config file. Once it is connected the line is
gone, and so is every other bit of that box. Setup is in the [repo
README](../README.md#let-cadence-see-your-calendar).

Ask *"what's on my calendar this week?"* and the agent reads it with the
`read_calendar` tool and answers in words, with the dates. Ask *"what's due this
week"* or *"when is my next deadline?"* and the answer comes from your own files
**in code**: no model call, no cost — the chip on the meta line reads `brief` in
the grey server-chosen style, like `quiz`. A request that asks for work rather
than a date (*"plan my week, I have six hours"*) still goes to the skill that
does the work.

The meta line under each reply tells you what the turn
actually did, including the tools it ran: `general · 4443ms · $0.000986 · super ·
calculate · 3 steps`, or `web_search (failed)` when the search could not run.
That line is the honest half of the feature — a plan that checked nothing says
so, instead of sounding confident. A searched answer also carries its
**sources**: the reply cites them as `[1]`, each marker is a link, and the list
under it is built only from the pages the search tool actually returned — a URL
the model typed stays text, and a citation with no page behind it is dropped
rather than shown. **Log** is the backend's own action log, read back over
`/log`: one line per event, counts and reasons only, never the student's words or
a page's contents. **Progress** is what the quizzes taught us.
**Memory** is everything the assistant has kept — grouped by topic, each note
with a × to delete it, and a footer naming the actual directory on disk. Nothing
is stored in the extension: each tab is a view over files on the backend.

The Memory tab is deliberately boring. Memory you cannot inspect is
indistinguishable from memory you should not trust, so the goal is that a student
can see all of it, understand where it lives, and remove any of it without
asking us — including by editing the file themselves.

The Progress tab makes the same promise about a **more** sensitive file. A study
record is a list of the things you got wrong, so it is one plain Markdown file
per quiz under `progress/`, and the tab is a view over those files:

- an **overall row** — quizzes taken, questions asked, how many were answered,
  and a running percentage over the questions that were actually graded;
- **weakest first** — each topic with its accuracy, and the number of questions
  it is based on, so an 0%-of-2 topic is not confused with an 0%-of-20 one;
- a **quiz list**, newest first, each expanding to the exact questions, your
  words, and the verdict — with the file path shown, and a × to delete it, plus
  a **Clear all** with one confirmation.

Like the Memory tab, nothing here is invented for display: the score is the
verdicts in the file, and the weakest-first order is accuracy over graded
questions only. A quiz you started and walked away from shows its unanswered
questions as unanswered rather than as failures, and the numbers say `no answers
yet` instead of a 0% that would be a lie.

## Test the voice loop

1. Click the big mic button. Chrome will ask for microphone permission the first
   time — **allow it**.
2. Say something: *"what's due this week"*. The words appear as you speak
   (greyed out and italic while provisional), and the mic becomes a nine-bar
   waveform that follows your voice. That waveform is read from a second, muted
   capture of the same microphone — nothing is recorded, stored or sent anywhere,
   and the stream stops the moment listening does. If the second capture is
   unavailable or refused, the button keeps its rings and everything else still
   works: the meter is decoration, never a requirement.
3. When you stop, the turn goes to the backend and the reply appears and is
   spoken aloud.

   **Which voice speaks it is the backend's decision, not the panel's.** With
   `ELEVENLABS_API_KEY` set, the panel asks the backend for audio
   (`POST /speak`) and plays it — the key never enters the bundle, and the panel
   never sees it. With no key, or after a quota, rate-limit, bad-key or
   unreachable failure, Chrome's own voice reads the reply instead and the notice
   line says why it suddenly sounds different. A reply longer than 1,500
   characters is read in part, and the notice says so. **Speak again** comes from
   the backend's small cache, so repeating it is free and instant.
4. The small line under each reply shows **which skill the router picked**, then
   latency, cost, tier, and the page title that was sent:

   ```
   study_breakdown  4717ms · $0.000733 · super · page: Canvas — Organic Chemistry Midterm
   ```

   The chip is the useful part. Ask *"break lab report 3 into study blocks"* and
   it reads `study_breakdown`; ask *"what is a ketone?"* and it reads `general`.
   If it ever shows `general` in **red**, the model's routing output failed
   validation and we fell back — the tooltip says so, and `backend/logs/` has
   the detail.

If the badge in the header says `backend: offline`, the backend isn't running —
start it and reload the panel. When it's healthy it shows the backend's mode:
`backend: router`.

## No microphone? Type instead

The mic needs Chrome's speech recognition, which is missing on some builds, and it
needs a microphone you have actually granted. Neither is a reason to be stuck, so
there are three ways into the same text box:

1. **Press Type instead** — the button sits beside the mic and opens a normal
   input with an **Ask** button.
2. **It opens by itself** when the browser has no speech recognition, or when
   Chrome reports `audio-capture` / `not-allowed`. The status line says *Type your
   question below*, and a note explains why the mic is not there.
3. **Enter sends.** The box is a real `<form>`, so the keyboard works and Shift
   does not need to be hunted for a newline.

A typed question is the same turn as a spoken one: same endpoint, same router,
same reply, and it is still spoken back if you have speech synthesis. The box is
capped at 2,000 characters, which is the backend's own `MAX_TRANSCRIPT_CHARS` —
not a smaller number invented here.

## Connecting your calendar

The connection is **one permission**: `calendar.readonly`. The Connect link opens
the backend's own `/oauth/google/start` in a new tab, which sends you to Google and
then back to `http://127.0.0.1:8787` — the loopback redirect Google documents for a
desktop client. The page you land on says whether it worked and what was granted;
click back into the panel and the line is already gone, because the panel re-reads
the calendar when it regains focus.

Two states are worth knowing apart:

- **no Connect link, just a sentence** — the backend has no `GOOGLE_CLIENT_ID`, so
  a connect cannot work and the panel says what to set instead of offering a
  button that would fail;
- **Connect Google Calendar** — configured, not connected yet. After it is
  connected there is **nothing on the Today tab about the calendar at all**.

A second Google account is the same link, with `Use another account` on Google's
screen — otherwise Google reconnects the one you clicked. The link is not in the
panel once a calendar is connected (nothing is), so open
`http://127.0.0.1:8787/oauth/google/start` directly for the second one.

**Reminders.** The extension notifies you **ten minutes before a timed event**, with
the event's title and its time and calendar, and clicking the notification opens
the side panel. It runs from the service worker rather than from the panel,
because a reminder that only fires while the panel is open is not a reminder.

**How the ten minutes are kept.** Two clocks, because "ten minutes before" and
"every five minutes" are not the same promise:

- each upcoming event gets its **own one-off timer set for exactly its ten-minute
  mark**, which is what makes the timing a time rather than a range;
- a **five-minute check** keeps up with the calendar — it notices events that were
  added, moved or deleted, arms the timers for them, clears the ones for events
  that are gone, and announces anything already inside its ten-minute window.

An event you add *after* its ten-minute mark has passed is announced as soon as
it's noticed, because there is no earlier moment to remind you at. All-day events
are skipped (there is no moment to remind you at), each occurrence is announced
once, and a machine that was asleep wakes up to at most three at once. Chrome can
deliver a one-off timer a little late under load; it never fires it early. Chrome
asks for the notification permission when you reload the unpacked extension —
that is the new `notifications` permission, along with `alarms` and `storage`.

**You can see that it happened.** A reminder is the only thing in this project
that runs where nothing is watching — no panel, no request, no tab — so a pop-up
that failed to draw used to look exactly like a rule that never matched, for
everyone, including us. The worker now reports each batch to `POST /reminders`, and
the line lands in the **Log** tab: `reminder_fired · shown 1 · starts 15:19`.

Two things are deliberately missing from that line: the event's **title** and
which calendar it came from. The log has never held your words and a calendar
title is one of them, so what it keeps instead is the timing, which is the part
only the log knows.

The same tab has **Send a test reminder**, which asks the worker for one now — the
same call the alarm makes, so a green test means the real path can draw. Use it to
check the pop-up without inventing a calendar event: press it, and either a banner
appears or the sentence underneath says why not.

**One limit worth saying**: the reminders poll `127.0.0.1:8787` directly, and so
does that report. A build pointed at a deployed backend with `CADENCE_TOKEN` set
cannot authenticate from the service worker yet, so reminders are local-only for
now — and on such a backend the banner still appears while the log line does not,
which the test button says out loud rather than hiding.

If the consent screen answers *access_denied*, the usual cause is an OAuth app in
*testing* mode that does not list your Google account under **Test users**.

## The quiz loop

Say *"quiz me on this page"* or *"quiz me on chapter 4"*, or press nothing and
just say *"quiz me, five questions"* — the skill reads the page if you asked it
to, or falls back to the topics you are weakest at. Question 1 is read aloud with
its number and the total, and then you just answer.

While a quiz is open, the panel is unchanged — there is no quiz UI to learn,
because the point is that you talk. A few things to know:

- **An answer is graded; a question is not.** *"the stroma"* is graded. *"explain
  that again"* is routed to a skill, so it answers you and leaves the question
  open. That precedence is decided by the backend, not the panel.
- **Four things are free.** *"stop"*, *"repeat that"*, *"how am I doing"*, and *"I
  don't know"* are answered in code with no model call. *"I don't know"* is scored
  as incorrect (the strongest signal of a weak topic), the right answer is spoken,
  and the next question follows.
- **Everything is written down.** Watch the Progress tab or open
  `progress/<date>-<topic>-<id>.md`; each answer and verdict appears as it happens.

When the last question is answered, the score is spoken and the meta line under
it shows the session status — the same line that shows the skill, latency, cost,
and tier for any other turn.

## Reading the page you're on

Three ways to invoke it. The first two are per-question, and the third is a file
you hand over:

1. **Say it** — *"analyze this page"*, *"summarize this article"*, *"explain this
   page"*, *"rephrase this paragraph"*, *"walk me through this section"*. The panel
   recognises the page-naming words itself (`utteranceReadsPage()`), so it reads
   the tab before asking. A summary request lands on `page_summary`; one that asks
   what an idea means lands on `explain`.
2. **Press it** — **Read this page** arms the *next* question only. The button
   flips to *Reading this page ✓* and a note appears above the transcript saying
   the text will be sent **once**; after that question the arm is spent, whether
   the turn succeeded or not. When both happen they mean the same thing, so the
   button is a convenience, not a second policy.
3. **Pick a PDF** — **Read a PDF** opens the file picker and attaches the file's
   text to the conversation until **Clear**. Deliberately *not* one-shot like the
   other two: choosing a file by hand is deliberate, and re-choosing it before
   every follow-up would be busywork. The note above the transcript names the file,
   its page count and its character count, so an attached document is never
   invisible.

**A Google Doc or Sheet is read as a document, not as a page.** Docs stopped
putting the document in the DOM years ago: the text is drawn into
`kix-canvas-tile-content` canvases, and `innerText` on that page returns the
ruler, the menu bar and the collaborator names. Measured on a live document, that
was **1,152 characters of interface labels and not one word of the document** —
which is why asking about a resume came back as *"this looks like interface
labels, could you paste the passage?"*. The model was right; the extractor was
wrong. So a `docs.google.com/document/d/<id>` tab is read through the file's own
text export (`/export?format=txt`; CSV for a Sheet), fetched from the **panel**
and not from inside the page, because a content script inherits the page's origin
and is subject to CORS while an extension page with host permission is not.
`credentials: 'include'` is what makes a private document work: the request
carries your session, so the export comes back as you. A failure there is reported
as a failure rather than papered over with the toolbar.

**Text that is drawn rather than written is refused, not summarised.** Slides, PDF
viewers and canvas apps have pixels and almost no words, so the read is rejected
before any model sees it — measured as canvas coverage over half the viewport with
under 1,500 characters of DOM text — with a sentence saying so. The earlier
behaviour was to send the menu bar and let the model apologise for it.

**A PDF you pick is readable; a PDF in a tab is not.** Those are different files
as far as Chrome is concerned, and the difference is the whole reason the picker
exists:

- **In a tab**, a PDF is an `application/pdf` embed that Chrome's viewer draws
  itself — measured on a real PDF, the page holds **0 characters of text** and no
  canvas. Nothing in the DOM is the document, so the panel says "that's a PDF" and
  asks you for the passage.
- **From your disk**, press **Read a PDF** and choose the file. It is read in the
  panel by pdf.js — bundled, running locally, needing no permission and no page
  access — and the text is attached to the conversation until you press **Clear**.
  Verified on real files on this machine: a 1-page transcript came out at 2,609
  characters, a 6-page worksheet at 5,328, a Google Doc printed to PDF at 5,295.
- **A scan is refused, not summarised.** A 42-page reading that was pages of
  images yielded 83 characters of text — measured — so anything averaging under 25
  characters a page is reported as a scan and answered with "paste the passage",
  because inferring a document from a caption is inventing it.

A `file://` page (a PDF or anything else opened from disk as a tab) is still
refused before anything is attempted: Chrome blocks extensions from local files
unless you switch on *Allow access to file URLs* by hand, and switching it on would
not help a PDF. The panel deliberately does **not** offer the Allow button there,
because that button could not fix it.

pdf.js is ~430 kB and is loaded only when you pick a file (`pdfLoader.js` is a
dynamic import), so a panel that never reads a PDF pays nothing for the parser.
Its worker is bundled as an asset — no code is fetched from a network at run time,
which an extension page's CSP would forbid anyway.

The meta line tells you exactly what happened:

```
explain  4265ms · $0.000538 · super · page: Photosynthesis - Wikipedia · read 246 chars
```

`page:` is the **title** (always sent, from `chrome.tabs`). `read 246 chars` only
appears when the **body** was read — so the absence of that clause is the
verifiable statement "this turn did not read your page". The number is what came
out of the tab — `chrome.scripting`'s extraction, or a Google file's own text
export — before the backend's 12,000-character cap.

When a read fails the turn still runs, with a sentence saying why:
`unsupported` (not running as an extension), `blocked` (Chrome refused to inject
into this tab), `no-tab`, `empty` (an image or a PDF), `export` (a Google file
that would not export — signed out, or an account without access), or `canvas`
(text drawn rather than written). None of those is an error state; they are all
"answered without the page".

**A blocked read is now recoverable, and this is the fix for the bug that made
page reading look broken.** Host access is an *optional* permission
(`optional_host_permissions` for `http://*/*` and `https://*/*`), requested on a
click: **Read this page** asks for it when the arm is set, and a reply whose read
was refused shows **Allow page access and retry**, which grants it and then sends
*the same question* again — no second speech, no second copy in the transcript.
Before this, the panel relied on the `activeTab` grant from the toolbar click,
which does not survive a tab switch or a reopened panel: every real turn in the
action log arrived with no title and no page text, so a page question was always
answered blind. The grant covers the sites you ask about and changes nothing
about *when* a read happens: still only on your words or your button.

What Chrome is actually asked for: `chrome.scripting.executeScript` into the
active tab, which reads `innerText` of `article, main, [role="main"], #content`
(falling back to `body`) — a reader's view, so `<script>`, `<style>` and hidden
nodes never travel. Nothing else in the panel reads a page, and there is no
background reader: closing the panel stops it entirely.

## What's deliberately NOT here

- **No model calls from the extension.** The extension holds no credentials and
  never talks to Nebius. Model calls happen on the backend; the panel only POSTs
  to it. (The reply you hear is a real Nemotron call now — just not one made
  from in here.)
- **No uninvited page contents.** The panel always sends the active tab's
  **title** to the router, because "what's due this week" means something
  different on Canvas than on a Wikipedia page. The **body** is read only for
  the one question you asked it for — see the section above. There is no
  standing "always read pages" setting, and no code path that reads a page the
  student did not ask for.
- **A short conversation, sent with every turn.** Without it, a reply offering
  *"would you like me to show you how?"* was followed by a "yes" that had nothing
  to refer to. The panel now posts the last six messages it is showing (three
  exchanges, each trimmed) along with the question. It is derived from the
  transcript on screen — pressing **Clear** clears what is sent too — and the
  backend keeps no session of its own: it uses the messages for that one turn,
  and its action log records only how many arrived and how long they were, never
  their text.
- **And a short note of everything older.** Three exchanges is a small window for
  a study session, so once a few messages have fallen out of it the backend folds
  them into a note of at most 120 words (`POST /summarize`). The panel holds that
  note and sends it back with each turn, exactly as it does with the messages — so
  the backend still keeps no session, and **Clear** clears the note too. The
  summariser is told to keep only what the student said, decided and asked, and to
  invent nothing; the note never reaches the action log, which records its length
  and no more. Demo mode skips it entirely, because the demo's promise is a
  bounded cost per visitor.
- **No microphone or audio state in the service worker.** `background.js` wires
  the toolbar button to the side panel, runs the reminder timers and the
  five-minute check behind them,
  answers the panel when it asks for a test reminder — and holds no conversation
  state at all. MV3 service workers are killed after ~30s idle, so anything kept
  there would vanish mid-sentence. The reminder's own state is one
  `chrome.storage.local` key of the occurrences already shown, which is the whole
  reason that check can live there.
- **The confirmation loop exists, and no shipped skill needs it.** A skill that
  declares `requires_confirmation`, or a general request the router flags, is
  answered with *"Shall I…?"* and two buttons — **Yes, go ahead** and **No** — under
  the meta line's *awaiting your go-ahead*. The buttons come off the turn the moment
  one is pressed, because the backend spends an approval once; a "yes" runs the
  action that was described, not a fresh reading of whatever gets said next, and
  either answer expires after five minutes. Since none of the seven shipped skills
  declares `requires_confirmation`, you will mostly see this when the general path
  flags a request that would change something.
- **The second kind of question is a file.** When the general path drafts a skill,
  the payload's `confirmation.kind` is `"skill"`, `path` is where it would be saved
  and `draft` is the file's exact text — rendered in a monospace `<pre>` under the
  reply, because a student approving a file has to be able to read it. The button
  says **Yes, save it**, and what the backend writes is that string byte for byte:
  this side composes nothing and edits nothing. A refusal spends no model call, and
  a draft that fails the backend's own check never becomes a question at all — it
  arrives as a reply that says nothing was saved.
- **No quiz state in the extension.** What question you are on, and whether the
  quiz is still open, live in `progress/*.md` on the backend. Closing the panel
  mid-quiz loses nothing: reopen it, or ask *"how am I doing"*, and the loop picks
  up where it was.
- **No calendar writes.** Nothing in the extension or the backend can create,
  move or delete an event, because the token it holds cannot. The notifications
  read the same `/calendar` payload the panel asks for, at the ten-minute mark
  before a timed event starts, and write nothing to Google at all. What they do write is two
  things of ours: a `chrome.storage.local` key holding the occurrences already
  shown, and a `POST /reminders` line saying **how many** went up and **when the
  soonest one starts** — counts and a clock time, never a title.
- **No memory editing in the UI.** You can delete a note from the Memory tab, but
  adding or rewriting one means asking the assistant or editing the file. The
  file editing is the point, so this may stay that way.

## Gotchas

- **No hot reload.** MV3's CSP blocks the inline scripts Vite's dev server needs,
  so `npm run dev` won't work. Run `npm run watch`, then hit the reload button
  on the card in `chrome://extensions` after a rebuild.
- **`npm run build` must succeed before Chrome will load changes.** Chrome reads
  `dist/`, so an unbuilt edit is invisible.
- **The session note needs the backend restarted after a code change.** The note
  is written by `POST /summarize`, so a backend still running an older build
  answers 404, the panel keeps the messages unsent, and continuity is back to the
  message window until the process is restarted.
- **A rebuild is not a reload, and the two halves update separately.** The side
  panel is read from disk every time it opens, but the *service worker* is only
  replaced when you hit Reload on the extension card. So a newly built button can
  appear next to a worker still running the previous build — which is exactly what
  **Send a test reminder** hits, and the tab now says so in words instead of
  repeating Chrome's *“Receiving end does not exist.”* The version on the card is
  the other way to tell: if it has not gone up, the worker is old.
- **Speech recognition needs a network connection.** Chrome's Web Speech API
  streams to Google's speech service; it is not an offline API. That is also why
  the mic can't be tested in a headless browser — the automated tests stub the
  speech boundary and verify everything on our side of it instead.
- **The panel also runs as a plain web page.** Serving `dist/` over HTTP renders
  the same UI, which is what `/demo` does (`CADENCE_DEMO=on python3
  backend/server.py`, then open `/demo`). Two things change outside Chrome:
  [`src/pageContext.js`](src/pageContext.js) reads the active tab's title via
  `chrome.tabs`, so it returns no title and the turn proceeds without one — the
  meta line just omits `page:`; and `src/speech.js` finds no Web Speech API, so the
  mic does nothing and the **Read this page** button is disabled. The turn loop,
  the tabs and the cost line are unchanged. The reminders do not run here: they
  live in the extension's service worker, which a plain page does not have.
- **The backend URL follows whoever served the page.** A build-time
  `VITE_BACKEND_URL` still wins, but with none set a panel served over HTTP talks
  to the origin that served it, and an extension (where `location.protocol` is
  `chrome-extension:`) falls back to `127.0.0.1:8787`. This was a real bug: a demo
  served on port 8791 reported `backend: offline` because the built panel was
  asking 8787.
- **`activeTab` + `scripting`, plus host access asked for on a click.** The
  manifest declares `activeTab`, which Chrome grants when you click the toolbar
  icon, and `scripting` to run the one extractor function above. Neither survives
  a tab switch, which is why `http://*/*` and `https://*/*` are listed under
  `optional_host_permissions` and requested only when you press **Read this page**
  or **Allow page access and retry**. That grant is what makes a read work on a
  tab you reached after opening the panel; it is never requested at install, and
  it cannot make a read happen on its own. Chrome still refuses `executeScript`
  on `chrome://` pages, the Web Store and PDFs, so a read that Chrome blocks
  comes back as the `blocked` message above. We never ask for `tabs`, and never
  read history or every open tab.
