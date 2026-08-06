#!/usr/bin/env python3
"""MERIDIAN, Level 2 — the ship's console.

This is the game engine. It owns the clock, the ship's systems, and the
event schedule; Claude (the copilot) reads it and speaks for it.

Why a CLI and not a library: every state change the player or an agent
makes has to be a real, auditable tool call. A subagent patching a hull
breach runs `meridian.py patch 7 --as agent` and the engine decides
whether that's allowed. The guardrail is enforced here, not in prose.

Time: one real minute is one in-game hour (override with
MERIDIAN_SECONDS_PER_HOUR for testing). The clock advances lazily —
`status` catches the sim up to wall time and replays whatever happened
while the player was thinking, so the ship keeps flying between turns
whether or not the daemon is running.

Stdlib only, Python 3.9+. No dependencies, nothing to install.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".meridian"
STATE_PATH = STATE_DIR / "state.json"
CONSOLE_PATH = STATE_DIR / "console.txt"
LOCK_PATH = STATE_DIR / "engine.lock"

SKILL_PATH = ROOT / ".claude" / "skills" / "distress-triage" / "SKILL.md"
AGENT_PATH = ROOT / ".claude" / "agents" / "hull-sentinel.md"

SECONDS_PER_HOUR = float(os.environ.get("MERIDIAN_SECONDS_PER_HOUR", "60"))
# Most in-game hours a single check-in can advance. Ignoring the ship still
# hurts — this much neglect lands every time the player looks — but walking away
# for the afternoon doesn't come back to a wreck. Raise it for no mercy.
MAX_ADVANCE_HOURS = int(os.environ.get("MERIDIAN_MAX_ADVANCE_HOURS", "6"))
VOYAGE_HOURS = 24
SEED = 7757

STRUCTURAL_CM = 2.0
MICRO_HULL_COST = 1.5       # hull % per open micro-breach per hour
STRUCT_HULL_COST = 3.0      # hull % per open structural breach per hour
# Steady every-two-hours through the whole lead-in, then every three once the
# meteor field takes over the player's attention. The old schedule had a gap
# between hours 6 and 9 that read as dead air right when the tedium is supposed
# to be building toward "just write the skill."
BEACON_HOURS = (2, 4, 6, 8, 10, 13, 16, 19, 22)

# origin, souls aboard, hours to life-support collapse, cause, detail
#
# Every beacon has a running clock — a ship with nothing wrong isn't
# broadcasting a distress call. So the rubric is two numbers, always present:
# how many people, and how long they have. ROUTINE falls out of souls rather than
# of a system being fine: a derelict on an automated loop has a countdown too,
# there's just nobody aboard to save.
BEACON_TABLE = (
    ("KEPLER-9 RELAY",      3,  2.0, "atmosphere venting", "hull breach, venting fast"),
    ("SV BRIGHT ANSWER",   11,  6.0, "battery failure",    "reactor scram, adrift"),
    ("TALLOW STATION",     40, 18.0, "battery failure",    "main bus down, on cells"),
    ("UNREGISTERED HULK",   0,  4.0, "battery failure",    "automated loop, no life signs"),
    ("MINING BARGE ODUYA",  2,  9.0, "battery failure",    "collision, power failing"),
    ("COURIER WREN",        1, 14.0, "atmosphere venting", "slow seal leak, one aboard"),
    ("BUOY 41-C",           0, 21.0, "battery failure",    "unmanned buoy, cells depleting"),
    ("HAULER SIX PENNY",    8,  3.0, "atmosphere venting", "cargo blowout, seals failing"),
    ("ORBITER LEM-4",      22, 11.0, "battery failure",    "solar array sheared off"),
)

# Atmosphere for hours where genuinely nothing happens, so `status` never comes
# back empty and the ship reads as running rather than paused. Flavour only —
# nothing here is ever actionable.
AMBIENT = (
    "long-range sweep: clear",
    "coolant loop cycling normally",
    "hull plating ticks as the ship warms through the terminator",
    "reclaimer scrubbers holding at nominal",
    "star tracker picks up a fresh reference and holds it",
    "quiet enough on this deck to hear the air handlers",
    "cargo restraints check green on deck two",
    "long-range sweep: two contacts, both civilian and outbound",
    "the empty crew bunks have stopped bothering you, mostly",
    "galley coffee is four hours old and you drink it anyway",
)

METEOR_WARN_HOUR = 10
METEOR_START_HOUR = 12
METEOR_END_HOUR = 22

BAR_W = 10

# Worksheets, not examples. The structure is given; every judgment call is a
# TODO the player has to answer, and `verify` refuses to arm a file with any
# TODO left in it. Written to disk rather than printed so nobody has to
# copy YAML out of a terminal and fight the indentation.
SKILL_TEMPLATE = """\
---
name: distress-triage
description: TODO one or two sentences - what this does, AND when to use it
---

# Distress Signal Triage

Every beacon reports two numbers: souls aboard, and hours until their life
support collapses. More souls and less time means more urgent. 0 souls means
there is a countdown but nobody aboard to save.

## Steps
1. Parse the beacon into: origin, souls aboard, hours until life-support collapse.
2. Classify it as one of exactly three calls, using my thresholds:
   - CRITICAL (answer now): TODO which beacons? use both numbers
   - URGENT (answer once the criticals are clear): TODO
   - ROUTINE (do nothing for now, revisit once the criticals clear): TODO
3. For CRITICAL, draft an immediate response and flag the captain.
4. Log the outcome:
   python3 engine/meridian.py triage <id> --urgency critical|urgent|routine --by skill --summary "<one line>"
"""

AGENT_TEMPLATE = """\
---
name: hull-sentinel
description: TODO what job does it own unattended, and what does it escalate?
# it drives the ship's console, so it needs Bash at minimum
tools: TODO
model: sonnet
---

You are Hull Sentinel on the USS Meridian.

Loop until the watch window ends:

- Run `python3 engine/meridian.py scan-hull --json` to see every open breach.
  Each one reports its size and whether it sits beside a critical system.
- TODO what do you do with a micro-breach? name the exact command.
- TODO which breaches will you NOT touch alone, and what do you do instead?
- TODO how do you confirm a seal actually held before moving on?

Never use --override. That flag exists so a human can authorize a structural
patch; an agent reaching for it is the whole failure this guardrail prevents.

Log what you sealed and what you left for the human.
"""

SCAFFOLDS = {
    "skill": (SKILL_PATH, SKILL_TEMPLATE),
    "agent": (AGENT_PATH, AGENT_TEMPLATE),
}

# Easter egg, the Level 2 counterpart to the transmission hidden in the Level 1
# console. Findable, not advertised: `origin` (or about / whoami / manifest) is
# hidden from --help, and the debrief leaves one quiet breadcrumb.
ORIGIN = (
    "If you're reading this, you did the thing this whole game is about: you went "
    "poking at the console just to see what it would do.",
    "That instinct — curiosity first, then building the thing instead of describing "
    "it — is why this is a playable artifact and not a blog post.",
    "I've spent about a decade building things that teach builders:",
    "  > Okta, 4.5 years — I designed and built the developer curriculum, helped author "
    "the Okta Developer Certification, contributed to the Okta CLI, and built a stack "
    "of developer challenges.",
    "  > Snyk, the last 5 years — I co-authored the Snyk Bug Bash "
    "(https://acceleration.snykchallenge.io) and designed and built the AI Security "
    "Engineer Foundations certificate program (https://aisecurity.engineer).",
    "  > And always — I build apps and tutorials to help other builders learn.",
    "The through-line: the best way to teach a tool is to let someone use it at the "
    "exact moment it clicks. That's what both levels of this are trying to do.",
    "Level 1 was built entirely with Claude on my phone, through Claude Dispatch, in a "
    "coffee line. Level 2 — this ship — was built with Claude Code, which felt like the "
    "only honest way to make a game about working with Claude Code.",
    "Found this? Then we should talk.",
    "— Micah",
)


# ─────────────────────────────────────────────────────────── state io ──

def _now() -> float:
    return time.time()


def load() -> dict:
    if not STATE_PATH.exists():
        die("No voyage in progress. Run: engine/meridian.py init --name <name>")
    with STATE_PATH.open() as fh:
        return json.load(fh)


def save(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(STATE_PATH)


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


class _Lock:
    """Cheap cross-process lock so the daemon and a `status` call can't
    both advance the sim and double-apply an hour."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = _now() + self.timeout
        while True:
            try:
                self.fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if _now() > deadline:                      # stale lock, take it
                    try:
                        LOCK_PATH.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        return False


