"""Serving the built panel as a plain page - the second half of `/demo`.

The panel is the same React app either way. Loaded as an extension it has
`chrome.*` and a mic; served as a page from the backend it has neither, and
`pageContext.js`/`speech.js` already degrade on exactly that, so nothing here is
a special build. This module is the part that makes the same files reachable by
a visitor who installed nothing.

Four rules, and three of them exist because this is the first route in the
backend that reads a file named by the request:

1. **One directory, chosen by us.** Everything resolves under `extension/dist`
   and the resolved path is checked against it afterwards. The path check is on
   the *resolved* path, so `..` is refused because the file it lands on is
   outside the root rather than because the string contains two dots.
2. **A known extension, or nothing.** No source maps and no dotfiles: a `.map`
   in a public demo hands a visitor our source, and the first file anyone probes
   for on a new URL is `/.env`. Unknown types are a 404 instead of a guess at a
   content type, because guessing is how a browser is talked into executing
   something as a different kind of thing than we meant.
3. **One entry point, and the assets it asks for - nothing else.** No directory
   listing and no extension probing: `/demo` serves `sidepanel.html`, and the
   only other paths that resolve are the ones inside `assets/`. Which is not an
   arbitrary line - it is what the page itself requests. Vite writes its script
   and stylesheet tags as root-absolute URLs (`/assets/sidepanel.js`), so a
   route that only answered under `/demo/` would serve a page whose own
   JavaScript 404s. The rest of the build (the extension manifest, the service
   worker) is not part of the page and is not served.
4. **Bounded.** A file larger than `MAX_BYTES` is refused rather than read into
   memory, and the build being absent is a 503 that says how to make one instead
   of a blank page.
"""

import os
from pathlib import Path, PurePosixPath

# Types a browser needs told correctly, and nothing else. `text/javascript` (not
# `application/javascript`) is what a `<script type="module">` is served as.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

MAX_BYTES = 2 * 1024 * 1024

# The panel's entry point, and the only file `/demo` itself serves.
DEFAULT_ENTRY = "sidepanel.html"


class StaticApp:
    """A read-only view over one build directory."""

    def __init__(self, root, entry=DEFAULT_ENTRY):
        self.root = Path(root)
        self.entry = entry

    @property
    def available(self) -> bool:
        """Is there a build to serve? Checked per request, not cached."""
        try:
            return (self.root / self.entry).is_file()
        except OSError:
            return False

    def missing_build_message(self) -> str:
        return (
            "the panel has not been built, so there is nothing to serve at /demo - "
            "run `cd extension && npm install && npm run build`, then reload this page"
        )

    def read(self, url_path):
        """Resolve one request. -> (status, content_type, body_bytes).

        Never raises: a request path is untrusted input, and the useful answer to
        a strange one is a 404 with a sentence, not a traceback.
        """
        if not self.available:
            return 503, "application/json; charset=utf-8", self._json(
                self.missing_build_message()
            )

        relative = self._relative(url_path)
        if relative is None:
            return 404, "application/json; charset=utf-8", self._json(
                "no such file in the panel build"
            )

        suffix = relative.suffix.lower()
        content_type = CONTENT_TYPES.get(suffix)
        if content_type is None:
            return 404, "application/json; charset=utf-8", self._json(
                "not a file type this route serves"
            )

        try:
            target = (self.root / relative).resolve()
            root = self.root.resolve()
        except OSError:
            return 404, "application/json; charset=utf-8", self._json("unreadable path")

        if not self._inside(root, target):
            return 404, "application/json; charset=utf-8", self._json(
                "no such file in the panel build"
            )
        if not target.is_file():
            return 404, "application/json; charset=utf-8", self._json(
                "no such file in the panel build"
            )
        try:
            size = target.stat().st_size
            if size > MAX_BYTES:
                return 413, "application/json; charset=utf-8", self._json(
                    f"that file is {size} bytes, over the {MAX_BYTES} byte limit"
                )
            return 200, content_type, target.read_bytes()
        except OSError:
            return 404, "application/json; charset=utf-8", self._json("unreadable file")

    # ------------------------------------------------------------- internals --

    def _relative(self, url_path):
        """Map a URL path to a path relative to the build root, or None.

        `/demo` and `/demo/` are the entry point. Beyond that, two shapes
        resolve: `/assets/<file>` (what the built HTML asks for) and
        `/demo/assets/<file>`. Everything else is a 404 by construction rather
        than by checking for it.
        """
        raw = (url_path or "").split("?")[0].split("#")[0]
        if raw in ("", "/", "/demo", "/demo/"):
            return Path(self.entry)

        rest = None
        for prefix in ("/demo/", "/"):
            if raw.startswith(prefix):
                rest = raw[len(prefix):]
                break
        if not rest:
            return None

        # Assessed as POSIX parts, whatever platform this is running on: a
        # backslash is a separator on one and a legal filename character on the
        # other, and a route that behaves differently on two machines is a route
        # nobody can reason about. Trailing slashes never resolve - there is no
        # directory listing here to fall back on.
        posix = PurePosixPath(rest)
        if "\\" in rest or "\x00" in rest or rest.endswith("/"):
            return None
        if str(posix) == self.entry:
            return Path(self.entry)
        if len(posix.parts) >= 2 and posix.parts[0] == "assets":
            return Path(*posix.parts)
        return None

    @staticmethod
    def _inside(root, target) -> bool:
        root_text = str(root)
        target_text = str(target)
        return target_text == root_text or target_text.startswith(root_text + os.sep)

    @staticmethod
    def _json(message):
        import json

        return json.dumps({"error": message}).encode("utf-8")
