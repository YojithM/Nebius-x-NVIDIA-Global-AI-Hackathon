import { useCallback, useEffect, useRef, useState } from 'react'
import { MAX_TURNS_IN_VIEW } from './config.js'
import {
  getRecognition,
  speak,
  speechToTextSupported,
  stopSpeaking,
  textToSpeechSupported,
  withoutCitations,
} from './speech.js'
import { answerApproval, BackendUnreachableError, checkHealth, sendTurn } from './api.js'
import LogTab from './LogTab.jsx'
import MemoryTab from './MemoryTab.jsx'
import ProgressTab from './ProgressTab.jsx'
import TodayBrief from './TodayBrief.jsx'
import {
  getPageContext,
  pageReadingSupported,
  readPageText,
  utteranceReadsPage,
} from './pageContext.js'

const STATUS_TEXT = {
  idle: 'Tap to talk',
  listening: 'Listening…',
  thinking: 'Thinking…',
  speaking: 'Speaking',
  error: 'Something went wrong',
}

const TAB_LABELS = {
  today: 'Today',
  memory: 'Memory',
  progress: 'Progress',
  log: 'Log',
}

// Why a page read failed, in words a student can act on. None of these is fatal:
// the turn still runs, just without the page.
const PAGE_READ_ERRORS = {
  unsupported:
    'I can’t read pages here — this isn’t running as a Chrome extension. Ask without the page and I’ll still answer.',
  'no-tab': 'I couldn’t find the tab you’re on.',
  blocked:
    'Chrome wouldn’t let me read this tab. Click the Cadence icon again while you’re on it, then ask.',
  empty: 'That page had no readable text — it may be an image or a PDF.',
}

/**
 * A reply, with `[1]` turned into a link to the page behind it.
 *
 * The links come from `sources`, which the backend builds from what the search
 * tool actually returned — never from the reply's own words. So a URL the model
 * typed stays plain text, and a number it invented has nothing to point at. Both
 * are the same promise: a reply can only link to a page we handed it.
 */
function citedText(text, sources) {
  const parts = String(text ?? '').split(/(\[\d{1,2}\])/g)
  if (parts.length === 1) return text
  return parts.map((part, index) => {
    const match = /^\[(\d{1,2})\]$/.exec(part)
    const source = match ? sources[Number(match[1]) - 1] : null
    if (!source) return part
    return (
      <a
        key={index}
        className="cite"
        href={source.url}
        target="_blank"
        rel="noreferrer noopener"
        title={source.title}
      >
        {part}
      </a>
    )
  })
}

// What each SpeechRecognition error actually means to a human, since the raw
// codes ("not-allowed", "no-speech") are useless in a UI.
//
// `not-allowed` is deliberately absent: it is the one error whose *remedy* is a
// place, not a sentence, so it renders as the help block below with the real
// extension ID in the URL. The text that used to be here said "click the mic
// icon in Chrome's address bar" - which is wrong for the panel this code runs
// in. A side panel has no address bar, so that instruction could not be
// followed and the student was left with a dead end and a stop sign.
const RECOGNITION_ERRORS = {
  'service-not-allowed': 'Chrome’s speech service refused the request.',
  'no-speech': 'I didn’t catch that. Try again a little closer to the mic.',
  'audio-capture': 'No microphone was found.',
  network: 'Speech recognition needs a network connection.',
  aborted: 'Listening stopped.',
}

/**
 * Where the microphone exception for *this* extension actually lives.
 *
 * Chrome keeps a media permission per origin, and an extension's origin is
 * `chrome-extension://<id>` - an ID we only know at runtime. The site-details
 * URL takes it as a query parameter, which is what makes this link-able
 * instead of a scavenger hunt through chrome://settings.
 *
 * Returns '' outside an extension (the demo page), where the generic settings
 * page is the honest answer instead.
 */
export function micSettingsUrl() {
  try {
    const id = globalThis.chrome?.runtime?.id
    if (!id) return ''
    const origin = `chrome-extension://${id}`
    return `chrome://settings/content/siteDetails?site=${encodeURIComponent(origin)}`
  } catch {
    return ''
  }
}

/** The side panel's own page, addressable as a normal tab. */
export function panelPageUrl() {
  try {
    return globalThis.chrome?.runtime?.getURL?.('sidepanel.html') || ''
  } catch {
    return ''
  }
}

