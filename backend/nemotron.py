"""Minimal Nebius Token Factory client for the Step 0 spike.

Stdlib only (no pip install) so the spike runs on a bare Python 3.9+.
Reused later by the FastAPI backend, so it already computes cost per request
and enforces the model-tier mapping from config/models.json.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "models.json"
ENV_PATH = ROOT / ".env"


class NebiusError(RuntimeError):
    """Any failure talking to Token Factory. Never contains the API key."""


def load_env(path=None):
    """Populate os.environ from a KEY=VALUE .env file. No third-party deps."""
    path = Path(path or ENV_PATH)
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def load_config(path=None):
    """Load tier config. NEMOTRON_CONFIG_PATH overrides the default path (used by tests)."""
    resolved = path or os.environ.get("NEMOTRON_CONFIG_PATH") or CONFIG_PATH
    return json.loads(Path(resolved).read_text(encoding="utf-8"))


def cost_usd(config, tier, usage):
    """Cost of one request from the usage block. Prices are per 1M tokens."""
    t = config["tiers"][tier]
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    return (
        prompt_tokens * float(t["price_in_per_1m"])
        + completion_tokens * float(t["price_out_per_1m"])
    ) / 1_000_000.0


class NebiusClient:
    def __init__(self, config=None, timeout=90):
        self.config = config or load_config()
        provider = self.config["provider"]
        self.base_url = provider["base_url"].rstrip("/")
        self.timeout = timeout
        self.api_key = os.environ.get(provider["api_key_env"], "").strip()
        if not self.api_key:
            raise NebiusError(
                "No API key found. Put your Nebius Token Factory key in "
                "{0} as {1}=... (the file is gitignored). Get a key at {2}".format(
                    ENV_PATH, provider["api_key_env"], provider.get("docs", "")
                )
            )

    def _request(self, method, path, payload=None):
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self.api_key)
        request.add_header("Content-Type", "application/json")
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            raise NebiusError(
                "HTTP {0} from {1} (model={2}): {3}".format(
                    exc.code, url, (payload or {}).get("model", "-"), detail
                )
            ) from None
        except urllib.error.URLError as exc:
            raise NebiusError(
                "Network error reaching {0}: {1}".format(url, exc.reason)
            ) from None
        except OSError as exc:
            # A reset or a timeout mid-read is not a URLError, so it slipped past
            # the handler above and took the whole request with it: the socket
            # closed with no reply and the panel showed a dead connection. One
            # flaky connection should cost the student a 502, not the turn.
            raise NebiusError(
                "Network error reaching {0}: {1}".format(url, exc)
            ) from None
        latency = time.time() - started
        try:
            return json.loads(body), latency
        except json.JSONDecodeError:
            raise NebiusError(
                "Expected JSON from {0} but got: {1}".format(url, body[:300])
            ) from None

    def list_models(self):
        """GET /v1/models -> (list of model ids, latency)."""
        data, latency = self._request("GET", self.config["provider"]["models_path"])
        entries = data.get("data", data) if isinstance(data, dict) else data
        ids = []
        for entry in entries or []:
            if isinstance(entry, dict):
                ids.append(entry.get("id") or entry.get("name") or "")
            elif isinstance(entry, str):
                ids.append(entry)
        return sorted(i for i in ids if i), latency

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        """POST /v1/chat/completions using the named tier. Returns (json, latency)."""
        if tier not in self.config["tiers"]:
            raise NebiusError(
                "Unknown tier {0!r}. Known tiers: {1}".format(
                    tier, sorted(self.config["tiers"])
                )
            )
        tier_cfg = self.config["tiers"][tier]
        payload = {
            "model": tier_cfg["model"],
            "messages": messages,
            "temperature": temperature,
        }
        limit = (
            max_tokens
            if max_tokens is not None
            else self.config["budgets"].get("max_output_tokens_per_request")
        )
        if limit:
            payload["max_tokens"] = limit

        # These Nemotron models reason before answering, and reasoning tokens are
        # billed as output. For a classification task that is pure waste - and
        # measurably harmful: at a small max_tokens the reasoning ate the whole
        # budget and the JSON never arrived. The tier decides, in one config file.
        if tier_cfg.get("thinking") is False:
            template_kwargs = dict(payload.get("chat_template_kwargs") or {})
            template_kwargs["enable_thinking"] = False
            payload["chat_template_kwargs"] = template_kwargs

        if extra:
            payload.update(extra)
        data, latency = self._request(
            "POST", self.config["provider"]["chat_completions_path"], payload
        )
        return data, latency


def reply_text(response):
    """Pull assistant text out of a chat-completion response, tolerating shapes."""
    choices = (response or {}).get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return (content or "").strip()
