// Single place to change where the panel talks to.
//
// Local development is the default: nothing to set, this points at
// 127.0.0.1:8787. Nothing below changes that unless a Vite variable is set.
//
// Deployed (Step 7): build with the two variables below, e.g.
//
//   VITE_BACKEND_URL=https://<your-endpoint-url> \
//   VITE_BACKEND_TOKEN=<the token you set on the backend> \
//   npm run build
//
// The panel still holds no *Nebius* key — that stays in the backend's env. The
// token here is for our own backend, a different secret that you rotate by
// restarting the backend with a new CADENCE_TOKEN. It is compiled into dist/,
// so treat the built extension as a secret: do not publish it.
//
// `import.meta.env` only exists under Vite; the optional chaining keeps this
// file importable from a plain node test or a non-bundled page.

const env = (typeof import.meta !== 'undefined' && import.meta.env) || {}

function fromEnv(value) {
  return typeof value === 'string' ? value.trim() : ''
}

const configuredUrl = fromEnv(env.VITE_BACKEND_URL)

// Where does this copy of the panel live?
//
// Three cases, in this order, and each one is a different deployment:
//
// 1. A build-time VITE_BACKEND_URL wins outright - a panel built to talk to a
//    host somewhere else. That is the deployed extension.
// 2. **Served as a page** by the backend itself (the `/demo` route, or `dist/`
//    behind any web server): talk to the origin that served the file. This is
//    not a convenience, it is the only answer that can be right - the page came
//    from the backend, and loading it from anywhere else than the port it was
//    served on is how a working demo reads "backend: offline". This was a real
//    bug here, found by opening the page: the built panel asked 127.0.0.1:8787
//    while the server that handed it over was on 8791.
// 3. Otherwise we are a Chrome extension, where `location.protocol` is
//    `chrome-extension:` and there is no origin to inherit: the local default.
//
// Same origin is also what keeps this simple: no CORS, and no host permission
// needed for a page the backend itself served.
const servedOverHttp =
  typeof window !== 'undefined' && /^https?:$/.test(window.location?.protocol || '')
const inherited = servedOverHttp ? fromEnv(window.location.origin) : ''

// No trailing slash: every caller appends a path that already starts with one.
export const BACKEND_URL = (
  configuredUrl ||
  inherited ||
  'http://127.0.0.1:8787'
).replace(/\/+$/, '')

// Empty means "a local backend with no token", which is the Step 6 behaviour.
export const BACKEND_TOKEN = fromEnv(env.VITE_BACKEND_TOKEN)

// How many turns to keep on screen. The panel is narrow and a student reads
// the last exchange, not a scrollback.
export const MAX_TURNS_IN_VIEW = 8
