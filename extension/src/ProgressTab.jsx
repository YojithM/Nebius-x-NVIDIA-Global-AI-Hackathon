import { useCallback, useEffect, useState } from 'react'
import {
  clearProgress,
  deleteQuizSession,
  fetchProgress,
} from './api.js'

/**
 * The Progress tab.
 *
 * A study record is more sensitive than a note — it is a list of the things you
 * got wrong — so this tab exists for the same reason the Memory tab does: what
 * we keep about the student has to be visible, and removable by them. It is a
 * view over `progress/*.md` on the backend, not a separate store.
 *
 * Two kinds of "weak" are shown, and they are never added together:
 *   - what the student *said* ("I struggle with stoichiometry"), which is their
 *     own judgement; and
 *   - what actually happened when we tested it (accuracy per topic).
 * Merging them into one score would invent a number nobody said out loud.
 */

// A verdict, in the smallest form that still reads. `unclear` is one of them
// because our grading can fail, and failing to score is not the same as a wrong
// answer — pretending otherwise would dock the student for our bug.
const VERDICTS = {
  correct: { mark: '✓', label: 'correct' },
  incorrect: { mark: '×', label: 'missed' },
  skipped: { mark: '–', label: 'skipped' },
  unclear: { mark: '?', label: 'not scored' },
  unanswered: { mark: '·', label: 'not asked yet' },
}

const STATUS_LABELS = {
  open: 'in progress',
  finished: 'finished',
  abandoned: 'left part-way',
}

export default function ProgressTab() {
  const [state, setState] = useState({ status: 'loading' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      setState({ status: 'ready', data: await fetchProgress() })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function onDelete(session) {
    setBusy(session.id)
    setError('')
    try {
      // The backend replies with the whole fresh state, so there is no
      // optimistic guess to drift out of sync with the files on disk.
      setState({ status: 'ready', data: await deleteQuizSession(session.id) })
    } catch (err) {
      setError(err.message)
      load()
    } finally {
      setBusy('')
    }
  }

  async function onClearAll() {
    setBusy('all')
    setError('')
    try {
      setState({ status: 'ready', data: await clearProgress() })
      setConfirmClear(false)
    } catch (err) {
      setError(err.message)
      load()
    } finally {
      setBusy('')
    }
  }

  if (state.status === 'loading') {
    return (
      <main className="progress">
        <p className="empty">Loading your study record…</p>
      </main>
    )
  }

  if (state.status === 'error') {
    return (
      <main className="progress">
        <p className="empty">{state.message}</p>
        <button type="button" className="retry" onClick={load}>
          Try again
        </button>
      </main>
    )
  }

  const {
    sessions = [],
    total_sessions: totalSessions = 0,
    weakest = [],
    totals = {},
    declared_weak: declaredWeak = [],
    active = null,
    errors = [],
    directory,
  } = state.data

  const declaredKeys = new Set(declaredWeak.map((text) => text.toLowerCase()))

  return (
    <main className="progress">
      {active && (
        <section className="quiz-active">
          <h2>Quiz in progress</h2>
          <p>
            <strong>{active.topic}</strong> — question {active.question ?? active.total} of{' '}
            {active.total}, {active.score}. Say <em>“stop”</em> to end it.
          </p>
        </section>
      )}

      {totalSessions === 0 && !active && (
        <p className="empty">
          Nothing studied yet. Say <em>“quiz me on chapter 4, five questions”</em> and
          the results land here.
        </p>
      )}

      {totalSessions > 0 && (
        <section className="progress-stats">
          <span>{totals.quizzes} quizzes</span>
          <span>{totals.questions} questions</span>
          <span>
            {totals.correct} of {totals.answered} correct
          </span>
        </section>
      )}

      {weakest.length > 0 && (
        <section className="weak-topics">
          <h2>Weakest first</h2>
          <ul>
            {weakest.map((row) => (
              <li key={row.topic}>
                <span className="topic">{row.topic}</span>
                <span className="accuracy">{Math.round(row.accuracy * 100)}%</span>
                <span className="evidence">
                  {row.correct} of {row.asked} right
                </span>
                {declaredKeys.has(row.topic.toLowerCase()) && (
                  <span className="declared" title="You told me this yourself">
                    you said so
                  </span>
                )}
              </li>
            ))}
          </ul>
          <p className="hint">
            From your quiz answers only. Quizzes you left part-way count what you
            actually answered, not what you did not reach.
          </p>
        </section>
      )}

      {declaredWeak.length > 0 && (
        <section className="declared-weak">
          <h2>What you told me</h2>
          <ul>
            {declaredWeak.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </section>
      )}

      {sessions.map((session) => (
        <section key={session.id} className="quiz">
          <h2>
            {session.topic}
            <span className={`status status-${session.status}`}>
              {STATUS_LABELS[session.status] || session.status}
            </span>
          </h2>
          <p className="quiz-meta">
            {session.started} · {session.total}{' '}
            {session.total === 1 ? 'question' : 'questions'} · {session.score}
            {session.source ? ` · from ${session.source}` : ''}
          </p>

          <details>
            <summary>Show the questions</summary>
            <ol>
              {(session.questions || []).map((question, index) => {
                const verdict = VERDICTS[question.verdict] || VERDICTS.unclear
                return (
                  <li key={index}>
                    <p className="q">{question.text}</p>
                    {question.answer && <p className="a">you said: {question.answer}</p>}
                    {question.expected && (
                      <p className="expected">answer: {question.expected}</p>
                    )}
                    <span className={`verdict verdict-${question.verdict}`}>
                      <span aria-hidden="true">{verdict.mark}</span> {verdict.label}
                    </span>
                  </li>
                )
              })}
            </ol>
          </details>

          <button
            type="button"
            className="forget"
            disabled={busy === session.id}
            aria-label={`Delete the quiz on ${session.topic}`}
            title="Delete this quiz"
            onClick={() => onDelete(session)}
          >
            {busy === session.id ? '…' : '×'}
          </button>
        </section>
      ))}

      {error && <p className="error">{error}</p>}

      {errors.length > 0 && (
        <p className="error">
          {errors.length} progress file(s) could not be read: {errors[0]}
        </p>
      )}

      {totalSessions > 0 && (
        <footer className="memory-foot">
          {totalSessions} quizzes in plain Markdown under <code>{directory}</code> — read
          or delete them yourself, and we will pick it up.
          {confirmClear ? (
            <span className="clear-confirm">
              <button type="button" className="retry" disabled={busy === 'all'} onClick={onClearAll}>
                {busy === 'all' ? 'Clearing…' : 'Yes, delete every quiz'}
              </button>
              <button type="button" className="retry" onClick={() => setConfirmClear(false)}>
                Keep them
              </button>
            </span>
          ) : (
            <button type="button" className="clear-all" onClick={() => setConfirmClear(true)}>
              Clear my study record
            </button>
          )}
        </footer>
      )}
    </main>
  )
}
