// Turning microphone samples into numbers the orb can draw.
//
// While the student is talking, the panel opens a second, silent capture of the
// same microphone Chrome's recogniser is already using. Nothing here records,
// stores or sends audio: the analyser is read into a small array of 0..1 values,
// written onto the button as CSS variables, and forgotten on the next frame. If
// the capture fails for any reason the panel keeps its ordinary listening
// animation - a meter is a nicety, never a requirement for asking a question.
//
// The arithmetic lives in this file rather than in the component because it can
// be tested without a browser, a microphone or a requestAnimationFrame: given a
// spectrum, which bars should move and by how much is a pure question.

/** How many bars the listening waveform draws. Odd, so one sits dead centre. */
export const MIC_BANDS = 9

/**
 * The centre-weighted height of each bar, in pixels, before the level scales it.
 *
 * Without this every bar would be the same height and a loud syllable would look
 * like a solid rectangle. The arch is what makes it read as a waveform rather
 * than a volume slider.
 */
export const MIC_BAR_HEIGHTS = Array.from(
  { length: MIC_BANDS },
  (_, index) => Math.round(24 + 20 * Math.sin(((index + 1) / (MIC_BANDS + 1)) * Math.PI)),
)

// Byte spectrum values, 0..255. Below FLOOR is room noise and keyboard clicks;
// CEILING is a voice close to the microphone. Chrome's analyser is already in
// decibels, so this is a dB window rather than a linear amplitude one.
const FLOOR = 30
const CEILING = 165

// Speech energy sits in the bottom of the spectrum, so the bands are spaced
// logarithmically: narrow slices where the voice lives, wide ones up top. Equal
// slices would leave the right-hand bars dead for every word.
const SPECTRUM_TOP = 0.3
const SPACING = 1.7

// Rising fast and falling slowly is what a level meter does, and it is what
// makes a syllable visible: an instant fall would make the bars flicker at the
// bottom on every consonant.
const ATTACK = 0.6
const RELEASE = 0.25

const clamp01 = (value) => (value < 0 ? 0 : value > 1 ? 1 : value)

/** One byte of spectrum, as a 0..1 level with the noise floor removed. */
export function normalise(bin) {
  const raw = (Number(bin) - FLOOR) / (CEILING - FLOOR)
  // A square root of sorts: quiet speech should still be *some* movement, not a
  // row of dots that only lights up when someone shouts.
  return clamp01(Math.pow(clamp01(raw), 0.75))
}

/**
 * A spectrum (byte frequency data) as `count` levels, one per bar.
 *
 * Empty, missing or nonsensical input returns silence rather than throwing:
 * this runs 25 times a second beside a live recogniser, and a malformed frame
 * should cost one flat frame, not the whole turn.
 */
export function bandsFromSpectrum(data, count = MIC_BANDS) {
  const size = Math.max(0, Math.floor(Number(count) || 0))
  if (size === 0) return []
  const levels = new Array(size).fill(0)
  if (!data || typeof data.length !== 'number' || data.length === 0) return levels

  const top = Math.max(1, Math.round(data.length * SPECTRUM_TOP))
  for (let band = 0; band < size; band += 1) {
    const from = Math.round((band / size) ** SPACING * top)
    const to = Math.max(from + 1, Math.round(((band + 1) / size) ** SPACING * top))
    let sum = 0
    for (let bin = from; bin < Math.min(to, data.length); bin += 1) sum += data[bin]
    levels[band] = normalise(sum / (Math.min(to, data.length) - from))
  }
  return levels
}

/**
 * Move each level towards its new reading at the meter's own speed.
 *
 * Kept as a separate step so the attack/release feel can be tuned (and tested)
 * without touching the spectrum maths, and so a dropped frame cannot make the
 * bars jump: one frame of silence never takes a bar all the way down.
 */
export function smoothLevels(previous, next, attack = ATTACK, release = RELEASE) {
  const target = Array.isArray(next) ? next : []
  return target.map((value, index) => {
    const before = clamp01(Number(previous?.[index]) || 0)
    const after = clamp01(Number(value) || 0)
    return before + (after - before) * (after > before ? attack : release)
  })
}

/** One number for the whole row, used for the glow behind the orb. */
export function overallLevel(levels) {
  if (!Array.isArray(levels) || levels.length === 0) return 0
  const total = levels.reduce((sum, value) => sum + clamp01(Number(value) || 0), 0)
  return clamp01(total / levels.length)
}