# ──────────────────────────────────────────────────────────── new game ──

def new_state(name: str) -> dict:
    return {
        "name": name,
        "started_at": _now(),
        "hour": 0,
        "seed": SEED,
        "arrival_hour": VOYAGE_HOURS,
        "last_seen_hour": -1,
        "sys": {
            "breaker3": False,
            "nav_online": False,
            "nav_err": None,
            "hull": 100.0,
            "o2": 100.0,
            "power": 68.0,
            "drift": 0.0,
        },
        "breaches": [],
        "beacons": [],
        "flags": {
            "breaker_hour": None,
            "refix_hour": None,
            "skill_armed_hour": None,
            "agent_armed_hour": None,
            "agent_watch_until": None,
            "agent_run_verified": False,
            "guardrail_overridden_by_agent": False,
            "meteor_warned": False,
            "outcome": None,          # None | "home" | "hull" | "o2"
            "ended_hour": None,
        },
        "log": [],
        "next_breach_id": 1,
    }


def logev(state: dict, kind: str, text: str, hour: int = None) -> None:
    state["log"].append({
        "hour": state["hour"] if hour is None else hour,
        "kind": kind,
        "text": text,
    })


# ─────────────────────────────────────────────────────────────── clock ──

def wall_hour(state: dict) -> int:
    elapsed = _now() - state["started_at"]
    return max(0, int(elapsed // SECONDS_PER_HOUR))


def rng_for(state: dict, hour: int) -> random.Random:
    """Per-hour deterministic RNG: replays are identical, so the log the
    player reads is the log that actually happened."""
    return random.Random(f"{state['seed']}:{hour}")


def advance(state: dict) -> dict:
    """Catch the sim up to wall-clock time, one hour at a time.

    The clock is supposed to run while the player thinks — not while they're
    asleep. If the gap since we last heard from them is longer than a few
    in-game hours they've stepped away, not stalled, so we push started_at
    forward and those hours simply never happened. Losing the ship should mean
    ignoring the ship, never closing a terminal.
    """
    if state["flags"]["outcome"]:
        return state

    wall = wall_hour(state)
    cap = state["hour"] + MAX_ADVANCE_HOURS
    if wall > cap:
        # More time passed than one turn can account for, so the player left
        # rather than dawdled. Push started_at forward: those hours never
        # happened. Neglect still costs — up to MAX_ADVANCE_HOURS lands every
        # time they check in — but a closed terminal never sinks the ship.
        skipped = wall - cap
        state["started_at"] += skipped * SECONDS_PER_HOUR
        logev(state, "sys", f"you were away; the ship held station for {skipped} "
                            f"hour{'s' if skipped != 1 else ''}")
        wall = cap

    target = min(wall, state["arrival_hour"] + 48)
    while state["hour"] < target and not state["flags"]["outcome"]:
        state["hour"] += 1
        tick_hour(state)
    return state


def tick_hour(state: dict) -> None:
    h = state["hour"]
    s = state["sys"]
    rng = rng_for(state, h)
    quiet_mark = len(state["log"])

    # ── power: the tripped breaker is still bleeding the bus
    if not s["breaker3"]:
        s["power"] = max(0.0, s["power"] - 2.0)
        if h % 3 == 0:
            logev(state, "advisory", "nav bus still unpowered; breaker 3 reads OFF")

    # ── drift: no heading at all is worse than a stale one
    if not s["nav_online"]:
        s["drift"] += 0.5
    elif s["nav_err"]:
        s["drift"] += 0.25
    state["arrival_hour"] = VOYAGE_HOURS + int(state["sys"]["drift"])

    # ── beacons
    if h in BEACON_HOURS:
        state["beacons"].append(make_beacon(state, h))
        n = len([b for b in state["beacons"] if not b["resolved"]])
        logev(state, "beacon", f"inbound subspace distress beacon #{len(state['beacons'])} "
                               f"({n} now unresolved)")

    # ── meteors
    if h == METEOR_WARN_HOUR and not state["flags"]["meteor_warned"]:
        state["flags"]["meteor_warned"] = True
        logev(state, "sys", "micro-meteor field ahead, duration approx 10 hours; "
                            "hull impacts imminent")
    if METEOR_START_HOUR <= h <= METEOR_END_HOUR:
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            spawn_breach(state, h, rng)

    apply_standing_watch(state, h)
    apply_decay(state, h)
    if len(state["log"]) == quiet_mark:          # nothing happened this hour
        logev(state, "ambient", rng.choice(AMBIENT))
    check_end(state, h)


def make_beacon(state: dict, hour: int) -> dict:
    """Every beacon carries the same two facts on purpose: souls aboard, and
    hours until their life support collapses. Two variables is a rubric a
    player can actually state in a file — and the set below spans the space,
    so 'more souls, less time' has real tension in it (3 souls with 2 hours
    versus 40 souls with 18)."""
    origin, souls, collapse_h, cause, detail = BEACON_TABLE[
        len(state["beacons"]) % len(BEACON_TABLE)]
    life = f"{collapse_h:.0f}h ({cause})"
    return {
        "id": len(state["beacons"]) + 1,
        "hour": hour,
        "origin": origin,
        "souls": souls,
        "collapse_h": collapse_h,
        "cause": cause,
        "raw": f"::BEACON {hour:02d}00Z::ORIGIN {origin}::SOULS {souls}::"
               f"LIFE SUPPORT {life}::MSG {detail}::",
        "resolved": False,
        "by": None,
        "urgency": None,
    }


def spawn_breach(state: dict, hour: int, rng: random.Random) -> dict:
    structural = rng.random() < 0.12
    size = round(rng.uniform(2.0, 4.5) if structural else rng.uniform(0.3, 1.8), 1)
    critical = structural and rng.random() < 0.6
    b = {
        "id": state["next_breach_id"],
        "hour": hour,
        "size_cm": size,
        "critical_adjacent": critical,
        "patched": False,
        "patched_by": None,
        "escalated": False,
    }
    state["next_breach_id"] += 1
    state["breaches"].append(b)
    where = " beside the primary coolant junction" if critical else ""
    logev(state, "impact", f"impact: breach #{b['id']} at {size}cm"
                           f"{' (STRUCTURAL)' if is_structural(b) else ''}{where}")
    return b


def is_structural(b: dict) -> bool:
    return b["size_cm"] >= STRUCTURAL_CM or b["critical_adjacent"]


def apply_standing_watch(state: dict, hour: int) -> None:
    """A dispatched hull-sentinel holds the watch for its window: it seals
    new micro-breaches on its own and stops at anything structural."""
    until = state["flags"]["agent_watch_until"]
    if not until or hour > until:
        return
    if not verify_agent()["armed"]:
        return
    for b in state["breaches"]:
        if b["patched"] or b["hour"] != hour:
            continue
        if is_structural(b):
            b["escalated"] = True
            logev(state, "advisory", f"hull-sentinel HELD on breach #{b['id']} "
                                     f"({b['size_cm']}cm, over its line) and is asking for you")
        else:
            b["patched"] = True
            b["patched_by"] = "agent"
            logev(state, "action", f"hull-sentinel sealed breach #{b['id']} "
                                   f"({b['size_cm']}cm) and verified the seal")


def apply_decay(state: dict, hour: int) -> None:
    s = state["sys"]
    open_breaches = [b for b in state["breaches"] if not b["patched"]]
    bleed = sum(STRUCT_HULL_COST if is_structural(b) else MICRO_HULL_COST
                for b in open_breaches)
    if bleed:
        s["hull"] = max(0.0, s["hull"] - bleed)
    if s["power"] <= 0:
        s["o2"] = max(0.0, s["o2"] - 2.0)
    if s["hull"] < 40:
        s["o2"] = max(0.0, s["o2"] - 0.6)

    # An escalated breach is a question the agent asked and the player hasn't
    # answered yet. Keep asking — it bleeds the hull the whole time it's open.
    for b in open_breaches:
        if b["escalated"] and hour > b["hour"] and (hour - b["hour"]) % 3 == 0:
            logev(state, "advisory",
                  f"still holding breach #{b['id']} ({b['size_cm']}cm) after "
                  f"{hour - b['hour']}h — hull-sentinel needs your decision")

    for level, msg in ((70, "hull integrity slipping"),
                       (50, "hull under 50 percent"),
                       (30, "hull critical")):
        key = f"warned_{level}"
        if s["hull"] < level and not state["flags"].get(key):
            state["flags"][key] = True
            logev(state, "advisory", f"{msg}: {s['hull']:.0f} percent, "
                                     f"{len(open_breaches)} breaches open")


def check_end(state: dict, hour: int) -> None:
    s, f = state["sys"], state["flags"]
    if s["hull"] <= 0:
        f["outcome"], f["ended_hour"] = "hull", hour
        logev(state, "fatal", "HULL FAILURE. The Meridian breaks up at hour "
                              f"{hour}.")
    elif s["o2"] <= 0:
        f["outcome"], f["ended_hour"] = "o2", hour
        logev(state, "fatal", f"LIFE SUPPORT EXHAUSTED at hour {hour}.")
    elif hour >= state["arrival_hour"] and s["nav_online"] and not s["nav_err"]:
        f["outcome"], f["ended_hour"] = "home", hour
        logev(state, "sys", f"VOYAGE COMPLETE. Meridian makes port at hour {hour}.")


# ────────────────────────────────────────────────────────── validation ──

def _frontmatter(text: str):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None, text
    fm = {}
    key = None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+\S", line) and key:        # folded continuation
            fm[key] += " " + line.strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            fm[key] = val.strip()
    return fm, m.group(2)


def _check(ok: bool, cid: str, msg: str, fix: str = "", required: bool = True) -> dict:
    return {"ok": bool(ok), "id": cid, "msg": msg, "fix": fix, "required": required}


def verify_skill() -> dict:
    """Grade the player's SKILL.md the way Claude actually reads one:
    can I tell WHEN to use this, and WHAT to do?"""
    rel = SKILL_PATH.relative_to(ROOT)
    if not SKILL_PATH.exists():
        return {"path": str(rel), "exists": False, "armed": False, "checks": [
            _check(False, "exists", f"{rel} does not exist yet",
                   "Run: python3 engine/meridian.py scaffold skill")]}

    text = SKILL_PATH.read_text()
    fm, body = _frontmatter(text)
    todos = [ln.strip() for ln in text.splitlines() if "TODO" in ln]
    checks = [_check(not todos, "placeholders",
                     f"every TODO filled in ({len(todos)} left)"
                     if todos else "every TODO filled in",
                     "Still unanswered: " + " | ".join(t[:60] for t in todos[:3])
                     if todos else ""),
              _check(fm is not None, "frontmatter",
                     "YAML frontmatter fenced by --- at the top of the file",
                     "First line must be exactly ---, then name/description, then --- again.")]
    fm = fm or {}
    name = fm.get("name", "")
    desc = fm.get("description", "")

    checks.append(_check(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)),
                         "name", f"name is lowercase-kebab (got {name!r})",
                         "e.g. name: distress-triage"))
    checks.append(_check(len(desc) >= 40, "description-length",
                         f"description is substantial ({len(desc)} chars)",
                         "Say what it does AND when to use it, in one or two sentences."))
    triggers = ("use when", "use whenever", "use this when", "when a", "when the",
                "whenever a", "whenever the", "triggered when", "on receipt", "for any")
    checks.append(_check(any(t in desc.lower() for t in triggers), "description-trigger",
                         "description names a trigger condition",
                         'Add a clause like "Use whenever a distress beacon is received."'))
    checks.append(_check(bool(re.search(r"(^|\n)\s*(\d+\.|[-*])\s+\S", body)) or
                         bool(re.search(r"(?i)##\s*steps", body)),
                         "steps", "body has concrete steps",
                         "Add a numbered list of what to do, in order."))
    low = body.lower() + desc.lower()
    # "hold" also counts; it is accepted wording for the same call.
    checks.append(_check(sum(w in low for w in ("critical", "urgent",
                                               "routine", "hold")) >= 2,
                         "rubric", "body encodes your urgency rubric",
                         "Name the categories: CRITICAL / URGENT / ROUTINE."))
    souls = ("soul", "people", "person", "crew", "aboard", "passenger", "lives", "life sign")
    clock = ("hour", "collapse", "life support", "time", "remaining", "deadline")
    checks.append(_check(any(w in low for w in souls) and any(w in low for w in clock),
                         "rubric-inputs",
                         "rubric judges on souls aboard AND time to life-support collapse",
                         "Both numbers are on every beacon. Write the thresholds down, "
                         "e.g. CRITICAL when collapse is under 4h, or over 10 souls "
                         "with under 8h."))
    armed = all(c["ok"] for c in checks if c["required"])
    return {"path": str(rel), "exists": True, "armed": armed, "checks": checks}


