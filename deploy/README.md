# Deploying the Cadence backend (Step 7)

Goal: the extension talks to a hosted backend instead of `127.0.0.1:8787`. Only
the URL and a token change — the browser still never holds a Nebius key.

> **Read the cost section first. It is the part of this step that can go wrong.**

---

## The cost problem, up front

The backend must stay running (persistent memory + a future scheduler), so it
needs an always-on host. Nebius bills that by the second, and the numbers are not
small:

| Option | Smallest sensible size | Hourly | ~30 days |
|---|---|---|---|
| Compute VM, AMD `cpu-d3` | 2 vCPU / 8 GiB | $0.066 | **~$48** |
| Compute VM, Intel `cpu-e2` (eu-north1) | 2 vCPU / 8 GiB | $0.060 | **~$44** |
| Serverless Endpoint, `cpu-d3` | 2 vCPU / 8 GiB | $0.066 | **~$48** |

The project budget is **~$25 total**, so an always-on instance does not fit — and
this matches the note already in the root README that said the hourly price was
unverified and a blocker. It is now verified, and it is a blocker.

Two facts make this workable anyway:

- **A stopped VM is not charged for compute.** Storage keeps costing, but the
  compute clock stops. So the honest deployment for a hackathon demo is: create
  the VM, start it before the demo, stop it after.
- **Serverless Endpoints have no documented scale-to-zero** and keep running
  until stopped or deleted. They are convenient and they are *not* cheap when
  idle.

**So the recommendation is a Compute VM that you stop when you are not demoing**,
budgeting roughly $0.07 per demo hour. If you would rather not manage a VM, the
Serverless Endpoint path at the bottom is the alternative — same cost per hour,
less ops, less durable storage.

Confirm the live price in the Nebius console before you create anything. Prices
change, and the console is the source of truth, not this file.

---

## What deployment changes in this repo (nothing surprising)

Everything is env-driven, so local development is untouched:

| Variable | What it does |
|---|---|
| `CADENCE_HOST` | bind address. `127.0.0.1` locally, `0.0.0.0` on a host |
| `CADENCE_PORT` | port, default `8787` |
| `CADENCE_TOKEN` | **when set, every route except `GET /health` requires `Authorization: Bearer <token>`** |
| `CADENCE_ALLOW_ORIGIN` | one extra exact origin allowed through CORS, for the panel served as a page |
| `CADENCE_BRIEF_HOUR` | hour the scheduler logs the day's brief, default `7` (local time) |
| `CADENCE_BRIEF_DAYS` | how close a deadline counts as a nudge, default `7` days |
| `CADENCE_BRIEF` | set to `off` to stop the background brief tick |
| `NEBIUS_API_KEY` | the model key (unchanged) |
| `EXA_API_KEY` | optional: the web-search tool the general agent loop can call (Step 9). `TAVILY_API_KEY` is the alternative; Exa wins if both are set. Without one, the loop still answers and says what it could not check, and `/health` reports `tools.search.configured: false` instead of pretending. |

`CADENCE_TOKEN` is the important one. The backend spends your Nebius credit, so a
public URL with no token is an open wallet. With it unset the server behaves
exactly as it does locally — which is why the local panel and the test suite are
unaffected.

The three `CADENCE_BRIEF*` variables only affect the scheduler's once-a-day log
line; the brief the panel shows is a request and is always answered, and it never
calls a model (see `backend/scheduler.py`).

All of them are read from the **process environment first, then `.env`** (the file
never overrides what the environment already sets). Either route works, including
on a host: `CADENCE_TOKEN` in `.env` is honoured. It is read at import time, so
changing it needs a restart — and that ordering is covered by tests, because a
token that is silently ignored comes up as a *public* backend rather than an
error.

---

## Before any of this: can this account write at all?

```bash
python3 deploy/preflight.py "$PROJECT_ID" --platform cpu-d3 --preset 2vcpu-8gb --probe-writes
```

If that ends in a `PermissionDenied`, stop — no command below will work, and the
rest of this file is a waste of an afternoon. This happened to us on an account
where the signed-in user **was the tenant owner**: reads passed everywhere
(`whoami`, project lookup, `compute platform list`, subnets, security groups,
quotas, even the price calculator), and three different services all refused to
create anything:

- `vpc security-group create` → `PermissionDenied: Permission denied to project-…` (VPC API)
- `registry create` → `PermissionDenied`
- `compute instance create` → `PermissionDenied` (service compute)

It was not a stale token (a fresh login behaved identically), not a missing role
(the `admins` group's only member resolved to the same name, email and federation
as the CLI), and not quota (all 15 compute allowances were provisioned). The
tenant was minutes old and auto-named, which points at **account activation —
most likely billing — rather than anything in this repo**. Check Billing in the
console; if it wants a payment method or shows no credit, that is your answer.

