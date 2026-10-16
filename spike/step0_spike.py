#!/usr/bin/env python3
"""Step 0 spike: prove one (well, two) real Nemotron calls from a script.

What it does, in order:
  1. GET /v1/models and check the three tier model IDs in config/models.json
     actually exist on Nebius Token Factory. This is the authoritative check —
     we never trust a model ID we have not seen in the live listing.
  2. One routing-shaped call on the Nano tier (what the router will do).
  3. One explanation-shaped call on the Super tier (the default tier).
  4. Print tokens, latency, and cost per request for each call.

Usage:
    python3 spike/step0_spike.py              # full spike
    python3 spike/step0_spike.py --list-models   # only verify model IDs (free)
"""

import sys
from pathlib import Path

# The Token Factory client lives in backend/ - it is backend code that the spike
# borrows, not the other way round. One client means one place computes cost.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from nemotron import (  # noqa: E402
    NebiusClient,
    NebiusError,
    cost_usd,
    load_config,
    load_env,
    reply_text,
)

RULE = "-" * 68


def banner(text):
    print("\n" + RULE)
    print(text)
    print(RULE)


def verify_models(client, config):
    """Return (ok, available_ids). Verifies every configured tier exists live."""
    banner("1. Verifying model IDs against the live Nebius catalog")
    try:
        available, latency = client.list_models()
    except NebiusError as exc:
        print("FAILED to list models: {0}".format(exc))
        return False, []

    print("GET {0}{1}  ->  {2} model(s) in {3:.2f}s".format(
        client.base_url, config["provider"]["models_path"], len(available), latency
    ))
    nemotrons = [m for m in available if "nemotron" in m.lower()]
    print("\nNVIDIA Nemotron models available on this account:")
    if nemotrons:
        for model_id in nemotrons:
            print("  - " + model_id)
    else:
        print("  (none found — check the account/key and the catalog)")

    print("\nConfigured tiers:")
    # The API is case-sensitive: a 404 proves a case-insensitive "match" is not
    # good enough. Only an exact match counts, so a near miss is a hard failure.
    exact = set(available)
    near = {}
    for model_id in available:
        near.setdefault(model_id.lower(), model_id)
    ok = True
    for tier, tier_cfg in config["tiers"].items():
        wanted = tier_cfg["model"]
        if wanted in exact:
            print("  [OK]         {0:<5} {1}  {2}".format(
                tier, wanted, tier_cfg["display_name"]))
            continue
        ok = False
        guess = near.get(wanted.lower())
        if guess:
            print("  [WRONG CASE] {0:<5} {1}".format(tier, wanted))
            print("                     live catalog has: {0}".format(guess))
        else:
            print("  [MISSING]    {0:<5} {1}  <- not in the live catalog".format(tier, wanted))
    if not ok:
        print("\nFix config/models.json so every ID matches the catalog EXACTLY, character for")
        print("character (case matters - a wrong-case ID returns HTTP 404 on the call).")
    return ok, available


def try_call(client, config, tier, label, question):
    cfg = config["tiers"][tier]
    print("\n-> {0} | tier={1} | model={2}".format(label, tier, cfg["model"]))
    try:
        response, latency = client.chat(
            tier,
            [
                {"role": "system", "content": "Answer in one short sentence."},
                {"role": "user", "content": question},
            ],
            temperature=0.3,
        )
    except NebiusError as exc:
        print("   FAILED: {0}".format(exc))
        return None

    usage = response.get("usage") or {}
    text = reply_text(response)
    cost = cost_usd(config, tier, usage)
    print("   reply : {0}".format(text or "(empty)"))
    print("   model : {0}".format(response.get("model", "?")))
    print("   tokens: {0} in / {1} out".format(
        usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")
    ))
    print("   time  : {0:.2f}s".format(latency))
    print("   cost  : ${0:.6f} per request".format(cost))
    return {"tier": tier, "text": text, "usage": usage, "cost": cost, "latency": latency}


def main():
    load_env()
    config = load_config()
    list_only = "--list-models" in sys.argv

    print("Step 0 spike — Nebius Token Factory x NVIDIA Nemotron")
    print("provider : {0}".format(config["provider"]["base_url"]))
    print("key env  : {0} (value never printed)".format(config["provider"]["api_key_env"]))

    try:
        client = NebiusClient(config)
    except NebiusError as exc:
        print("\nBLOCKED: {0}".format(exc))
        return 2

    models_ok, _ = verify_models(client, config)
    if list_only:
        return 0 if models_ok else 1
    if not models_ok:
        print("\nStopping before spending anything: model IDs must be confirmed first.")
        return 1

    banner("2. Nano tier — the shape the router will use")
    nano = try_call(
        client, config, "nano", "routing / short reply",
        "Reply with exactly: ROUTER OK",
    )

    banner("3. Super tier — the default for explanations")
    super_ = try_call(
        client, config, "super", "explanation",
        "In one sentence, what is spaced repetition?",
    )

    banner("Summary")
    total = 0.0
    for result in (nano, super_):
        if result:
            total += result["cost"]
            print("  {0:<6} {1:>9.2f}s  ${2:.6f}  {3}".format(
                result["tier"], result["latency"], result["cost"],
                (result["text"][:52] + "...") if len(result["text"]) > 55 else result["text"],
            ))
        else:
            print("  one tier FAILED — see above")
    if nano and super_:
        print("\n  Spike cost: ${0:.6f} of the ${1:.0f} credit budget.".format(
            total, config["budgets"]["total_credit_usd"]
        ))
        print("  At this size you get roughly {0:,} such requests.".format(int(0.01 / total * 100)))
        print("\nSTEP 0 SPIKE (LLM path): PASS")
        return 0
    print("\nSTEP 0 SPIKE (LLM path): PARTIAL — see failures above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