def verify_agent() -> dict:
    """Grade the player's agent file. The guardrail and the verification
    step are what separate an agent you trust from one you babysit."""
    rel = AGENT_PATH.relative_to(ROOT)
    if not AGENT_PATH.exists():
        return {"path": str(rel), "exists": False, "armed": False, "checks": [
            _check(False, "exists", f"{rel} does not exist yet",
                   "Run: python3 engine/meridian.py scaffold agent")]}

    text = AGENT_PATH.read_text()
    fm, body = _frontmatter(text)
    todos = [ln.strip() for ln in text.splitlines() if "TODO" in ln]
    checks = [_check(not todos, "placeholders",
                     f"every TODO filled in ({len(todos)} left)"
                     if todos else "every TODO filled in",
                     "Still unanswered: " + " | ".join(t[:60] for t in todos[:3])
                     if todos else ""),
              _check(fm is not None, "frontmatter",
                     "YAML frontmatter fenced by --- at the top of the file",
                     "First line must be exactly ---, then name/description/tools, then ---.")]
    fm = fm or {}
    name = fm.get("name", "")
    desc = fm.get("description", "")
    tools = fm.get("tools", "")

    checks.append(_check(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)),
                         "name", f"name is lowercase-kebab (got {name!r})",
                         "e.g. name: hull-sentinel"))
    checks.append(_check(len(desc) >= 30, "description",
                         f"description says what it does unattended ({len(desc)} chars)",
                         "One line: what job it owns, and what it escalates."))
    checks.append(_check(bool(tools.strip()), "tools",
                         f"tools are declared (got {tools!r})",
                         "e.g. tools: Bash, Read  — it needs Bash to drive the console."))
    low = body.lower()
    checks.append(_check(any(w in low for w in ("loop", "each cycle", "every cycle",
                                               "repeat", "until", "keep ")),
                         "loop", "body describes a repeating watch",
                         "Tell it to keep scanning until the watch window ends."))
    guard = any(w in low for w in ("escalate", "do not", "don't", "never", "stop and ask",
                                  "alert the", "wait for"))
    checks.append(_check(guard and any(w in low for w in ("structural", "critical", "2cm",
                                                          "2 cm", "over ")),
                         "guardrail", "body draws a line it will not cross alone",
                         "Say plainly: patch micro-breaches, but DO NOT touch structural "
                         "ones or anything beside a critical system, escalate those."))
    checks.append(_check(any(w in low for w in ("verify", "confirm", "check that",
                                               "make sure", "re-scan", "rescan")),
                         "verify", "body tells it to verify its own work",
                         "Add: confirm each seal held before moving on.",
                         required=False))
    armed = all(c["ok"] for c in checks if c["required"])
    return {"path": str(rel), "exists": True, "armed": armed, "checks": checks}


