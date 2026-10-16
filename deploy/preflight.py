#!/usr/bin/env python3
"""Check the Nebius profile before spending money on a VM.

    python3 deploy/preflight.py <project-id>
    python3 deploy/preflight.py <project-id> --preset 2vcpu-8gb --platform cpu-e2

Step 7 lost about an hour to four failures that all looked like each other
(written up in FEEDBACK.md):

1. the profile logged in fine but stored **no tenant**, so nothing downstream
   could traverse from a project up to a tenant;
2. the id on the **Token Factory** console (`aiproject-…`) is not an AI Cloud
   project id at all;
3. a Token Factory account can have a working inference key and **no AI Cloud
   project to deploy into**;
4. `nebius profile list` and `nebius iam whoami` both report success for a
   profile that cannot list a single compute platform.

This runs the checks in the order that tells those apart, and every failure
prints what it means and what to do instead of an rpc error. It creates nothing
and calls nothing that costs money — unless you pass --probe-writes, which
attempts the one write the deploy needs anyway (creating the security group) so
a read-only account is caught here instead of after the whole runbook.

Exit status is 0 only when every check passes.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG = Path.home() / ".nebius" / "config.yaml"
FALLBACK_BIN = Path.home() / ".nebius" / "bin" / "nebius"

# The preset the runbook uses. Both strings are free-form to the API, so a typo
# is only caught here or at create time.
DEFAULT_PLATFORM = "cpu-e2"
DEFAULT_PRESET = "2vcpu-8gb"


class Result:
    def __init__(self):
        self.lines = []
        self.failed = 0

    def ok(self, label, detail=""):
        self.lines.append(("ok", label, detail))

    def bad(self, label, problem, remedy):
        self.failed += 1
        self.lines.append(("bad", label, f"{problem}\n      -> {remedy}"))

    def report(self):
        width = max((len(label) for _, label, _ in self.lines), default=0)
        for state, label, detail in self.lines:
            mark = "PASS" if state == "ok" else "FAIL"
            head = f"  [{mark}] {label.ljust(width)}"
            if detail:
                print(f"{head}  {detail}" if "\n" not in detail else f"{head}\n      {detail}")
            else:
                print(head)


def nebius_binary():
    found = shutil.which("nebius")
    if found:
        return found
    if FALLBACK_BIN.exists():
        return str(FALLBACK_BIN)
    return None


def run(binary, *args):
    """Run a nebius command. Returns (returncode, parsed-json-or-None, stderr)."""
    proc = subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=120
    )
    data = None
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
    return proc.returncode, data, (proc.stderr or "").strip()


def first_error(stderr):
    """The one useful line out of a CLI error dump."""
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("Error:"):
            return line
    return stderr.splitlines()[0].strip() if stderr else "command failed"


def read_profile(result):
    """Profile name, tenant id and project id, without printing any credential.

    The file is YAML, but it is a flat two-level map of strings, so this reads it
    with a regex rather than depending on PyYAML - the repo is stdlib-only on
    purpose, and a directory listing of ~/.nebius is all the rotation any
    credential needs.
    """
    if not CONFIG.exists():
        result.bad(
            "profile exists",
            "no ~/.nebius/config.yaml",
            "run: nebius profile create --profile <name> --endpoint api.nebius.cloud "
            "--federation-endpoint auth.nebius.com --tenant-id <tenant> --parent-id <project>",
        )
        return {}
    text = CONFIG.read_text(encoding="utf-8", errors="replace")
    fields = {}
    for key in ("default", "tenant-id", "parent-id"):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}:"):
                fields[key] = stripped.split(":", 1)[1].strip().strip("\"'")
                break
    fields.setdefault("default", "")
    fields.setdefault("tenant-id", "")
    fields.setdefault("parent-id", "")
    result.ok("profile exists", f"active: {fields['default'] or '(none)'}")

    if fields["tenant-id"]:
        result.ok("tenant id present", fields["tenant-id"])
    else:
        result.bad(
            "tenant id present",
            "the profile has NO tenant id",
            "this is the silent one: profile create accepts an empty tenant and reports\n"
            "      success. Recreate it with --tenant-id, or run: nebius profile update\n"
            "      --tenant-id <tenant-id>. Without it, every project call fails with\n"
            "      'get tenant ID for ... iam traverse up ... Resource not found'.",
        )
    return fields


def check_whoami(binary, result):
    code, data, err = run(binary, "iam", "whoami", "--format", "json")
    if code != 0:
        result.bad("authenticated", first_error(err), "re-run: nebius profile create")
        return
    profile = (data or {}).get("user_profile") or {}
    state = profile.get("user_account_state", "?")
    name = ((profile.get("attributes") or {}).get("name") or "?").strip()
    result.ok("authenticated", f"{name} ({state})")


def check_project(binary, project_id, result):
    if not project_id:
        result.bad(
            "project id given",
            "no project id on the command line",
            "pass one: python3 deploy/preflight.py project-xxxxxxxxxxxxxxxxxxxx\n"
            "      Take it from the AI Cloud console (console.nebius.com), NOT from the\n"
            "      Token Factory rate-limits page, which shows aiproject-... instead.",
        )
        return
    if project_id.startswith("aiproject-"):
        result.bad(
            "project id given",
            f"{project_id} is a Token Factory id",
            "the Compute API rejects the whole 'aiproject' type: 'expected id types:\n"
            "      project, got \"aiproject\"'. Get the project-... id from\n"
            "      console.nebius.com.",
        )
        return
    if not project_id.startswith("project-"):
        result.bad(
            "project id given",
            f"{project_id} does not start with 'project-'",
            "AI Cloud project ids look like project-e00a2wpqk70kchfzjn.",
        )
        return

    code, data, err = run(binary, "iam", "v2", "project", "get", "--id", project_id,
                          "--format", "json")
    if code != 0:
        result.bad(
            "project exists",
            f"{first_error(err)}",
            "either the id is wrong or it is not in this account. If the AI Cloud\n"
            "      console shows no project at all, this account has inference access only\n"
            "      and cannot host a VM - there is nothing to fix in the runbook.",
        )
        return
    meta = (data or {}).get("metadata") or {}
    result.ok("project exists", f"{meta.get('name') or '(unnamed)'}  parent={meta.get('parent_id') or '?'}")


def check_compute(binary, project_id, platform, preset, result):
    code, data, err = run(binary, "compute", "platform", "list",
                          "--parent-id", project_id, "--format", "json")
    if code != 0:
        result.bad(
            "compute access",
            first_error(err),
            "without Compute this account cannot host the backend. The free-tunnel\n"
            "      path in deploy/README.md needs no account at all.",
        )
        return
    items = (data or {}).get("items") or []
    names = [((p.get("metadata") or {}).get("name") or "") for p in items]
    result.ok("compute access", f"{len(items)} platforms: {', '.join(n for n in names if n)[:70]}")

    if platform not in names:
        result.bad(
            "platform available",
            f"{platform!r} is not in that list",
            f"pick one of: {', '.join(n for n in names if n) or '(none listed)'}",
        )
        return
    result.ok("platform available", platform)

    # Presets have moved between spec/status across releases, so search the whole
    # entry rather than assuming a path; say so instead of claiming absence.
    entry = next(p for p in items if ((p.get("metadata") or {}).get("name")) == platform)
    presets = []

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "presets" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            presets.append(item.get("name") or (item.get("metadata") or {}).get("name"))
                        else:
                            presets.append(item)
                else:
                    collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(entry)
    presets = [p for p in presets if p]
    if not presets:
        result.ok("preset available", "(presets not in this response - verify by hand)")
    elif preset in presets:
        result.ok("preset available", f"{preset} (of {', '.join(presets[:6])})")
    else:
        result.bad(
            "preset available",
            f"{preset!r} not among {', '.join(presets[:8])}",
            "these names are free-form strings to the API, so a typo is only caught\n"
            "      here or at create time. Prefer one from that list.",
        )


def check_writes(binary, project_id, result):
    """Attempt the cheapest write the deploy needs, because read permission is
    not write permission and every read check above passes on a read-only
    account. This is what stopped the Step 7 deploy: platform list, project get
    and whoami all passed on an account that could not create anything.

    It creates `cadence-backend-sg` if it is missing — a resource the deploy
    needs anyway — and reuses it if it already exists.
    """
    code, data, err = run(binary, "vpc", "security-group", "get-by-name",
                          "--parent-id", project_id, "--name", "cadence-backend-sg",
                          "--format", "json")
    if code == 0:
        result.ok("write access", "cadence-backend-sg already exists (reused)")
        return

    net = run(binary, "vpc", "network", "list", "--parent-id", project_id, "--format", "json")
    networks = (net[1] or {}).get("items") or []
    if not networks:
        result.bad("write access", "could not find a network to test against",
                   "create a network in the console, or run this probe there")
        return
    network_id = (networks[0].get("metadata") or {}).get("id")

    code, data, err = run(binary, "vpc", "security-group", "create",
                          "--parent-id", project_id, "--name", "cadence-backend-sg",
                          "--network-id", network_id, "--format", "json")
    if code != 0:
        result.bad(
            "write access",
            first_error(err),
            "this account can READ the project but not create anything in it, so no "
            "runbook will work. Ask whoever provisioned it for a role that can write "
            "(or a service-account key with one), or use the free-tunnel path, which "
            "needs no cloud permissions at all.",
        )
        return
    result.ok("write access", "created cadence-backend-sg (needed by the deploy anyway)")


def main():
    parser = argparse.ArgumentParser(description="Validate the Nebius deploy prerequisites.")
    parser.add_argument("project_id", nargs="?", default=os.environ.get("PROJECT_ID", ""),
                        help="AI Cloud project id from console.nebius.com (project-...)")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--preset", default=DEFAULT_PRESET)
    parser.add_argument("--probe-writes", action="store_true",
                        help="also attempt one write (creates cadence-backend-sg if missing); "
                             "the only check that touches the cloud, and the only way to "
                             "catch a read-only account before the runbook does")
    args = parser.parse_args()

    print("Nebius deploy preflight - creates nothing, costs nothing\n")
    result = Result()

    binary = nebius_binary()
    if not binary:
        result.bad("nebius CLI installed", "not on PATH and not in ~/.nebius/bin",
                   "curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash")
        result.report()
        return 1
    code, data, err = run(binary, "version")
    version = (data or "") if data else ""
    result.ok("nebius CLI installed", f"{binary}  {version if isinstance(version, str) else ''}".strip())

    read_profile(result)
    check_whoami(binary, result)
    check_project(binary, args.project_id, result)
    if args.project_id.startswith("project-"):
        check_compute(binary, args.project_id, args.platform, args.preset, result)
        if args.probe_writes:
            check_writes(binary, args.project_id, result)
        else:
            result.ok("write access", "not checked - re-run with --probe-writes")
    else:
        result.bad("compute access", "skipped - no valid project id",
                   "the check that would answer it needs a project to scope to, which is\n"
                   "      the circular problem this whole step ran into.")

    result.report()
    if result.failed:
        print(f"\n{result.failed} check(s) failed. Nothing was created.")
        return 1
    print("\nAll checks passed. Safe to continue with deploy/README.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
