import { useCallback, useEffect, useState } from 'react'
import { deleteMemoryFact, fetchMemory } from './api.js'

/**
 * The Memory tab.
 *
 * This exists to make one promise checkable rather than merely stated: the
 * student can see everything we kept, and remove any of it. Memory you cannot
 * inspect is indistinguishable from memory you should not trust, and a delete
 * button that is missing is how a "we remember things for you" feature becomes
 * something people resent.
 *
 * Facts are grouped by topic because that is how they are stored on disk — one
 * Markdown file per topic — so what is on screen matches what is in the file.
 */
export default function MemoryTab() {
  const [state, setState] = useState({ status: 'loading' })
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(async () => {
    setError('')
    try {
      setState({ status: 'ready', data: await fetchMemory() })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function onForget(fact) {
    setBusyId(fact.id)
    setError('')
    try {
      // The backend answers a delete with the whole fresh state, so there is no
      // optimistic guess to get out of sync with the file on disk.
      setState({ status: 'ready', data: await deleteMemoryFact(fact.topic, fact.id) })
    } catch (err) {
      setError(err.message)
      load()
    } finally {
      setBusyId(null)
    }
  }

  if (state.status === 'loading') {
    return <main className="memory"><p className="empty">Loading what we remember…</p></main>
  }

  if (state.status === 'error') {
    return (
      <main className="memory">
        <p className="empty">{state.message}</p>
        <button type="button" className="retry" onClick={load}>
          Try again
        </button>
      </main>
    )
  }

  const { topics = [], total = 0, errors = [], directory } = state.data
  const withFacts = topics.filter((t) => t.count > 0)

  return (
    <main className="memory">
      {withFacts.length === 0 && (
        <p className="empty">
          Nothing saved yet. Say <em>“remember that my midterm is on the 12th”</em> and
          it lands here.
        </p>
      )}

      {withFacts.map((topic) => (
        <section key={topic.topic} className="memory-topic">
          <h2>
            {topic.title}
            <span className="count">
              {topic.count} {topic.count === 1 ? 'note' : 'notes'}
            </span>
          </h2>
          <ul>
            {state.data.facts
              .filter((fact) => fact.topic === topic.topic)
              .map((fact) => (
                <li key={fact.id}>
                  <span className="fact">{fact.text}</span>
                  {fact.added && <span className="added">{fact.added}</span>}
                  <button
                    type="button"
                    className="forget"
                    disabled={busyId === fact.id}
                    aria-label={`Forget: ${fact.text}`}
                    title="Forget this"
                    onClick={() => onForget(fact)}
                  >
                    {busyId === fact.id ? '…' : '×'}
                  </button>
                </li>
              ))}
          </ul>
        </section>
      ))}

      {error && <p className="error">{error}</p>}

      {errors.length > 0 && (
        <p className="error">
          {errors.length} memory file(s) could not be read: {errors[0]}
        </p>
      )}

      {total > 0 && (
        <footer className="memory-foot">
          {total} {total === 1 ? 'note' : 'notes'} in plain Markdown under{' '}
          <code>{directory}</code> — edit or delete them yourself, and we'll pick it up.
        </footer>
      )}
    </main>
  )
}
