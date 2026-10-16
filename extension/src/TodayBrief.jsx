import { useEffect, useState } from 'react'
import { fetchBrief } from './api.js'

/**
 * The morning brief (Step 8): the top of the Today tab.
 *
 * This is the scheduler's first half. The backend composes it from
 * `memory/deadlines.md` and `progress/` in code — no model call — so it is free,
 * it is the same answer twice, and every date in it came from a sentence the
 * student wrote. The line at the bottom says that out loud, because "where did
 * this come from?" is the first question a brief invites.
 *
 * Two deliberate silences:
 *
 * * It renders nothing while loading. A brief is a courtesy at the top of a
 *   screen the student is already using; a spinner there would be noise on every
 *   open.
 * * It renders nothing when the backend is unreachable. The header already
 *   reports `backend: offline`, and saying it twice does not help anyone.
 *
 * `refreshKey` re-reads it after every turn, so "remember my midterm is on
 * November 12" appears here a moment later — which is the moment the feature
 * starts to feel like it is keeping up rather than keeping a copy.
 */
export default function TodayBrief({ refreshKey = 0 }) {
  const [state, setState] = useState({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const data = await fetchBrief()
        if (!cancelled) setState({ status: 'ready', data })
      } catch {
        if (!cancelled) setState({ status: 'error' })
      }
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

  // Nothing to report and nothing to explain: the empty state of a brief should
  // take no space at all, because an empty brief is good news.
  const quiet = deadlines.length === 0 && undated.length === 0
  if (quiet && weakTopics.length === 0 && !openQuiz) return null

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