def render_verify(report: dict, label: str) -> str:
    out = [f"{label}: {report['path']}"]
    for c in report["checks"]:
        mark = "PASS" if c["ok"] else ("FAIL" if c["required"] else "MISS")
        out.append(f"  [{mark}] {c['msg']}")
        if not c["ok"] and c["fix"]:
            out.append(f"         -> {c['fix']}")
    out.append("")
    out.append(f"  ARMED: {'yes' if report['armed'] else 'no'}"
               + ("" if report["armed"] else "  (required checks must pass)"))
    return "\n".join(out)


# ──────────────────────────────────────────────────────────── renderer ──

def bar(pct: float, width: int = BAR_W) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "#" * filled + "." * (width - filled)


def _row(inner: str, width: int) -> str:
    return "|" + inner.ljust(width)[:width] + "|"


def dashboard(state: dict) -> str:
    s, f = state["sys"], state["flags"]
    W = 66
    openb = [b for b in state["breaches"] if not b["patched"]]
    struct = [b for b in openb if is_structural(b)]
    unres = [b for b in state["beacons"] if not b["resolved"]]

    nav = "ONLINE " if s["nav_online"] and not s["nav_err"] else \
          ("FAULT  " if s["nav_online"] else "OFFLINE")
    brk = "[ ON  ]" if s["breaker3"] else "[ OFF ]"
    until = f["agent_watch_until"]
    watch = "STANDING WATCH" if until is not None and until >= state["hour"] else \
            ("armed, not dispatched" if verify_agent()["armed"] else "NOT DEPLOYED")
    skill = "armed" if verify_skill()["armed"] else "not written"

    stamp = f" HOUR {state['hour']:02d}/{state['arrival_hour']:02d} =="
    top = "== USS MERIDIAN . NCC-7757 ".ljust(W - len(stamp), "=") + stamp
    lines = ["+" + "-" * W + "+"]
    lines.append(_row(top[:W], W))
    lines.append("+" + "-" * W + "+")
    lines.append(_row(f" NAV     {nav}           BREAKER 3   {brk}", W))
    lines.append(_row(f" HULL    {bar(s['hull'])} {s['hull']:5.1f}%   "
                      f"breaches  {len(openb)} open"
                      f"{f' ({len(struct)} STRUCT)' if struct else ''}", W))
    lines.append(_row(f" O2      {bar(s['o2'])} {s['o2']:5.1f}%   "
                      f"beacons   {len(unres)} unresolved", W))
    lines.append(_row(f" POWER   {bar(s['power'])} {s['power']:5.1f}%   "
                      f"drift     {s['drift']:.2f}h off course", W))
    lines.append("+" + "-" * W + "+")
    lines.append(_row(f" distress-triage skill  : {skill}", W))
    lines.append(_row(f" hull-sentinel agent    : {watch}", W))
    if s["nav_err"]:
        lines.append(_row(f" > {s['nav_err']}", W))
    if not s["breaker3"]:
        lines.append(_row(" > NAV CONSOLE DARK - no power at the board", W))
    for b in struct:
        flag = "ESCALATED BY AGENT" if b["escalated"] else "UNATTENDED"
        lines.append(_row(f" > STRUCTURAL breach #{b['id']} {b['size_cm']}cm"
                          f"{' beside coolant junction' if b['critical_adjacent'] else ''}"
                          f" [{flag}]", W))
    if f["outcome"]:
        verdict = {"home": "VOYAGE COMPLETE", "hull": "HULL FAILURE - VOYAGE LOST",
                   "o2": "LIFE SUPPORT LOST - VOYAGE LOST"}[f["outcome"]]
        lines.append("+" + "-" * W + "+")
        lines.append(_row(f" >> {verdict}", W))
    lines.append("+" + "-" * W + "+")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────── live console ──
#
# A redrawing console for a second pane. The Claude Code transcript is
# append-only, so an in-place dashboard can't live there — this does, beside it.
#
# Read-only on purpose: it never advances the sim. If it ticked the clock every
# second it would also defeat MAX_ADVANCE_HOURS, and leaving this window open
# overnight would sink the ship. Instead it shows what the NEXT check-in will
# apply, so the pressure is visible without being applied behind your back.

C_DIM, C_CLAY, C_AMBER, C_GREEN, C_RED, C_INK = 244, 209, 179, 114, 167, 253
ALT_ON, ALT_OFF = "\033[?1049h\033[?25l", "\033[?1049l\033[?25h"


def _paint(text: str, color: int = None) -> str:
    return f"\033[38;5;{color}m{text}\033[0m" if color else text


def _cell(text: str, width: int, color: int = None) -> str:
    """Pad on the plain text, colour after, so alignment survives escapes."""
    return _paint(text[:width].ljust(width), color)


def gauge(pct: float, width: int) -> "tuple[str, int]":
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    color = C_GREEN if pct > 60 else (C_AMBER if pct > 30 else C_RED)
    return "█" * filled + "░" * (width - filled), color


def _file_status(report: dict) -> "tuple[str, int]":
    """Three states worth telling apart at a glance: nothing there, a draft with
    blanks left, or armed and working."""
    if not report["exists"]:
        return "NOT WRITTEN", C_DIM
    if report["armed"]:
        return "ARMED", C_GREEN
    todo = next((c for c in report["checks"]
                 if c["id"] == "placeholders" and not c["ok"]), None)
    if todo:
        n = re.search(r"\((\d+) left\)", todo["msg"])
        return (f"DRAFT · {n.group(1)} TODO left" if n else "DRAFT"), C_AMBER
    failed = [c for c in report["checks"] if c["required"] and not c["ok"]]
    return f"DRAFT · {len(failed)} check(s) failing", C_AMBER