### The two ids that are easy to get wrong

`--parent-id` must be an **AI Cloud project id** (`project-…`) from the **AI Cloud
console** (console.nebius.com), not the id the **Token Factory** console shows for
its project (`aiproject-…`). They are the same trailing characters and different
namespaces: the Compute API rejects the Token Factory one with
`expected id types: project, got "aiproject"`, and IAM calls aimed at it answer
`PermissionDenied: service iam, resource ID: aiproject-…`. If you copy the id off
the rate-limits page because that is the console you were already in, every
command below fails in a different way.

A profile also needs the **tenant id** (`profile create` will happily store *no*
tenant, and then `compute platform list` fails later with
`get tenant ID for … iam traverse up … Resource not found` instead of saying
"your profile has no tenant"). Prefer the non-interactive form, which makes both
values explicit:

```bash
nebius profile create \
  --profile cadence \
  --endpoint api.nebius.cloud \
  --federation-endpoint auth.nebius.com \
  --tenant-id "$TENANT_ID" \
  --parent-id "$PROJECT_ID"
```

Confirm the result before going further — this is the whole reason to check rather
than trust:

```bash
nebius iam whoami                     # proves the login landed
cat ~/.nebius/config.yaml             # must show tenant-id AND parent-id
nebius compute platform list --parent-id "$PROJECT_ID" | head
```

### Everything else

- `nebius` CLI installed and authenticated (`nebius profile list`). To install:

  ```bash
  curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
  # Then create the profile with the two ids above, NOT the bare interactive
  # form: that one accepted an empty tenant id for us and reported success.
  ```

  The installer verifies the downloaded binary reports the version it just
  fetched before installing, and it writes only under `$HOME/.nebius`. A profile
  that already exists can be corrected rather than recreated — `nebius profile
  update --tenant-id … --parent-id …` takes both. Every
  command in this file was checked against `nebius` **0.12.291**; flags do move
  between releases, so re-read `--help` if something is rejected.
- a project id and a subnet id in your project
- Docker (to build the image) — optional on the VM path, required for the endpoint
- an SSH key for the VM path

```bash
export PROJECT_ID="<project-id>"
export SUBNET_ID="<subnet-id>"
export REGION="eu-north1"
```

**Generate the token once and keep it** — you will need the same value in two
places (the backend, and the extension build):

```bash
export CADENCE_TOKEN="$(openssl rand -hex 32)"
echo "$CADENCE_TOKEN"   # store it somewhere safe; never commit it
```

---

## Path A — Compute VM (recommended)

### 1. Create the VM

```bash
# Run deploy/preflight.py first. The platform and preset names are free-form
# strings, so a typo is an API error at create time, not a warning — and the
# right ones are per-project, not per-docs. In the account this was written
# against (us-north1) the platform is `cpu-d3`, not the `cpu-e2` the pricing
# pages describe, and the smallest preset is `2vcpu-8gb`:
#
#   python3 deploy/preflight.py "$PROJECT_ID" --platform cpu-d3 --preset 2vcpu-8gb

nebius compute instance create \
  --parent-id "$PROJECT_ID" \
  --name cadence-backend \
  --resources-platform cpu-d3 \
  --resources-preset 2vcpu-8gb \
  --boot-disk-managed-disk-name cadence-boot \
  --boot-disk-managed-disk-type network_ssd \
  --boot-disk-managed-disk-size-gibibytes 30 \
  --boot-disk-managed-disk-source-image-family-image-family ubuntu24.04-driverless \
  --boot-disk-attach-mode read_write \
  --network-interfaces "[{\"name\":\"eth0\",\"ip_address\":{},\"public_ip_address\":{},\"subnet_id\":\"$SUBNET_ID\"}]" \
  --cloud-init-user-data "$(cat deploy/cloud-init.yaml)"
```

Two details here were wrong in the first version of this file and were caught by
reading `nebius compute instance create --help` off the installed binary
(0.12.291) rather than trusting the docs:

- **`--cloud-init-user-data-file` does not exist.** The flag is
  `--cloud-init-user-data` and it takes the YAML itself, so the file is passed
  through `$(cat ...)`. The `${CADENCE_IMAGE}` placeholder inside the systemd unit
  survives literally — command substitution output is not re-expanded by the
  shell — which is what we want, because it is systemd that fills it in at
  runtime from `/etc/cadence.env`.
- **`--boot-disk-attach-mode read_write`, lower case.** The CLI's own OPTIONS
  block lists `read_only` / `read_write` while its own EXAMPLE directly above uses
  `READ_WRITE`. When the binary contradicts itself, the enum list is the one that
  is enforced.

