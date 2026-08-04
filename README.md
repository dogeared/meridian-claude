# MERIDIAN

Two levels of the same idea: the fastest way to understand Claude is to use Claude at the
exact moment it clicks.

Your ship is failing and the crew is gone. All that's left is you and the onboard AI.

---

## Level 1 — A Claude Copilot Adventure

**`meridian-a-claude-copilot-adventure.html`** — open it in any browser. No build, no
dependencies, nothing to install.

A short choose-your-own-adventure (~8 minutes) that walks the three rungs of working with
Claude: writing a good prompt, capturing repeated work as a **Skill**, and delegating a
long unattended job to an **Agent**. Every code sample in it is real and
copy-pasteable. There's an easter egg in the console at the bottom.

Level 1 shows you the shape of the thing.

## Level 2 — The Long Watch

**Level 2 makes you build it for real, inside Claude Code.**

```sh
git clone https://github.com/<you>/meridian && cd meridian
claude
> /meridian
```

Same ship, same copilot, but now it's a real-time game played through an ASCII console,
and the two files at the center of it are files *you* write, on your actual disk, that
actually run.

```
+------------------------------------------------------------------+
|== USS MERIDIAN . NCC-7757 ========================= HOUR 16/26 ==|
+------------------------------------------------------------------+
| NAV     ONLINE            BREAKER 3   [ ON  ]                    |
| HULL    ########..  79.0%   breaches  4 open (1 STRUCT)          |
| O2      ########## 100.0%   beacons   2 unresolved               |
| POWER   ######....  60.0%   drift     2.00h off course           |
+------------------------------------------------------------------+
| distress-triage skill  : armed                                   |
| hull-sentinel agent    : STANDING WATCH                          |
| > STRUCTURAL breach #4 3.1cm beside coolant junction [UNATTENDED]|
+------------------------------------------------------------------+
```

### How it plays

**One real minute is one in-game hour.** The clock runs while you think. Ignore the ship
and the ship notices.

1. **The console is dark.** Ask your copilot why. It'll tell you about breaker 3. Flip it.
2. **`NAV-ERR 0x19`.** Hand the copilot the exact error, not a paraphrase, and watch how
   much faster that goes. Every hour you drift pushes your arrival further out.
3. **Distress beacons start arriving**, one every three hours. Work a few by hand. Get
   bored. Then write `.claude/skills/distress-triage/SKILL.md` — and the engine will
   refuse to let the skill resolve a beacon until the file is actually good. The
   description is the part that matters: it's how Claude knows *when* to reach for it.
4. **A micro-meteor field**, ten hours of impacts, while you're needed two decks down.
   You can't be in two places. Write `.claude/agents/hull-sentinel.md`, then watch it get
   dispatched as a genuine subagent that scans the hull, patches micro-breaches, verifies
   its own seals, and refuses to touch anything structural.
5. **Hour 22-ish, something over the line.** The agent stops and asks. That moment is the
   whole point of the guardrail you wrote.

You can lose. Hull or oxygen at zero ends the voyage; drifting the entire way gets you
there around hour 22. The debrief tells you what a skill or an agent would have caught.

### What makes it real

The guardrail isn't roleplay. It's enforced in the engine:

```sh
$ python3 engine/meridian.py patch 4 --as agent --verify
REFUSED. Breach #4 is 3.1cm and sits beside a critical system. That is over
the line for an autonomous patch.
Escalate it to the human and keep working the micro-breaches.
```

Your subagent hits that wall with a real non-zero exit code and has to escalate. If your
agent file forgot the guardrail, you find out the honest way.

Same for the skill — `triage --by skill` fails while your `SKILL.md` is unarmed, and
`verify` tells you exactly which check missed:

```sh
$ python3 engine/meridian.py verify skill
  [PASS] name is lowercase-kebab (got 'distress-triage')
  [FAIL] description names a trigger condition
         -> Add a clause like "Use whenever a distress beacon is received."
```

### The engine

`engine/meridian.py` — Python 3.9+, stdlib only, no dependencies. It owns the clock, the
systems, the event schedule, and the scoring; Claude reads it and speaks for it. Every
in-game hour is generated from a seeded RNG, so the log you read is the log that
happened.

Useful outside the game loop:

| Command | What it does |
| --- | --- |
| `status` | Advance the clock, print the console, list what happened while you were away |
| `log --since 12` | Raw event history |
| `verify all` | Grade both of your files, check by check |
| `scan-hull --json` | What a dispatched agent reads each cycle |
| `debrief` | Flight record and final sync score |
| `daemon` | Optional heartbeat; writes `.meridian/console.txt` for a live second pane |

Set `MERIDIAN_SECONDS_PER_HOUR=1` to compress a 24-minute voyage into 24 seconds — handy
for testing, ruinous for drama.

State lives in `.meridian/state.json`. To start over:

```sh
python3 engine/meridian.py init --name "<name>" --force
```

### The two files you'll write

Gitignored on purpose, so the next person starts with a blank page:

```
.claude/skills/distress-triage/SKILL.md   ← Level 2, beat 3
.claude/agents/hull-sentinel.md           ← Level 2, beat 4
```

They're ordinary Claude Code files. When you're done playing, they still work — and so
does everything you learned making them, in whatever repo you open tomorrow.
