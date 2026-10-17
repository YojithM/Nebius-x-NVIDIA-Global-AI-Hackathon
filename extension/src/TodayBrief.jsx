import { useEffect, useState } from 'react'
import { BACKEND_URL } from './config.js'
import { fetchBrief, fetchCalendar } from './api.js'

/**
 * The morning brief (Step 8), plus the calendar half (Step 12).
 *
 * The deadines half is composed by the backend from `memory/deadlines.md` and
 * `progress/` in code — no model call — so it is free, it is the same answer
 * twice, and every date in it came from a sentence the student wrote. The line at
 * the bottom says that out loud, because "where did this come from?" is the first
 * question a brief invites.
 *
 * The calendar half is a **second request**, and that is the design rather than an
 * accident: `/brief` reads files on this machine and cannot fail, while `/calendar`
 * talks to Google and can. Two sources, two footnotes, no shared failure mode.
 * The calendar also never writes anything — not into memory, not into the
 * calendar. Events are read fresh each time, so a moved event cannot leave a
 * stale note behind.
 *
 * Three deliberate silences:
 *
 * * It renders nothing while loading. A brief is a courtesy at the top of a
 *   screen the student is already using; a spinner there would be noise on every
 *   open.
 * * It renders nothing when the backend is unreachable. The header already
 *   reports `backend: offline`, and saying it twice does not help anyone.
 * * An empty calendar section says nothing at all, because an empty week is good
 *   news — the same courtesy the deadlines half already makes.
 *
 * `refreshKey` re-reads both after every turn, so "remember my midterm is on
 * November 12" appears here a moment later — which is the moment the feature
 * starts to feel like it is keeping up rather than keeping a copy.
 */
export default function TodayBrief({ refreshKey = 0 }) {
  const [state, setState] = useState({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      // Settled rather than all: a calendar that cannot be read must not cost the
      // student the deadlines it sits next to.
      const [brief, calendar] = await Promise.allSettled([
        fetchBrief(),
        fetchCalendar(),
      ])
      if (cancelled) return
      if (brief.status !== 'fulfilled') {
        setState({ status: 'error' })
        return
      }
      setState({
        status: 'ready',
        data: brief.value,
        calendar: calendar.status === 'fulfilled' ? calendar.value : null,
      })
    })()
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  if (state.status !== 'ready') return null

  const {
    date,
    headline,
    deadlines = [],
    nudges = [],
    undated = [],
    weak_topics: weakTopics = [],
    open_quiz: openQuiz,
    counts = {},
    horizon_days: horizon,
  } = state.data

  const calendar = state.calendar || {}
  const calendarLines = calendar.lines || []
  // What the calendar half has to say, if anything. Four cases, and only two of
  // them are about events: connected with lines, connected but quiet, connected
  // but failing, and not connected at all (which is either a button or a setting
  // the operator has not filled in yet).
  const calendarNote = !calendar.connected
    ? calendar.configured === false
      ? 'Calendar off — set GOOGLE_CLIENT_ID in .env to connect a read-only Google Calendar.'
      : calendar.explanation ||
        'Calendar not connected yet — connect it and I will watch what is coming up.'
    : ''
  const showCalendar =
    calendarLines.length > 0 || Boolean(calendar.error) || Boolean(calendarNote)

  // Nothing to report and nothing to explain: the empty state of a brief should
  // take no space at all, because an empty brief is good news.
  const quiet = deadlines.length === 0 && undated.length === 0
  if (quiet && weakTopics.length === 0 && !openQuiz && !showCalendar) return null

  return (
    <section className="brief" aria-label="Today's brief">
      <h2>
        Brief
        <span className="count">{date}</span>
      </h2>

      <p className={counts.overdue ? 'brief-headline brief-urgent' : 'brief-headline'}>
        {headline}
      </p>

      {nudges.length > 0 && (
        <ul className="brief-nudges">
          {nudges.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}

      {/* From Google rather than from a file, so it is labelled as such and
          carries its own footnote. Read-only: nothing here can change an event. */}
      {calendarLines.length > 0 && (
        <>
          <p className="brief-line brief-label">From your calendar</p>
          <ul className="brief-nudges brief-cal">
            {calendarLines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p className="brief-cal-foot">
            Read live from Google Calendar{calendar.counts?.more ? ', first page' : ''} —
            read-only, never written into your files.
          </p>
        </>
      )}

      {calendar.connected && calendarLines.length === 0 && !calendar.error && (
        <p className="brief-line brief-muted">
          Nothing on your calendar for the next {calendar.horizon_days ?? horizon} days.
        </p>
      )}

      {calendar.error && (
        <p className="brief-line brief-muted">Calendar: {calendar.error.message}</p>
      )}

      {calendarNote && (
        <p className="brief-line brief-muted">
          {calendar.configured === false ? (
            calendarNote
          ) : (
            <>
              <a
                href={`${BACKEND_URL}/oauth/google/start`}
                target="_blank"
                rel="noreferrer noopener"
                className="connect"
              >
                Connect Google Calendar
              </a>{' '}
              to see what is coming up. Read-only: I can never change an event.
              {calendar.explanation ? ` ${calendar.explanation}` : ''}
            </>
          )}
        </p>
      )}

      {/*
        A list rather than one sentence, because these arrive as whole sentences
        the student wrote ("I really struggle with stoichiometry"). Joined with
        commas, two of them read as a single run-on claim about both subjects.
      */}
      {weakTopics.length > 0 && (
        <>
          <p className="brief-line brief-label">Called hard by you</p>
          <ul className="brief-nudges brief-topics">
            {weakTopics.map((topic) => (
              <li key={topic}>
                <strong>{topic}</strong>
              </li>
            ))}
          </ul>
        </>
      )}

      {openQuiz && (
        <p className="brief-line">
          Quiz open — <strong>{openQuiz.topic}</strong>
          {openQuiz.question ? ` · question ${openQuiz.question} of ${openQuiz.total}` : ''}
        </p>
      )}

      {undated.length > 0 && (
        <p className="brief-line brief-muted">
          {undated.length} note{undated.length === 1 ? '' : 's'} with no date I can read:{' '}
          {undated.map((note) => note.text).join(' · ')}
        </p>
      )}

      <p className="brief-foot">
        Read from your own files, looking {horizon} days ahead — no model call, so
        nothing here was invented.
      </p>
    </section>
  )
}