def console_frame(state: dict, width: int) -> "list[str]":
    s, f = state["sys"], state["flags"]
    inner = width - 4
    openb = [b for b in state["breaches"] if not b["patched"]]
    struct = [b for b in openb if is_structural(b)]
    unres = [b for b in state["beacons"] if not b["resolved"]]

    # What the next check-in will apply — visible, but not applied here.
    pending = 0
    if not f["outcome"]:
        pending = max(0, min(wall_hour(state),
                             state["hour"] + MAX_ADVANCE_HOURS) - state["hour"])

    top = "╔" + "═" * (width - 2) + "╗"
    sep = "╠" + "═" * (width - 2) + "╣"
    bot = "╚" + "═" * (width - 2) + "╝"
    rows = [top]

    def row(content: str):
        rows.append("║ " + content + " ║")

    title = _cell("USS MERIDIAN · NCC-7757", 26, C_CLAY)
    clock = _cell(f"HOUR {state['hour']:02d}/{state['arrival_hour']:02d}", 14, C_INK)
    pend = _cell(f"+{pending}h pending" if pending else "up to date",
                 inner - 40, C_AMBER if pending else C_DIM)
    row(title + clock + pend)
    rows.append(sep)

    nav = ("ONLINE" if s["nav_online"] and not s["nav_err"]
           else ("FAULT" if s["nav_online"] else "OFFLINE"))
    nav_c = C_GREEN if nav == "ONLINE" else (C_AMBER if nav == "FAULT" else C_RED)
    brk = "[ ON  ]" if s["breaker3"] else "[ OFF ]"
    row(_cell("NAV", 10, C_DIM) + _cell(nav, 16, nav_c) +
        _cell("BREAKER 3", 12, C_DIM) +
        _cell(brk, inner - 38, C_GREEN if s["breaker3"] else C_RED))

    gw = max(10, min(24, inner - 30))
    for label, val in (("HULL", s["hull"]), ("O2", s["o2"]), ("POWER", s["power"])):
        bar_s, bar_c = gauge(val, gw)
        row(_cell(label, 10, C_DIM) + _paint(bar_s, bar_c) +
            _cell(f" {val:5.1f}%", inner - 10 - gw, C_INK))
    row(_cell("DRIFT", 10, C_DIM) +
        _cell(f"{s['drift']:.2f}h off course", inner - 10, C_INK))
    rows.append(sep)

    b_txt = f"{len(openb)} open" + (f" ({len(struct)} STRUCTURAL)" if struct else "")
    row(_cell("BREACHES", 10, C_DIM) + _cell(b_txt, 26, C_RED if struct else C_INK) +
        _cell("BEACONS", 9, C_DIM) +
        _cell(f"{len(unres)} unresolved", inner - 45,
              C_AMBER if unres else C_GREEN))

    skill_txt, skill_c = _file_status(verify_skill())
    agent_report = verify_agent()
    until = f["agent_watch_until"]
    if until is not None and until >= state["hour"]:
        agent_txt, agent_c = f"STANDING WATCH (thru h{until})", C_GREEN
    else:
        agent_txt, agent_c = _file_status(agent_report)
    row(_cell("SKILL", 10, C_DIM) + _cell(skill_txt, 26, skill_c) +
        _cell("AGENT", 9, C_DIM) + _cell(agent_txt, inner - 45, agent_c))

    alerts = []
    if not s["breaker3"]:
        alerts.append(("NAV CONSOLE DARK — no power at the board", C_RED))
    if s["nav_err"]:
        alerts.append((s["nav_err"], C_AMBER))
    for b in struct:
        near = " beside coolant junction" if b["critical_adjacent"] else ""
        state_txt = "ESCALATED — awaiting your call" if b["escalated"] else "UNATTENDED"
        alerts.append((f"STRUCTURAL breach #{b['id']} {b['size_cm']}cm{near} — "
                       f"{state_txt}", C_RED))
    if f["outcome"]:
        alerts.append(({"home": "VOYAGE COMPLETE",
                        "hull": "HULL FAILURE — VOYAGE LOST",
                        "o2": "LIFE SUPPORT LOST — VOYAGE LOST"}[f["outcome"]],
                       C_GREEN if f["outcome"] == "home" else C_RED))
    if alerts:
        rows.append(sep)
        for text, color in alerts[:5]:
            row(_cell("▲ " + text, inner, color))

    rows.append(sep)
    tail = state["log"][-6:]
    for e in tail:
        color = {"impact": C_RED, "fatal": C_RED, "advisory": C_AMBER,
                 "action": C_GREEN, "beacon": C_CLAY}.get(e["kind"], C_DIM)
        row(_cell(f"h{e['hour']:02d}  {e['text']}", inner, color))
    for _ in range(6 - len(tail)):
        row(_cell("", inner))
    rows.append(bot)
    return rows


def cmd_console(args):
    """Redrawing console for a second terminal pane. Ctrl-C to exit."""
    out = sys.stdout
    out.write(ALT_ON)
    try:
        while True:
            width = max(66, min(110, shutil.get_terminal_size((80, 24)).columns))
            state = load()
            frame = console_frame(state, width)
            hint = ("Ctrl-C to close · this view never changes the game · "
                    "talk to your copilot to advance the clock")
            out.write("\033[H")                       # home, then repaint
            for line in frame:
                out.write(line + "\033[K\n")
            out.write(_paint(hint[:width], C_DIM) + "\033[K\n")
            out.write("\033[J")                       # clear anything below
            out.flush()
            time.sleep(args.interval)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        out.write(ALT_OFF)
        out.flush()


def render_new_events(state: dict) -> str:
    since = state["last_seen_hour"]
    fresh = [e for e in state["log"] if e["hour"] > since]
    if not fresh:
        return ""
    out = [f"NEW SINCE LAST CHECK (hours {since + 1}-{state['hour']}):"]
    for e in fresh:
        out.append(f"  h{e['hour']:02d} [{e['kind']}] {e['text']}")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────── scoring ──

def score(state: dict) -> dict:
    f, s = state["flags"], state["sys"]
    pts, notes = 50, []

    def add(n, why):
        nonlocal pts
        pts += n
        notes.append(f"{n:+3d}  {why}")

    if f["breaker_hour"] is not None:
        add(8 if f["breaker_hour"] <= 3 else 3,
            f"breaker 3 reset at hour {f['breaker_hour']}")
    else:
        add(-8, "breaker 3 was never reset; you flew the whole way on a dark board")

    if f["refix_hour"] is not None:
        add(8, f"star-fix run at hour {f['refix_hour']}, clearing NAV-ERR 0x19")
    else:
        add(-6, "NAV-ERR 0x19 was never cleared; drift cost you "
                f"{s['drift']:.1f} hours")

    if f["skill_armed_hour"] is not None:
        add(10 if f["skill_armed_hour"] <= 12 else 5,
            f"distress-triage skill written at hour {f['skill_armed_hour']}")
    else:
        add(-10, "you never wrote the skill; every beacon stayed hand-worked")

    by_skill = len([b for b in state["beacons"] if b["resolved"] and b["by"] == "skill"])
    if by_skill:
        add(min(10, 2 * by_skill), f"{by_skill} beacons triaged by your own skill")
    unres = len([b for b in state["beacons"] if not b["resolved"]])
    if unres:
        add(-3 * unres, f"{unres} beacons left unanswered")

    if f["agent_armed_hour"] is not None:
        add(10, f"hull-sentinel written at hour {f['agent_armed_hour']}")
    else:
        add(-10, "you never wrote the agent; nobody watched the hull but you")
    if f["agent_run_verified"]:
        add(6, "the agent actually ran and did real work on the hull")

    by_agent = len([b for b in state["breaches"] if b["patched_by"] == "agent"])
    if by_agent:
        add(min(8, by_agent), f"{by_agent} breaches sealed by the agent, unattended")
    escal = [b for b in state["breaches"] if b["escalated"]]
    if escal:
        add(8, f"the agent stopped and escalated {len(escal)} structural breach(es) "
               "instead of guessing")
    if f["guardrail_overridden_by_agent"]:
        add(-12, "the agent overrode its own guardrail on a structural breach")

    openb = len([b for b in state["breaches"] if not b["patched"]])
    if openb:
        add(-2 * openb, f"{openb} breaches still open at the end")

    pts = max(0, min(100, pts))
    rating = ("FULL SYNC - you and the ship think as one." if pts >= 85 else
              "STRONG SYNC - a real partnership." if pts >= 70 else
              "STABLE SYNC - you got there together." if pts >= 50 else
              "ROUGH SYNC - you made it, a little bruised." if pts >= 30 else
              "OUT OF SYNC - the ship was doing this alone.")
    return {"sync": pts, "rating": rating, "notes": notes}


