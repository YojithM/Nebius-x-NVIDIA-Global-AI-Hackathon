// The service worker does ONE thing: wire the toolbar button to the side panel.
//
// Everything else — microphone, speech recognition, text-to-speech, the turn
// loop — deliberately lives in the side panel. MV3 service workers are
// terminated after ~30s idle, so any audio or conversation state kept here
// would be silently destroyed mid-sentence.
//
// Note: no API key is present anywhere in this extension. Model calls happen
// on the backend; the browser only ever talks to that backend.

chrome.runtime.onInstalled.addListener(() => {
  // Clicking the toolbar icon opens the side panel instead of a popup.
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((error) => console.warn('[cadence] side panel behavior:', error));
});

chrome.runtime.onStartup?.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch(() => {});
});
