// The listening waveform, tested without a microphone.
//
// `micLevel.js` is pure on purpose: which bars move, how far, and how fast they
// fall is arithmetic, and arithmetic does not need Chrome. The hook that owns
// the stream is deliberately not tested here - it is mostly `getUserMedia` and
// a requestAnimationFrame loop, and the failure that matters (no permission, no
// device) is the one the panel already handles by falling back to its rings.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  MIC_BANDS,
  MIC_BAR_HEIGHTS,
  bandsFromSpectrum,
  normalise,
  overallLevel,
  smoothLevels,
} from '../src/micLevel.js'

const spectrum = (length = 128, fill = 0) => new Uint8Array(length).fill(fill)

test('silence is silence, not a row of halfway bars', () => {
  assert.deepEqual(bandsFromSpectrum(spectrum(128, 0), MIC_BANDS), new Array(MIC_BANDS).fill(0))
  // Room noise and key clicks sit under the floor and are deliberately ignored:
  // otherwise the waveform twitches whenever the laptop fan speeds up.
  assert.equal(normalise(30), 0)
  assert.equal(normalise(12), 0)
})

test('a full-scale spectrum fills every bar', () => {
  const levels = bandsFromSpectrum(spectrum(128, 255), MIC_BANDS)
  assert.equal(levels.length, MIC_BANDS)
  for (const level of levels) {
    assert.ok(level > 0.99 && level <= 1, `expected a full bar, got ${level}`)
  }
  assert.equal(normalise(255), 1)
})

test('quiet speech still moves the bars', () => {
  // 60 is a low but real voice in the analyser's dB window. A linear scale
  // would put it at 0.22 and look motionless; the curve lifts it.
  const quiet = normalise(60)
  assert.ok(quiet > 0.3, `quiet speech should be visible, got ${quiet}`)
  assert.ok(quiet < normalise(120), 'and still quieter than a louder one')
})

test('bands climb with the spectrum, low frequencies first', () => {
  // Bin n carries n, so every band's average is higher than the one before it.
  const data = Uint8Array.from({ length: 128 }, (_, index) => Math.min(255, index * 4))
  const levels = bandsFromSpectrum(data, MIC_BANDS)
  for (let index = 1; index < levels.length; index += 1) {
    assert.ok(
      levels[index] >= levels[index - 1],
      `band ${index} (${levels[index]}) should not be quieter than band ${index - 1}`,
    )
  }
  assert.ok(levels[0] < levels.at(-1), 'the spectrum should not be read inside out')
})

test('the bars are centre-weighted, so a loud syllable reads as a waveform', () => {
  assert.equal(MIC_BAR_HEIGHTS.length, MIC_BANDS)
  const middle = Math.floor(MIC_BANDS / 2)
  assert.equal(
    Math.max(...MIC_BAR_HEIGHTS),
    MIC_BAR_HEIGHTS[middle],
    'the tallest bar belongs in the middle',
  )
  assert.ok(MIC_BAR_HEIGHTS[0] < MIC_BAR_HEIGHTS[middle])
  assert.ok(MIC_BAR_HEIGHTS.at(-1) < MIC_BAR_HEIGHTS[middle])
  // Heights are multiplied by 0..1 in CSS, so they have to be usable pixels.
  assert.ok(MIC_BAR_HEIGHTS.every((height) => height > 10 && height < 60))
})

test('the level rises fast and falls slowly', () => {
  // One frame of a new syllable is most of the way up; one frame of silence is
  // only a quarter of the way down. That asymmetry is what stops flicker.
  const rising = smoothLevels([0, 1, 0], [1, 0, 0])
  assert.equal(Math.round(rising[0] * 100), 60)
  assert.equal(Math.round(rising[1] * 100), 75)
  assert.equal(rising[2], 0)
})

test('smoothing copes with a short history and out-of-range readings', () => {
  // First frame: there is no previous value, so the reading is the only input.
  assert.deepEqual(smoothLevels(undefined, [0.5]).map((n) => +n.toFixed(2)), [0.3])
  assert.deepEqual(smoothLevels([], [1]).map((n) => +n.toFixed(2)), [0.6])
  const clamped = smoothLevels([0], [2])
  assert.ok(clamped[0] <= 1)
  assert.equal(smoothLevels([0.5], [-1])[0], 0.375)
})

test('malformed frames cost one flat frame, not the turn', () => {
  assert.deepEqual(bandsFromSpectrum(null, MIC_BANDS), new Array(MIC_BANDS).fill(0))
  assert.deepEqual(bandsFromSpectrum(undefined, MIC_BANDS), new Array(MIC_BANDS).fill(0))
  assert.deepEqual(bandsFromSpectrum([], MIC_BANDS), new Array(MIC_BANDS).fill(0))
  assert.deepEqual(bandsFromSpectrum(spectrum(128, 255), 0), [])
  assert.deepEqual(bandsFromSpectrum(spectrum(128, 255), -3), [])
  assert.equal(bandsFromSpectrum(spectrum(4, 255), 5).length, 5, 'short spectra still fit')
})

test('the overall level is one honest number', () => {
  assert.equal(overallLevel([]), 0)
  assert.equal(overallLevel(null), 0)
  assert.equal(overallLevel([0, 1, 0.5]), 0.5)
  assert.equal(overallLevel([2, -1]), 0.5, 'a bad frame cannot push the glow out of range')
  assert.ok(overallLevel(bandsFromSpectrum(spectrum(128, 255))) > 0.99)
})