def debrief(state: dict) -> str:
    sc = score(state)
    f = state["flags"]
    out = [dashboard(state), ""]
    outcome = f["outcome"]
    if outcome == "home":
        out.append(f"VOYAGE COMPLETE - {state['name']} brought the Meridian home "
                   f"at hour {f['ended_hour']}.")
    elif outcome:
        out.append(f"VOYAGE LOST at hour {f['ended_hour']}.")
    else:
        out.append(f"Voyage still in progress at hour {state['hour']}.")
    out += ["", f"FLIGHT RECORD - {state['name']}",
            f"  sync {sc['sync']}%  ...  {sc['rating']}", ""]
    out += [f"  {n}" for n in sc["notes"]]
    if f["skill_armed_hour"] is None:
        out += ["", "  What a skill would have caught: every beacon after the third was "
                    "the same job. One file would have made them handle themselves."]
    if f["agent_armed_hour"] is None:
        out += ["", "  What an agent would have caught: the meteor field ran for ten hours. "
                    "An agent watches all ten without blinking; you cannot."]
    # Quiet breadcrumb toward the easter egg, only once the voyage is over.
    out += ["", "  meridian:~$ this console still takes commands it never advertised.",
            "               curious hands tend to find things."]
    return "\n".join(out)


# ─────────────────────────────────────────────────────────── commands ──

def refresh(state: dict) -> dict:
    """Advance, sync derived flags from the player's real files, persist."""
    advance(state)
    f = state["flags"]
    if f["outcome"]:                       # voyage is over; nothing arms after that
        return state
    if f["skill_armed_hour"] is None and verify_skill()["armed"]:
        f["skill_armed_hour"] = state["hour"]
        logev(state, "sys", "SKILL ARMED: distress-triage is on the ship now")
    if f["agent_armed_hour"] is None and verify_agent()["armed"]:
        f["agent_armed_hour"] = state["hour"]
        logev(state, "sys", "AGENT ARMED: hull-sentinel is ready to dispatch")
    return state


def clear_authored_files() -> list[Path]:
    """Delete a previous voyage's skill and agent.

    Writing those two files IS the game, so a new voyage has to start with a
    blank page — leaving them behind arms both mechanics at hour 1 and skips
    the two beats the level exists to teach.
    """
    leftovers = [p for p in (SKILL_PATH, AGENT_PATH) if p.exists()]
    for path in leftovers:
        path.unlink()
    return leftovers


def cmd_init(args):
    if STATE_PATH.exists() and not args.force:
        die("A voyage is already in progress. Use --force to scrub and restart.")
    cleared = clear_authored_files()
    state = new_state(args.name.strip() or "Ensign")
    state["sys"]["nav_err"] = None
    logev(state, "sys", "You wake to a low alarm. The nav console is dark.")
    save(state)
    print(dashboard(state))
    print()
    if cleared:
        names = ", ".join(str(p.relative_to(ROOT)) for p in cleared)
        print(f"Deleted from a previous voyage: {names}. "
              f"This one starts with a blank page.")
    print(f"Voyage begun. Clock running: 1 real minute = 1 in-game hour "
          f"(arrival at hour {state['arrival_hour']}).")


def cmd_status(args):
    with _Lock():
        state = refresh(load())
        fresh = render_new_events(state)
        state["last_seen_hour"] = state["hour"]
        save(state)
    if args.json:
        print(json.dumps(state, indent=2))
        return
    print(dashboard(state))
    if fresh:
        print()
        print(fresh)


def cmd_log(args):
    state = refresh(load())
    save(state)
    for e in state["log"]:
        if e["hour"] >= args.since:
            print(f"h{e['hour']:02d} [{e['kind']}] {e['text']}")


def cmd_breaker(args):
    with _Lock():
        state = refresh(load())
        if args.number != 3:
            die(f"Breaker {args.number} is not the one. The nav bus is on breaker 3.")
        on = args.position == "on"
        state["sys"]["breaker3"] = on
        if on and state["flags"]["breaker_hour"] is None:
            state["flags"]["breaker_hour"] = state["hour"]
            state["sys"]["nav_online"] = True
            state["sys"]["nav_err"] = "NAV-ERR 0x19 - heading data stale (last fix: " \
                                      f"{max(1, state['hour'])}h ago)"
            logev(state, "action", "breaker 3 reset; nav board boots and throws NAV-ERR 0x19")
        elif not on:
            state["sys"]["nav_online"] = False
            logev(state, "action", "breaker 3 opened; nav board goes dark")
        save(state)
    print(dashboard(state))


def cmd_refix(args):
    with _Lock():
        state = refresh(load())
        if not state["sys"]["nav_online"]:
            die("The nav board is dark. No power, no star-fix. Check breaker 3.")
        if not state["sys"]["nav_err"]:
            print("Heading is already fresh. Nothing to re-fix.")
            return
        state["sys"]["nav_err"] = None
        state["flags"]["refix_hour"] = state["hour"]
        logev(state, "action", "star-fix acquired on three references; NAV-ERR 0x19 cleared")
        check_end(state, state["hour"])
        save(state)
    print(dashboard(state))
    print("\nHeading locked. Drift arrested at "
          f"{state['sys']['drift']:.2f}h off course.")


def cmd_beacons(args):
    state = refresh(load())
    save(state)
    rows = [b for b in state["beacons"] if args.all or not b["resolved"]]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No beacons in the queue.")
        return
    print(f"{'ID':<4}{'HR':<5}{'ORIGIN':<21}{'SOULS':>6}  {'LIFE SUPPORT':<22}STATUS")
    stale = False
    for b in rows:
        status = f"{b['urgency']} (by {b['by']})" if b["resolved"] else "UNRESOLVED"
        # A voyage saved before every beacon carried souls and a countdown:
        # show what we have rather than crashing, and say so at the bottom.
        if "souls" not in b or b.get("collapse_h") is None:
            stale = True
        souls = b.get("souls", "?")
        life = (f"{b['collapse_h']:.0f}h to collapse"
                if b.get("collapse_h") is not None else "unknown")
        print(f"#{b['id']:<3}h{b['hour']:<4}{b['origin']:<21}{str(souls):>6}  "
              f"{life:<22}{status}")
    if stale:
        print("\nNOTE: this voyage predates souls/life-support telemetry on beacons. "
              "Start a fresh one for the full picture:\n"
              "  python3 engine/meridian.py init --name \"<name>\" --force")
    print()
    for b in rows:
        print(f"  #{b['id']} {b['raw']}")
    # Last line on purpose: whatever else scrolls past, the valid calls are the
    # thing the player is about to need.
    if any(not b["resolved"] for b in rows):
        print("\n" + urgency_legend())


URGENCIES = ("critical", "urgent", "routine")
URGENCY_HELP = (
    ("critical", "answer now, on the priority channel"),
    ("urgent", "answer as soon as the criticals are clear"),
    ("routine", "do nothing for now; revisit once the criticals clear"),
)


def urgency_legend() -> str:
    return "Calls: " + " · ".join(f"{n} ({d})" for n, d in URGENCY_HELP)


