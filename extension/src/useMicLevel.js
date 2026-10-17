import { useEffect, useRef, useState } from 'react'
import { MIC_BANDS, bandsFromSpectrum, overallLevel, smoothLevels } from './micLevel.js'

// 25 frames a second. The recogniser is the thing that needs the microphone;
// the meter is decoration, and it has no business competing for the main thread
// while a student is talking.
const FRAME_MS = 40

/**
 * Is there any way to read the microphone level here?
 *
 * Resolved at call time, not import time, for the same reason `speech.js` does:
 * the panel is also served as a plain page for the demo, and a missing
 * `mediaDevices` should mean "no waveform", not a crash on load.
 */
export function micLevelSupported() {
  return Boolean(
    typeof navigator !== 'undefined' &&
      navigator.mediaDevices?.getUserMedia &&
      (typeof window !== 'undefined'
        ? window.AudioContext || window.webkitAudioContext
        : false),
  )
}

/**
 * Drive the listening orb from the live microphone.
 *
 * Returns `{ ref, live }`: attach `ref` to the element whose children draw the
 * bars, and `live` tells the markup whether there is a real signal to show. The
 * levels never enter React state - they are written straight onto the element as
 * CSS variables (`--level`, `--l0`..`--l8`) at animation-frame speed. Ten style
 * writes per frame is cheap; ten `setState` calls per frame would re-render the
 * transcript while someone is speaking into it.
 *
 * Everything is torn down when `active` goes false: the tracks stop, the audio
 * context closes and the variables are removed, so the microphone light goes out
 * the moment listening ends. This is also why the stream is opened only after
 * the recogniser has started: the panel listens when the student asked it to,
 * never merely because it was open.
 */
export function useMicLevel(active, bands = MIC_BANDS) {
  const ref = useRef(null)
  const [live, setLive] = useState(false)

  useEffect(() => {
    if (!active) {
      setLive(false)
      return undefined
    }
    const element = ref.current
    if (!element || !micLevelSupported()) return undefined

    let cancelled = false
    let frame = 0
    let lastPaint = 0
    let stream = null
    let context = null
    let source = null
    let analyser = null
    let levels = new Array(bands).fill(0)

    const clear = () => {
      element.style.removeProperty('--level')
      for (let index = 0; index < bands; index += 1) {
        element.style.removeProperty(`--l${index}`)
      }
    }

    const paint = () => {
      for (let index = 0; index < bands; index += 1) {
        element.style.setProperty(`--l${index}`, levels[index].toFixed(3))
      }
      element.style.setProperty('--level', overallLevel(levels).toFixed(3))
    }

    const start = async () => {
      try {
        // A second consumer of the same device the recogniser is using. Chrome
        // shares it; the permission is the extension origin's, already granted
        // if speaking to Cadence works at all.
        //
        // Local references as well as the closure's: the cleanup nulls the
        // closure variables, so a promise that resolves just after the student
        // stopped would otherwise have nothing left to close.
        const media = await navigator.mediaDevices.getUserMedia({ audio: true })
        stream = media
        if (cancelled) {
          // Listening ended while Chrome was still asking for the device. The
          // cleanup has already run, so nothing else will stop this stream: if
          // it is not closed here, the microphone stays on after the student
          // stopped.
          media.getTracks().forEach((track) => track.stop())
          stream = null
          return
        }

        const AudioContext = window.AudioContext || window.webkitAudioContext
        const audio = new AudioContext()
        context = audio
        // A context created outside a gesture can start suspended, and a
        // suspended one feeds an analyser nothing but zeros.
        if (audio.state === 'suspended') await audio.resume()
        if (cancelled) {
          audio.close().catch(() => {})
          context = null
          return
        }

        source = audio.createMediaStreamSource(media)
        analyser = audio.createAnalyser()
        analyser.fftSize = 256
        analyser.smoothingTimeConstant = 0.7
        // Connected to the analyser only - never to the destination, or the
        // panel would play the student's own microphone back at them.
        source.connect(analyser)
        setLive(true)

        const data = new Uint8Array(analyser.frequencyBinCount)
        const tick = (now) => {
          if (cancelled) return
          frame = requestAnimationFrame(tick)
          if (analyser === null || now - lastPaint < FRAME_MS) return
          lastPaint = now
          analyser.getByteFrequencyData(data)
          levels = smoothLevels(levels, bandsFromSpectrum(data, bands))
          paint()
        }
        frame = requestAnimationFrame(tick)
      } catch {
        // Refused, no device, or a context that could not start. The rings and
        // the breathing orb keep going, and the student is never told that a
        // decoration failed.
        if (!cancelled) setLive(false)
      }
    }

    start()

    return () => {
      cancelled = true
      cancelAnimationFrame(frame)
      setLive(false)
      clear()
      if (stream) stream.getTracks().forEach((track) => track.stop())
      if (source) source.disconnect()
      if (analyser) analyser.disconnect()
      if (context && context.state !== 'closed') context.close().catch(() => {})
      stream = null
      source = null
      analyser = null
      context = null
    }
  }, [active, bands])

  return { ref, live }
}