export default function SidePanel() {
  const [status, setStatus] = useState('idle')
  const [interim, setInterim] = useState('')
  const [turns, setTurns] = useState([])
  const [error, setError] = useState('')
  const [backend, setBackend] = useState({ state: 'checking' })
  const [tab, setTab] = useState('today')
  // Armed by the "Read this page" button. One-shot: it applies to the next
  // question and is spent when that question is sent, so a page read can never
  // become a standing setting that quietly reads everything afterwards.
  const [pageArmed, setPageArmed] = useState(false)
  const [notice, setNotice] = useState('')
  // The open quiz, reported by the backend on every turn. The panel does not
  // decide whether a quiz is running - it shows what the backend says, so the
  // two can never disagree about whose turn it is to ask a question.
  const [quiz, setQuiz] = useState(null)
  // What the backend has spent, and this visitor's question budget (Step 10).
  // Seeded from /health on load and then updated from every turn, so the cost
  // line is populated before the first question rather than after it.
  const [spend, setSpend] = useState(null)
  const [session, setSession] = useState(null)
  // Typing instead of talking. The panel was voice-only, which quietly made it
  // useless to anyone without a microphone - and `audio-capture` is an error
  // Chrome already reports, so "no mic" was a case the code knew about and had
  // no answer for. The box opens by itself when recognition is missing or the
  // mic is refused, and the button beside the mic opens it on demand.
  const [typed, setTyped] = useState('')
  const [typedOpen, setTypedOpen] = useState(false)
  // Blocked microphone. Resolved at the moment Chrome says no, because the ID
  // in the settings URL only exists inside the extension.
  const [micBlocked, setMicBlocked] = useState(false)
  const [micCopied, setMicCopied] = useState(false)

  const recognitionRef = useRef(null)
  // The previous reply, needed by "say that more simply". A ref, because
  // runTurn is reached from SpeechRecognition callbacks registered once per
  // recording and must not close over a stale turns array.
  const lastReplyRef = useRef(null)
  // runTurn is reached from SpeechRecognition event callbacks, which are
  // registered once per recording. Going through a ref keeps those callbacks
  // from closing over a stale version of runTurn.
  const runTurnRef = useRef(() => {})

  useEffect(() => {
    const controller = new AbortController()
    checkHealth(controller.signal)
      .then((info) => {
        setBackend({ state: 'ok', info })
        // The cost display starts here, not after the first turn: what the
        // backend has already spent is a fact about it, not about this question.
        if (info?.spend) setSpend(info.spend)
        if (info?.demo?.on) {
          setSession({
            demo: true,
            used: 0,
            cap: info.demo.session_cap,
            remaining: info.demo.session_cap,
          })
        }
      })
      .catch((err) => setBackend({ state: 'down', message: err.message }))
    return () => controller.abort()
  }, [])

  /**
   * Put a reply on screen and let it be heard. Shared by both ways a turn ends
   * (a question, and an answer to a question we asked) so the two cannot drift:
   * a confirmed action updates the cost line and the quiz state exactly like an
   * ordinary reply, because it is one.
   */
  const showReply = useCallback((data, extraMeta) => {
    setTurns((prev) =>
      [
        ...prev,
        { role: 'cadence', text: data.reply, meta: { ...data, ...(extraMeta || {}) } },
      ].slice(-MAX_TURNS_IN_VIEW),
    )
    // Keep the health payload (it carries the backend's mode) - replacing the
    // whole object would blank the badge back to a generic "ok".
    setBackend((prev) => ({ ...prev, state: 'ok' }))
    setQuiz(data.quiz ?? null)
    if (data.spend) setSpend(data.spend)
    if (data.session) setSession(data.session)
    // Spoken without the citation markers: they point at links on screen, and
    // "[1]" read aloud mid-sentence is noise the student has to filter out.
    if (speak(withoutCitations(data.reply), { onEnd: () => setStatus('idle') })) {
      setStatus('speaking')
    } else {
      setStatus('idle')
    }
  }, [])

  const runTurn = useCallback(async (text) => {
    setStatus('thinking')
    setError('')
    setNotice('')
    setTurns((prev) =>
      [...prev, { role: 'you', text }].slice(-MAX_TURNS_IN_VIEW),
    )

    // Arms are spent here, whatever happens next: a page read belongs to one
    // question, and an arm that survived a failed turn would be a surprise page
    // read later on.
    const armed = pageArmed
    setPageArmed(false)

    try {
      // Read before the call, not after: this describes the page the question
      // was asked on, even if the student switches tabs while it's thinking.
      const context = await getPageContext()

      // The page BODY only when the student asked for it — with the button, or in
      // the sentence itself. This is the whole policy, and it is two lines.
      const asked = armed || utteranceReadsPage(text)
      let page = null
      if (asked) {
        const read = await readPageText()
        if (read.error) setNotice(PAGE_READ_ERRORS[read.error] || PAGE_READ_ERRORS.empty)
        else page = read
      }

      // Reported on every turn, including the ones that are not about the quiz.
      // Kept after an escape that ends the quiz: the backend sends null, and
      // this is where the line above the transcript disappears.

      const data = await sendTurn({
        transcript: text,
        pageTitle: context.title,
        pageText: page?.text,
        pageUrl: page?.url,
        pageInvoked: asked,
        previousReply: lastReplyRef.current,
      })
      showReply(data, {
        // sent_page_title is ours, not the backend's: it records what the
        // router actually received, which is what makes the meta line
        // verifiable instead of decorative.
        sent_page_title: context.title || null,
        // What we actually read, which is not always what the backend kept (it
        // caps the text): the meta line has to show the true amount.
        read_page_chars: page ? page.length : 0,
      })
    } catch (err) {
      if (err instanceof BackendUnreachableError) {
        setBackend({ state: 'down', message: err.message })
      }
      setError(err.message)
      setStatus('error')
    }
    // pageArmed is a dependency on purpose: the callback reads it to decide
    // whether to read the page, and a stale `false` here would silently drop the
    // student's Read this page. The mic callbacks reach this through
    // runTurnRef, which the effect below keeps current.
  }, [pageArmed, showReply])

  /**
   * Answer a question the backend asked (Step 10).
   *
   * A yes re-runs the action the reply described - the backend kept the decision
   * it showed, so nothing is re-routed. A no is answered in words and costs
   * nothing. Either way the buttons come off the turn: an approval is used once,
   * and leaving a live "Yes" on screen after it has been spent is how a student
   * ends up clicking a button that can only fail.
   */
  const answerTurn = useCallback(
    async (approvalId, approved) => {
      setStatus('thinking')
      setError('')
      setNotice('')
      setTurns((prev) =>
        prev.map((turn) =>
          turn.meta?.confirmation?.id === approvalId
            ? { ...turn, meta: { ...turn.meta, confirmation: null } }
            : turn,
        ),
      )
      try {
        const data = await answerApproval(approvalId, approved)
        showReply(data)
      } catch (err) {
        if (err instanceof BackendUnreachableError) {
          setBackend({ state: 'down', message: err.message })
        }
        setError(err.message)
        setStatus('error')
      }
    },
    [showReply],
  )

  useEffect(() => {
    runTurnRef.current = runTurn
  }, [runTurn])

  useEffect(() => {
    const last = [...turns].reverse().find((t) => t.role === 'cadence')
    lastReplyRef.current = last ? last.text : null
  }, [turns])

  const startListening = useCallback(() => {
    const recognition = getRecognition()
    if (!recognition) {
      setError(
        'This browser has no Web Speech API speech recognition. Use Chrome or Edge.',
      )
      setStatus('error')
      return
    }

    recognitionRef.current = recognition

    recognition.onstart = () => {
      setStatus('listening')
      setInterim('')
      setError('')
      // The mic works now, so whatever the student changed, it changed. Keeping
      // the help block up after a successful listen would be a stale complaint.
      setMicBlocked(false)
      setMicCopied(false)
    }

    recognition.onresult = (event) => {
      let finalText = ''
      let interimText = ''
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const chunk = event.results[i][0].transcript
        if (event.results[i].isFinal) finalText += chunk
        else interimText += chunk
      }
      // Words show up here while you are still speaking — this is the live
      // transcript, and it is why interimResults is on.
      if (interimText) setInterim(interimText)
      if (finalText.trim()) {
        setInterim('')
        runTurnRef.current(finalText.trim())
      }
    }

    recognition.onerror = (event) => {
      if (event.error === 'not-allowed') {
        // A blocked permission is not an error message, it is a setting that has
        // to be changed somewhere. The help block below says where that is and how
        // to get there; the plain error line stays empty so the two do not compete.
        setMicBlocked(true)
        setError('')
        setTypedOpen(true)
      } else {
        setError(
          RECOGNITION_ERRORS[event.error] || `Speech recognition error: ${event.error}`,
        )
      }
      if (event.error === 'audio-capture') {
        // No input device at all. Nothing to allow, so no help block - the box
        // is the whole remedy.
        setTypedOpen(true)
      }
      if (event.error === 'no-speech' || event.error === 'aborted') {
        setStatus('idle')
        setInterim('')
      } else {
        setStatus('error')
      }
    }

    recognition.onend = () => {
      // Ends after every utterance; only fall back to idle if we aren't already
      // busy handling the result we just received.
      setStatus((prev) => (prev === 'listening' ? 'idle' : prev))
    }

    try {
      recognition.start()
    } catch (err) {
      setError(`Could not start listening: ${err.message}`)
      setStatus('error')
    }
  }, [])

  /**
   * The two things a blocked mic needs: the exact settings URL, and a way to get
   * Chrome to ask again.
   *
   * The link is *copied* rather than opened, because Chrome refuses
   * extension-initiated navigation to its own chrome:// pages - a button that
   * silently did nothing would be a second dead end in place of the first. The
   * second button opens the panel's own page in a normal tab, which is where a
   * media prompt is shown the way most people have seen one.
   */
  async function copyMicLink() {
    try {
      await navigator.clipboard.writeText(micSettingsUrl())
      setMicCopied(true)
    } catch {
      // Clipboard access refused. The URL is on screen and selectable anyway.
      setMicCopied(false)
    }
  }

  function openPanelInTab() {
    try {
      const url = panelPageUrl()
      if (url) globalThis.chrome.tabs.create({ url })
    } catch {
      // Not running as an extension: there is no page to open.
    }
  }

  function onMicClick() {
    if (status === 'listening') {
      recognitionRef.current?.stop()
      setStatus('idle')
      return
    }
    if (status === 'speaking') {
      stopSpeaking()
      setStatus('idle')
      return
    }
    if (status === 'thinking') return // don't stack turns while one is in flight
    startListening()
  }

  /**
   * Send what was typed. Goes through `runTurnRef` for the same reason the mic
   * callbacks do: this effect-free path must not close over a stale turn, and a
   * typed question is otherwise the identical journey a spoken one takes.
   */
  const submitTyped = useCallback(() => {
    const text = typed.trim()
    if (!text || status === 'thinking') return
    setTyped('')
    runTurnRef.current(text)
  }, [typed, status])

  const lastReply = [...turns].reverse().find((t) => t.role === 'cadence')
  // Resolved per render, not at import: `speech.js` looks these up at call time
  // so the panel can be driven on a browser without a microphone.
  const canTalk = speechToTextSupported()
  const showTyped = typedOpen || !canTalk
  // Resolved only while the help block is on screen, so a plain web page never
  // computes a chrome-extension URL it cannot have.
  const micUrl = micBlocked ? micSettingsUrl() : ''
  const panelUrl = micBlocked ? panelPageUrl() : ''

  return (
    <div className={`panel status-${status}`}>
      <header className="panel-header">
        <div>
          <h1>Cadence</h1>
          <p className="subtitle">Your study assistant</p>
        </div>
        <span
          className={`backend backend-${backend.state}`}
          title={backend.info?.service || backend.message || 'checking backend'}
        >
          {backend.state === 'ok'
            ? `backend: ${backend.info?.mode ?? 'ok'}`
            : backend.state === 'down'
              ? 'backend: offline'
              : 'backend: …'}
        </span>
      </header>

      <nav className="tabs" role="tablist">
        {['today', 'memory', 'progress', 'log'].map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? 'tab tab-on' : 'tab'}
            onClick={() => setTab(name)}
          >
            {TAB_LABELS[name]}
          </button>
        ))}
      </nav>

      {/* The cost line: what the backend has spent, and - in demo mode - how
          much of the visitor's own budget is left. Shown on every tab, because
          it is a fact about the whole session rather than about Today. */}
      {spend && (
        <p className="cost-line" title="Measured from the provider's own token counts, not estimated.">
          <span className="cost">${Number(spend.cost_usd ?? 0).toFixed(6)}</span> over{' '}
          {spend.turns} {spend.turns === 1 ? 'turn' : 'turns'} ·{' '}
          {spend.remaining_credit_usd === 0
            ? 'budget spent'
            : `$${Number(spend.remaining_credit_usd).toFixed(3)} of the $${Number(
                spend.credit_usd,
              ).toFixed(2)} budget left`}
          {session?.demo && session.cap
            ? ` · ${session.remaining} of ${session.cap} questions left in this demo`
            : ''}
        </p>
      )}

      {tab === 'memory' && <MemoryTab />}
      {tab === 'progress' && <ProgressTab />}
      {tab === 'log' && <LogTab />}

      {/* Above the transcript and above the quiz line: what is due is the thing
          the student came to the tab for. Keyed on the turn count so a new
          "remember ..." shows up here without a reload. */}
      {tab === 'today' && <TodayBrief refreshKey={turns.length} />}

      {tab === 'today' && quiz && (
        <p className="quiz-line">
          Quiz — <strong>{quiz.topic}</strong> · question{' '}
          {quiz.question ?? quiz.total} of {quiz.total} · {quiz.score} · say{' '}
          <em>“stop”</em> to end
        </p>
      )}

      {tab === 'today' && pageArmed && (
        <p className="armed">
          Next question reads <strong>this page</strong> and sends its text to the
          backend once.
        </p>
      )}

      <main className="transcript" aria-live="polite" hidden={tab !== 'today'}>
        {turns.length === 0 && !interim && (
          <p className="empty">
            Ask me something out loud — <em>“what’s due this week?”</em>
          </p>
        )}

        {turns.map((turn, index) => (
          <div key={index} className={`turn turn-${turn.role}`}>
            <span className="who">{turn.role === 'you' ? 'You' : 'Cadence'}</span>
            <p>
              {turn.meta?.sources?.length ? citedText(turn.text, turn.meta.sources) : turn.text}
            </p>
            {turn.meta?.sources?.length > 0 && (
              // What the answer is based on, from the backend's `sources` - the
              // pages the search tool actually returned. Nothing else in a reply
              // becomes a link, so a URL the model made up stays plain words.
              <ul className="sources">
                {turn.meta.sources.map((source, sourceIndex) => (
                  <li key={source.url}>
                    <a href={source.url} target="_blank" rel="noreferrer noopener">
                      [{sourceIndex + 1}] {source.title}
                    </a>
                  </li>
                ))}
              </ul>
            )}
            {turn.meta?.confirmation?.kind === 'skill' && (
              // The exact bytes that would be written, not a summary of them. The
              // student is approving a *file*, and a preview that differed from
              // what gets saved would make the question a formality - which is
              // also why the draft is shown at all: nothing else on this line is
              // something they can read before it happens.
              <div className="draft">
                <p className="draft-head">
                  This would be saved as <code>{turn.meta.confirmation.path}</code>:
                </p>
                <pre className="draft-text">{turn.meta.confirmation.draft}</pre>
              </div>
            )}
            {turn.meta && (
              <span className="meta">
                {/* Which skill the router picked is the one thing Step 3 adds,
                    so it is the first thing shown — not hidden in a log. */}
                <span
                  className={`skill skill-${turn.meta.skill_source === 'server' ? 'server' : 'router'}`}
                  title={
                    turn.meta.skill_source === 'server'
                      ? 'Chosen by us, not the model — the router output could not be trusted.'
                      : 'Chosen by the router (Nemotron Nano).'
                  }
                >
                  {turn.meta.skill || 'unknown'}
                </span>
                {turn.meta.needs_confirmation && (
                  <span className="confirm">awaiting your go-ahead</span>
                )}
                {turn.meta.confirmation && (
                  // The buttons that answer "Shall I?". They come off the turn
                  // the moment one is pressed - the backend uses an approval
                  // once, so a second click could only fail.
                  <span className="approval">
                    <button
                      type="button"
                      className="approve"
                      disabled={status === 'thinking'}
                      onClick={() => answerTurn(turn.meta.confirmation.id, true)}
                    >
                      {turn.meta.confirmation.kind === 'skill' ? 'Yes, save it' : 'Yes, go ahead'}
                    </button>
                    <button
                      type="button"
                      className="decline"
                      disabled={status === 'thinking'}
                      onClick={() => answerTurn(turn.meta.confirmation.id, false)}
                    >
                      No
                    </button>
                    <span className="approval-note">
                      {/* The separator is inside the note, not after it: JSX drops
                          the newline between this element and the latency that
                          follows, and the line read "min10246ms". */}
                      this expires in{' '}
                      {Math.round((turn.meta.confirmation.expires_in_seconds || 300) / 60)}{' '}
                      min ·{' '}
                    </span>
                  </span>
                )}
                {turn.meta.latency_ms}ms · $
                {Number(turn.meta.cost_usd ?? 0).toFixed(6)}
                {turn.meta.tier ? ` · ${turn.meta.tier}` : ''}
                {/* Which tools the plan actually ran, and whether one failed. A
                    failed search is the difference between "I checked" and "I
                    could not check", so it is on the meta line, not in a log. */}
                {turn.meta.tools?.length
                  ? ` · ${turn.meta.tools.map((t) => t.tool).join('+')}` +
                    (turn.meta.tools.some((t) => !t.ok) ? ' (failed)' : '')
                  : ''}
                {turn.meta.steps > 1 ? ` · ${turn.meta.steps} steps` : ''}
                {turn.meta.steps_truncated ? ' · stopped at the step ceiling' : ''}
                {turn.meta.skill_saved
                  ? ` · saved ${turn.meta.skill_saved.path}`
                  : ''}
                {/* Over the per-request ceiling this config declares. The numbers
                    are in the reply (and were spoken); this is the glanceable
                    version, with the detail on hover rather than in the line. */}
                {turn.meta.cost_warning ? (
                  <span
                    className="cost-over"
                    title={`This turn cost $${Number(
                      turn.meta.cost_warning.cost_usd,
                    ).toFixed(4)}, over the $${Number(
                      turn.meta.cost_warning.ceiling_usd,
                    ).toFixed(4)} per-request ceiling in config/models.json. Nothing was blocked.`}
                  >
                    {' · over the cost ceiling'}
                  </span>
                ) : null}
                {turn.meta.sent_page_title
                  ? ` · page: ${turn.meta.sent_page_title.slice(0, 40)}`
                  : ''}
                {turn.meta.read_page_chars
                  ? ` · read ${turn.meta.read_page_chars.toLocaleString()} chars`
                  : ''}
              </span>
            )}
          </div>
        ))}

        {interim && (
          <div className="turn turn-you turn-interim">
            <span className="who">You</span>
            <p>{interim}</p>
          </div>
        )}
      </main>

      {tab === 'today' && error && <p className="error">{error}</p>}
      {tab === 'today' && !error && notice && <p className="notice">{notice}</p>}

      {/*
        A blocked microphone, answered with the fix rather than a description of
        it. Two things this panel knows that a generic error cannot: the
        extension's own origin (so the settings URL is exact), and the fact that
        it is a side panel - which is why "click the icon in the address bar" was
        an instruction nobody could follow here.

        Nothing here blocks typing: the box is already open by the time this is
        read, because a blocked mic is not a broken app.
      */}
      {tab === 'today' && micBlocked && (
        <div className="notice mic-help">
          <p>
            <strong>Chrome is blocking Cadence’s microphone.</strong> A side panel
            has no address bar, so the switch is on Cadence’s own page in
            Chrome’s settings — not on the extension’s Details page.
          </p>

          {micUrl ? (
            <>
              <p className="mic-url">{micUrl}</p>
              <div className="mic-actions">
                <button type="button" onClick={copyMicLink}>
                  {micCopied ? 'Copied ✓' : 'Copy this link'}
                </button>
                {panelUrl ? (
                  <button type="button" onClick={openPanelInTab}>
                    Open Cadence in a tab
                  </button>
                ) : null}
              </div>
              <p className="mic-steps">
                Paste it into a new tab → set <strong>Microphone</strong> to{' '}
                <strong>Allow</strong> → then click the mic again here.
              </p>
            </>
          ) : (
            <p className="mic-steps">
              Open <code>chrome://settings/content/microphone</code> and allow
              Cadence.
            </p>
          )}

          <p className="mic-steps">
            On a Mac also check <strong>System Settings → Privacy &amp; Security →
            Microphone → Google Chrome</strong>, and let macOS quit and reopen
            Chrome if it asks — a grant there can need a fresh Chrome to take
            effect.
          </p>
          <p className="mic-steps">
            Typing needs no permission at all, so the box below already works.
          </p>
        </div>
      )}

      {tab === 'today' && !canTalk && (
        // A notice, not an error: nothing is broken, there is just no mic in
        // this browser - and the box below is the whole remedy.
        <p className="notice">
          No speech recognition in this browser, so there’s no mic here. Type
          below instead.
        </p>
      )}

      <footer className="controls" hidden={tab !== 'today'}>
        {canTalk && (
        <button
          type="button"
          className="mic"
          onClick={onMicClick}
          aria-pressed={status === 'listening'}
          aria-label="Talk to Cadence"
        >
          <span className="pulse" aria-hidden="true" />
          <span className="pulse pulse-2" aria-hidden="true" />
          <svg viewBox="0 0 24 24" width="34" height="34" aria-hidden="true">
            <path
              d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Z"
              fill="currentColor"
            />
            <path
              d="M18 11a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.92V19H9a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2h-2v-2.08A6 6 0 0 0 18 11Z"
              fill="currentColor"
            />
          </svg>
        </button>
        )}

        <p className="status-text">
          {!canTalk && status === 'idle' ? 'Type your question below' : STATUS_TEXT[status]}
        </p>

        <div className="secondary">
          <button
            type="button"
            className={pageArmed ? 'arm arm-on' : 'arm'}
            aria-pressed={pageArmed}
            disabled={!pageReadingSupported()}
            title={
              pageReadingSupported()
                ? 'Read the text of this tab and use it for your next question — once.'
                : 'Only available when Cadence runs as a Chrome extension.'
            }
            onClick={() => setPageArmed((prev) => !prev)}
          >
            {pageArmed ? 'Reading this page ✓' : 'Read this page'}
          </button>
          {canTalk && (
            <button
              type="button"
              aria-pressed={typedOpen}
              title={
                typedOpen
                  ? 'Hide the text box and go back to talking.'
                  : 'No microphone, or you would rather not talk? Type the question instead.'
              }
              onClick={() => setTypedOpen((prev) => !prev)}
            >
              {typedOpen ? 'Hide typing' : 'Type instead'}
            </button>
          )}
          <button
            type="button"
            disabled={!lastReply || !textToSpeechSupported()}
            onClick={() =>
              lastReply &&
              speak(lastReply.text, { onEnd: () => setStatus('idle') })
            }
          >
            Speak again
          </button>
          <button
            type="button"
            disabled={turns.length === 0}
            onClick={() => {
              stopSpeaking()
              setTurns([])
              setInterim('')
              setError('')
              setNotice('')
              setPageArmed(false)
              // Clearing the transcript does not end a quiz: the backend is the
              // one holding it, and the line above comes back on the next turn.
              setStatus('idle')
            }}
          >
            Clear
          </button>
        </div>

        {/* The typed question. A form rather than a bare input so Enter sends
            it with no key handling of our own. `maxLength` matches the
            backend's own MAX_TRANSCRIPT_CHARS: the server truncates at 2000
            rather than refusing, so the box stops at the same place instead of
            letting a student paste something the far end will silently cut. */}
        {showTyped && (
          <form
            className="typed"
            onSubmit={(event) => {
              event.preventDefault()
              submitTyped()
            }}
          >
            <input
              type="text"
              value={typed}
              autoComplete="off"
              maxLength={2000}
              disabled={status === 'thinking'}
              aria-label="Type your question to Cadence"
              placeholder="Type your question — no mic needed"
              onChange={(event) => setTyped(event.target.value)}
            />
            <button
              type="submit"
              disabled={!typed.trim() || status === 'thinking'}
              title="Send what you typed (Enter)"
            >
              Ask
            </button>
          </form>
        )}
      </footer>
    </div>
  )
}