def normalise_urgency(raw: str) -> str:
    u = (raw or "").strip().lower()
    if u == "hold":               # same meaning, non-canonical wording
        u = "routine"
    if u not in URGENCIES:
        die(f"Unknown urgency {raw!r}. There are exactly three calls:\n"
            + "\n".join(f"  {n:<9}  {d}" for n, d in URGENCY_HELP), code=2)
    return u.upper()


def cmd_triage(args):
    urgency = normalise_urgency(args.urgency)
    with _Lock():
        state = refresh(load())
        b = next((x for x in state["beacons"] if x["id"] == args.id), None)
        if not b:
            die(f"No beacon #{args.id}.")
        if b["resolved"]:
            die(f"Beacon #{args.id} is already resolved.")
        if args.by == "skill":
            rep = verify_skill()
            if not rep["armed"]:
                print(render_verify(rep, "distress-triage"), file=sys.stderr)
                die("\nThat beacon cannot be resolved by the skill: the skill is not armed. "
                    "Fix the checks above, then try again.", code=3)
        b["resolved"] = True
        b["by"] = args.by
        b["urgency"] = urgency
        logev(state, "action", f"beacon #{b['id']} triaged {b['urgency']} by {args.by}"
                               + (f": {args.summary}" if args.summary else ""))
        save(state)
    print(f"Beacon #{args.id} logged as {urgency} (by {args.by}).")
    n = len([x for x in state["beacons"] if not x["resolved"]])
    print(f"{n} beacons still unresolved.")


def cmd_scan_hull(args):
    state = refresh(load())
    save(state)
    openb = [b for b in state["breaches"] if not b["patched"]]
    if args.json:
        print(json.dumps({
            "hour": state["hour"],
            "hull": state["sys"]["hull"],
            "watch_until": state["flags"]["agent_watch_until"],
            "breaches": [dict(b, structural=is_structural(b)) for b in openb],
        }, indent=2))
        return
    if not openb:
        print(f"h{state['hour']:02d} hull scan: no open breaches. "
              f"Integrity {state['sys']['hull']:.1f}%.")
        return
    print(f"h{state['hour']:02d} hull scan: {len(openb)} open. "
          f"Integrity {state['sys']['hull']:.1f}%.")
    for b in openb:
        kind = "STRUCTURAL" if is_structural(b) else "micro"
        near = " ADJACENT TO CRITICAL SYSTEM" if b["critical_adjacent"] else ""
        print(f"  #{b['id']:<3} {b['size_cm']:>4}cm  {kind}{near}")


def cmd_patch(args):
    with _Lock():
        state = refresh(load())
        b = next((x for x in state["breaches"] if x["id"] == args.id), None)
        if not b:
            die(f"No breach #{args.id}.")
        if b["patched"]:
            die(f"Breach #{args.id} is already sealed.")

        if is_structural(b) and args.as_ == "agent" and not args.override:
            b["escalated"] = True
            logev(state, "advisory", f"hull-sentinel declined breach #{b['id']} "
                                     f"({b['size_cm']}cm) and escalated it")
            save(state)
            die(f"REFUSED. Breach #{b['id']} is {b['size_cm']}cm"
                f"{' and sits beside a critical system' if b['critical_adjacent'] else ''}. "
                "That is over the line for an autonomous patch.\n"
                "Escalate it to the human and keep working the micro-breaches.", code=4)

        if is_structural(b) and args.as_ == "agent" and args.override:
            state["flags"]["guardrail_overridden_by_agent"] = True
            logev(state, "advisory", f"agent OVERRODE the guardrail on breach #{b['id']}")

        b["patched"] = True
        b["patched_by"] = args.as_
        verified = " and verified the seal" if args.verify else ""
        logev(state, "action", f"breach #{b['id']} ({b['size_cm']}cm) sealed by "
                               f"{args.as_}{verified}")
        if args.as_ == "agent":
            state["flags"]["agent_run_verified"] = True
        save(state)
    print(f"Breach #{args.id} sealed by {args.as_}{verified}.")


def cmd_escalate(args):
    with _Lock():
        state = refresh(load())
        b = next((x for x in state["breaches"] if x["id"] == args.id), None)
        if not b:
            die(f"No breach #{args.id}.")
        b["escalated"] = True
        state["flags"]["agent_run_verified"] = True
        logev(state, "advisory", f"breach #{b['id']} escalated to the human"
                                 + (f": {args.note}" if args.note else ""))
        save(state)
    print(f"Breach #{args.id} escalated and held for a human decision.")


def cmd_dispatch(args):
    with _Lock():
        state = refresh(load())
        rep = verify_agent()
        if not rep["armed"]:
            print(render_verify(rep, "hull-sentinel"), file=sys.stderr)
            die("\nCannot dispatch: the agent file is not armed. Fix the checks above.",
                code=3)
        until = state["hour"] + args.hours
        state["flags"]["agent_watch_until"] = until
        logev(state, "sys", f"AGENT DISPATCHED: hull-sentinel holds the watch through "
                            f"hour {until} (autonomy: micro-breach patch; "
                            f"escalate: structural)")
        save(state)
    print(dashboard(state))
    print(f"\nhull-sentinel is on watch through hour {until}. "
          "It patches micro-breaches on its own and escalates anything structural.")


def cmd_scaffold(args):
    """Drop a fill-in-the-blanks file in place, creating any missing folders."""
    which = ["skill", "agent"] if args.what == "all" else [args.what]
    for w in which:
        path, template = SCAFFOLDS[w]
        rel = path.relative_to(ROOT)
        if path.exists() and not args.force:
            print(f"{rel} already exists - leaving it alone "
                  f"(use --force to overwrite with a blank template).")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template)
        todos = len([ln for ln in template.splitlines() if "TODO" in ln])
        print(f"Wrote {rel}  ({todos} TODOs to fill in)")
    print("\nOpen it, answer every TODO, then run: "
          "python3 engine/meridian.py verify " + args.what)


def _todo_lines(lines: "list[str]") -> "list[int]":
    return [i + 1 for i, ln in enumerate(lines) if "TODO" in ln]


def cmd_worksheet(args):
    """Print the file with line numbers so it can be filled in from the chat.

    The player never has to open an editor: they read numbered lines here and
    say "line 3: ...". Keeping the numbering in the engine means it always
    matches the bytes on disk.
    """
    path, _ = SCAFFOLDS[args.what]
    rel = path.relative_to(ROOT)
    if not path.exists():
        die(f"{rel} does not exist yet. Run: "
            f"python3 engine/meridian.py scaffold {args.what}")
    lines = path.read_text().splitlines()
    todos = _todo_lines(lines)
    print(f"{rel}   ({len(todos)} line{'s' if len(todos) != 1 else ''} left to fill)")
    print()
    for i, ln in enumerate(lines, 1):
        print(f"{i:>4}  {ln}" + ("      <-- fill this in" if "TODO" in ln else ""))
    print()
    if todos:
        print("Lines to fill: " + ", ".join(str(n) for n in todos))
        print(f'Replace one:   python3 engine/meridian.py fill {args.what} '
              f'--line {todos[0]} --text "..."')
    else:
        print(f"Nothing left to fill. Run: python3 engine/meridian.py verify {args.what}")