`deploy/cloud-init.yaml` installs Docker and a small `cadence` systemd service
that runs the container with `--restart always`. Read it before using it: it
takes the image name and your key as variables you must fill in.

### 2. Put the key and token on the box, not in the repo

The backend reads them from the environment. On the VM, write them once into
`/etc/cadence.env` (mode `600`, owned by root) — see the unit file
[`deploy/cadence.service`](cadence.service):

```
NEBIUS_API_KEY=...
CADENCE_TOKEN=...
CADENCE_HOST=0.0.0.0
CADENCE_PORT=8787
```

### 3. TLS

Give the VM a hostname (DNS A record to its public IP) and run Caddy, which
obtains a certificate automatically:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the hostname inside first
sudo systemctl reload caddy
```

Caddy terminates TLS on 443 and proxies to `127.0.0.1:8787`. The backend keeps
binding `0.0.0.0:8787` behind it; the firewall should expose only 22/80/443.

### 4. Verify before touching the extension

```bash
BASE="https://cadence.example.com"

# Open by design: this is how a deploy is checked from outside.
# `tools.search.configured` is worth reading on every deploy: false means the loop
# will answer "I could not check that" rather than searching, which is a working
# server and a missing feature - two very different things.
curl -s "$BASE/health" | jq '{ok, mode, skills_loaded, api_key_present, tools}'

# The gate is real: no token must be refused, and no model call may happen.
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/progress"     # expect 401
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/turn" -X POST \
  -H 'Content-Type: application/json' -d '{"transcript":"hello"}'   # expect 401

# With the token:
curl -s "$BASE/progress" -H "Authorization: Bearer $CADENCE_TOKEN" | jq .totals

# What the backend has spent (Step 10). Micro-dollar accounting from the
# provider's own token counts: `turns`, `cost_usd`, `remaining_credit_usd`,
# and `by_tier`. This is the number the panel shows on every tab.
curl -s "$BASE/health" | jq '.spend'

# The action log (Step 10), newest first. Counts and events only - no page text,
# no deadline words, nothing the student said. `note` says when older lines are on
# disk but were not read.
curl -s "$BASE/log?limit=10" -H "Authorization: Bearer $CADENCE_TOKEN" \
  | jq '{count, note, entries: [.entries[] | {ts, event, skill}]}'

# The panel as a page. With a token set this is 401 - a browser cannot send a
# bearer token from an address bar, which is exactly why the demo mode exists.
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/demo"   # expect 401 here

# The brief (Step 8): what is due, from the student's own files. Expect 0 cost -
# it is composed in code, and `source` says so.
curl -s "$BASE/brief" -H "Authorization: Bearer $CADENCE_TOKEN" \
  | jq '{date, headline, counts, cost_usd, source}'

# The general agent loop (Step 9). `tools` reports what the plan ran - server-side
# counts, never the query - and `steps` must stay within `max_steps`. With no search
# key the expected shape is one `web_search` entry with `ok: false`.
curl -s "$BASE/turn" -H "Authorization: Bearer $CADENCE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"transcript":"how many days until my midterm"}' \
  | jq '{skill, reply, steps, max_steps, tools, steps_truncated, search_configured}'
```

### 5. Point the extension at it

```bash
cd extension
VITE_BACKEND_URL="$BASE" VITE_BACKEND_TOKEN="$CADENCE_TOKEN" npm run build
```

**Before building: add your hostname to `host_permissions`.** This is the step
people skip, and its failure is quiet — Chrome blocks the request before it leaves
the browser, so it looks exactly like the backend being down. The manifest ships
with `https://*.nebius.cloud/*` and localhost; a Caddy hostname is neither. Edit
[`extension/public/manifest.json`](../extension/public/manifest.json):

```json
  "host_permissions": [
    "http://127.0.0.1:8787/*",
    "http://localhost:8787/*",
    "https://*.nebius.cloud/*",
    "https://cadence.example.com/*"
  ],
```

Then reload the extension in `chrome://extensions`. The panel badge should read
`backend: router`, and the header shows the remote host.

Check both halves without a browser — the origin in the built manifest, and the
URL the built panel will actually call:

`$BASE` from step 4 is already an origin (no path), which is what
`host_permissions` matches on:

```bash
cd extension
jq -r '.host_permissions[]' dist/manifest.json | grep -q "$BASE" \
  && echo "manifest allows $BASE"
grep -o "$BASE" dist/assets/sidepanel.js | head -1   # the compiled URL
```

If the panel still cannot reach a backend you have curl'd successfully, that is
the message you will get: since Step 7 it names `host_permissions` explicitly
instead of guessing "it may be stopped".

### 6. Stop it when you are done

