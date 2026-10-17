import { useEffect, useState } from 'react'
import { BACKEND_URL } from './config.js'
import { fetchCalendar } from './api.js'

/**
 * The calendar's one home on the Today tab: an offer to connect, for a student
 * who has not yet, and **nothing at all** once they have.
 *
 * The brief that used to sit here printed today's deadlines and then the next two
 * weeks of events, on every open, whether or not anyone had asked. That is a
 * dashboard in a panel whose whole shape is a conversation: it took the top of the
 * screen from the transcript, it answered a question nobody had asked yet, and it
 * made "what is on my calendar this week?" look like a thing you read rather than a
 * thing you say. So the events are answered by the agent's `read_calendar` tool
 * when they are asked for, and the panel keeps a single quiet line until the
 * calendar is connected - because a connection that cannot be made from the UI is
 * not a feature, it is a config file.
 *
 * Two silences, both deliberate:
 *
 * * It renders nothing while loading. A link that appears a second late is worse
 *   than the space it would have held.
 * * It renders nothing when the calendar is connected, or when the backend cannot
 *   be reached at all - the header already reports `backend: offline`.
 *
 * The read is repeated on `focus` as well as on `refreshKey`, so a student who
 * connects in the tab the link opened and then clicks back into the panel sees the
 * line disappear without reloading anything.
 */
export default function CalendarOffer({ refreshKey = 0 }) {
  const [calendar, setCalendar] = useState(null)

  useEffect(() => {
    let cancelled = false
    const read = async () => {
      try {
        const payload = await fetchCalendar()
        if (!cancelled) setCalendar(payload)
      } catch {
        // Silent on purpose: see the second silence above.
      }
    }
    read()
    window.addEventListener('focus', read)
    return () => {
      cancelled = true
      window.removeEventListener('focus', read)
    }
  }, [refreshKey])

  // Not "no payload yet" only: connected (or failing with a live connection) is
  // also a reason to show nothing here.
  if (!calendar || calendar.connected) return null

  if (calendar.configured === false) {
    return (
      <p className="calendar-offer">
        Calendar off — set GOOGLE_CLIENT_ID in .env to connect a read-only Google
        Calendar.
      </p>
    )
  }

  return (
    <p className="calendar-offer">
      <a
        href={`${BACKEND_URL}/oauth/google/start`}
        target="_blank"
        rel="noreferrer noopener"
        className="connect"
      >
        Connect Google Calendar
      </a>{' '}
      to see what is coming up — then just ask me what is on this week. Read-only:
      I can never change an event.
    </p>
  )
}