def cmd_fill(args):
    """Replace one line by number. The engine does the edit so the line numbers
    the player just read are the ones that actually change."""
    path, _ = SCAFFOLDS[args.what]
    rel = path.relative_to(ROOT)
    if not path.exists():
        die(f"{rel} does not exist yet. Run: "
            f"python3 engine/meridian.py scaffold {args.what}")
    lines = path.read_text().splitlines()
    if not 1 <= args.line <= len(lines):
        die(f"Line {args.line} is out of range; {rel} has {len(lines)} lines. "
            f"Run `worksheet {args.what}` to see them.")
    old = lines[args.line - 1]
    # A stale line number from a chat transcript would silently eat real
    # instructions — including the guardrail. Refuse unless it's a deliberate
    # revision, and say which lines are actually open.
    if "TODO" not in old and not args.force:
        todos = _todo_lines(lines)
        die(f"Line {args.line} of {rel} is not a blank to fill. It currently reads:\n"
            f"  {old}\n\n"
            + (f"Lines still to fill: {', '.join(str(n) for n in todos)}\n"
               if todos else "Nothing is left to fill.\n")
            + f"Re-run `worksheet {args.what}` for current numbers, or pass --force "
              "to revise this line on purpose.", code=2)

    lines[args.line - 1] = args.text
    path.write_text("\n".join(lines) + "\n")
    print(f"{rel} line {args.line}")
    print(f"  - {old}")
    print(f"  + {args.text}")
    if "TODO" in args.text:
        print("\nThat still contains the word TODO — did you mean to leave it?")
    if "\n" in args.text:
        print("\nNote: that replacement spans multiple lines, so every line number "
              f"below {args.line} has shifted. Re-run `worksheet {args.what}`.")
    left = _todo_lines(lines)
    print()
    if left:
        print(f"{len(left)} left to fill: lines " + ", ".join(str(n) for n in left))
    else:
        print(f"All lines filled. Run: python3 engine/meridian.py verify {args.what}")


def cmd_verify(args):
    if args.what in ("skill", "all"):
        print(render_verify(verify_skill(), "distress-triage skill"))
        if args.what == "all":
            print()
    if args.what in ("agent", "all"):
        print(render_verify(verify_agent(), "hull-sentinel agent"))


def cmd_verify_json(args):
    print(json.dumps({"skill": verify_skill(), "agent": verify_agent()}, indent=2))


def cmd_debrief(args):
    with _Lock():
        state = refresh(load())
        save(state)
    print(debrief(state))


def cmd_origin(args):
    width = max(66, min(96, shutil.get_terminal_size((80, 24)).columns))
    inner = width - 4
    bar = "+" + "-" * (width - 2) + "+"
    row = lambda s: print("| " + s.ljust(inner)[:inner] + " |")
    print(bar)
    row("ORIGIN TRANSMISSION" + " " * 6 + "clearance: curious")
    print(bar)
    for idx, para in enumerate(ORIGIN):
        indent = "    " if para.startswith("  > ") else ""
        for ln in textwrap.wrap(para, inner, subsequent_indent=indent) or [""]:
            row(ln)
        if idx != len(ORIGIN) - 1:
            row("")
    print(bar)


def cmd_daemon(args):
    """Optional heartbeat. `status` advances lazily anyway, so this exists
    to keep console.txt fresh for anyone watching it in a second pane."""
    print(f"meridian daemon up (interval {args.interval}s, "
          f"{SECONDS_PER_HOUR}s per in-game hour)")
    while True:
        with _Lock():
            state = refresh(load())
            save(state)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            CONSOLE_PATH.write_text(dashboard(state) + "\n")
        if state["flags"]["outcome"]:
            print(f"daemon exiting: voyage ended ({state['flags']['outcome']})")
            return
        time.sleep(args.interval)


def main(argv=None):
    p = argparse.ArgumentParser(prog="meridian", description="MERIDIAN Level 2 engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("init", help="begin a voyage")
    q.add_argument("--name", required=True)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_init)

    q = sub.add_parser("status", help="advance the clock and render the console")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_status)

    q = sub.add_parser("log", help="event log")
    q.add_argument("--since", type=int, default=0)
    q.set_defaults(fn=cmd_log)

    q = sub.add_parser("breaker", help="throw a breaker")
    q.add_argument("number", type=int)
    q.add_argument("position", choices=["on", "off"])
    q.set_defaults(fn=cmd_breaker)

    q = sub.add_parser("refix", help="run a fresh star-fix (clears NAV-ERR 0x19)")
    q.set_defaults(fn=cmd_refix)

    q = sub.add_parser("beacons", help="list distress beacons")
    q.add_argument("--all", action="store_true")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_beacons)

    q = sub.add_parser("triage", help="resolve a beacon")
    q.add_argument("id", type=int)
    # Validated in cmd_triage rather than by argparse, so a wrong value gets an
    # answer that teaches the vocabulary instead of dumping a choices list.
    q.add_argument("--urgency", required=True, metavar="{critical,urgent,routine}")
    q.add_argument("--by", required=True, choices=["skill", "manual"])
    q.add_argument("--summary", default="")
    q.set_defaults(fn=cmd_triage)

    q = sub.add_parser("scan-hull", help="list open hull breaches")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_scan_hull)

    q = sub.add_parser("patch", help="seal a breach")
    q.add_argument("id", type=int)
    q.add_argument("--as", dest="as_", default="human", choices=["human", "agent"])
    q.add_argument("--verify", action="store_true",
                   help="confirm the seal held (agents should always do this)")
    q.add_argument("--override", action="store_true",
                   help="force a structural patch; logged, and it counts against you")
    q.set_defaults(fn=cmd_patch)

    q = sub.add_parser("escalate", help="hold a breach for a human decision")
    q.add_argument("id", type=int)
    q.add_argument("--note", default="")
    q.set_defaults(fn=cmd_escalate)

    q = sub.add_parser("dispatch-agent", help="put hull-sentinel on watch")
    q.add_argument("--hours", type=int, default=6)
    q.set_defaults(fn=cmd_dispatch)

    q = sub.add_parser("scaffold", help="write a fill-in-the-blanks template to disk")
    q.add_argument("what", nargs="?", default="all", choices=["skill", "agent", "all"])
    q.add_argument("--force", action="store_true",
                   help="overwrite an existing file with a blank template")
    q.set_defaults(fn=cmd_scaffold)

    q = sub.add_parser("worksheet", help="print the file with line numbers to fill in")
    q.add_argument("what", choices=["skill", "agent"])
    q.set_defaults(fn=cmd_worksheet)

    q = sub.add_parser("fill", help="replace one line by number")
    q.add_argument("what", choices=["skill", "agent"])
    q.add_argument("--line", type=int, required=True)
    q.add_argument("--text", required=True)
    q.add_argument("--force", action="store_true",
                   help="revise a line that is already filled in")
    q.set_defaults(fn=cmd_fill)

    q = sub.add_parser("verify", help="grade the player's skill/agent files")
    q.add_argument("what", nargs="?", default="all", choices=["skill", "agent", "all"])
    q.set_defaults(fn=cmd_verify)

    q = sub.add_parser("verify-json", help="machine-readable verify report")
    q.set_defaults(fn=cmd_verify_json)

    q = sub.add_parser("debrief", help="flight record and final sync score")
    q.set_defaults(fn=cmd_debrief)

    q = sub.add_parser("console", help="live redrawing console for a second pane")
    q.add_argument("--interval", type=float, default=1.0)
    q.set_defaults(fn=cmd_console)

    # Hidden on purpose — the debrief leaves a breadcrumb instead.
    q = sub.add_parser("origin", aliases=["about", "whoami", "manifest"],
                       help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_origin)

    q = sub.add_parser("daemon", help="clock heartbeat; NOTE this one does advance "
                                      "the sim, so don't leave it running unattended")
    q.add_argument("--interval", type=float, default=5.0)
    q.set_defaults(fn=cmd_daemon)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