```bash
# `stop` takes an id, not a name — resolve it once and reuse it.
INSTANCE_ID=$(nebius compute instance get-by-name \
  --parent-id "$PROJECT_ID" --name cadence-backend --format json \
  | jq -r '.metadata.id')

nebius compute instance stop --id "$INSTANCE_ID"    # compute billing stops
nebius compute instance start --id "$INSTANCE_ID"   # before the next demo
```

`nebius compute instance stop cadence-backend` — a name where the CLI wants an
id — returns an invalid-argument error rather than resolving the name.

---

## Path B — Serverless Endpoint (managed HTTPS, no VM)

Shorter to set up, and the managed HTTPS URL means no DNS or Caddy. Two caveats
from the Nebius docs: the endpoint's **container disk does not survive a stop or
restart**, so durable memory needs a mounted bucket or shared filesystem; and
there is no documented scale-to-zero, so it bills while it exists.

```bash
# 1. Registry + image
nebius registry create --name cadence --parent-id "$PROJECT_ID"
nebius registry configure-helper
docker build -t cadence-backend .
docker tag cadence-backend "$REGISTRY/cadence-backend:0.4.0"
docker push "$REGISTRY/cadence-backend:0.4.0"

# 2. Endpoint. Nebius itself enforces the bearer token at the managed URL, and
#    our backend enforces the SAME value - one token, two layers.
nebius ai endpoint create \
  --parent-id "$PROJECT_ID" \
  --subnet-id "$SUBNET_ID" \
  --name cadence-backend \
  --image "$REGISTRY/cadence-backend:0.4.0" \
  --platform cpu-d3 \
  --preset 2vcpu-8gb \
  --container-port 8787/http \
  --env CADENCE_HOST=0.0.0.0 \
  --env CADENCE_PORT=8787 \
  --env CADENCE_TOKEN="$CADENCE_TOKEN" \
  --env NEBIUS_API_KEY="$NEBIUS_API_KEY" \
  --auth token --token "$CADENCE_TOKEN" \
  --public=false \
  --retries 1

# 3. Wait for RUNNING, then read the URL
nebius ai endpoint get "$ENDPOINT_ID" --format json \
  | jq -r '.status.public_endpoints[] | select(startswith("https://"))'
```

Two limitations, and — since the CLI is now installed — what `nebius ai endpoint
create --help` actually says about each:

- **Durable storage.** The container disk is ephemeral. The flag exists and takes
  a string array: `--volume SOURCE:CONTAINER_PATH[:MODE]`, or
  `s3://BUCKET:/container_path[:MODE[:PROFILE]]`, where `MODE` is `rw` or `ro`
  (default `rw`). So the shape for keeping `memory/` and `progress/` is two
  `--volume` arguments. **What is still unverified is the `SOURCE` form for a
  persistent disk or shared filesystem on this account** — the S3 form is the one
  the help spells out. Run it and check that the data survives a restart; do not
  assume it does.
- **Secret handling.** `--env NEBIUS_API_KEY=...` puts the key in the endpoint
  spec where anyone with read access to the project can see it. The better flag is
  real and documented in the binary: `--env-secret KEY=SECRET_SELECTOR` (a
  MysteryBox secret name, id, or version), with `--token-secret` for the auth
  token. Prefer those, and rotate the key afterwards if you used plain `--env`.

### Tear down

```bash
nebius ai endpoint stop "$ENDPOINT_ID"      # stop serving, keep the config
nebius ai endpoint delete "$ENDPOINT_ID"    # billing stops
```

---

## What this repo still does NOT do

- **No 24/7 public demo.** For the budget above, a public always-on instance is
  not affordable. The `/demo` mode now exists (`CADENCE_DEMO=on`, sample course
  load, 20 questions per visitor), so a demo deployment is a real option — but read
  the two bullets below before pointing the public at one.
- **`CADENCE_DEMO=on` and `CADENCE_TOKEN` cannot both be set.** The server refuses
  to start rather than choosing for you. A demo is public by definition (a visitor
  clicking a link has no token and cannot be given one), so running one means
  running with **no gate on the API** for as long as it is up. The `/demo` page
  itself is behind the token like every other route, which is what stops you
  accidentally serving someone's real memory at a public URL.
- **No secret manager integration.** The key is an env var on the host. That is a
  real limitation worth naming rather than hiding.
- **No autoscaling, and the cap is not a defence.** The per-session request cap
  (`max_requests_per_session_demo`, now enforced in demo mode) is keyed to the
  client address and is spend *bounding* for casual use, not protection from
  someone who wants to burn your credit: a new address is a new budget. A leaked
  token, or a public demo left running, still means real spend — which is why the
  recommendation above is a VM you stop when you are not demoing.
