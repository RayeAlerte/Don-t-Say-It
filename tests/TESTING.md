# Testing — Don't Say It!

## Structure

```
project_root/
├── Models/
│   ├── game_state.py
│   └── payloads.py
├── logic/
│   └── round_manager.py
├── static/
├── tests/
│   ├── test_scoring.py
│   ├── test_round_manager_rulesets.py
│   └── test_simulation_stats.py
├── main.py
├── index.html
└── requirements.txt
```

All test files live in `tests/`. Each file uses `sys.path.insert` to add the
project root so imports work without installing the package. No test requires
a running server or a real WebSocket connection — a `DummyWS` stub is used
wherever `Player` needs a `ws` object.

---

## Running the tests

### Run all tests (recommended)
```bash
pytest tests/ -v
```

### Run a single file
```bash
pytest tests/test_scoring.py -v
pytest tests/test_round_manager_rulesets.py -v
pytest tests/test_simulation_stats.py -v
```

### Run as plain scripts (no pytest required)
```bash
python tests/test_scoring.py
python tests/test_round_manager_rulesets.py
python tests/test_simulation_stats.py
```

---

## What each file covers

### `test_scoring.py`
The canonical end-to-end scoring regression test. Covers all player outcomes
in a single round:

| Player | Setup | Expected score | Why |
|---|---|---|---|
| SafeSam | Locked "cherry", no bounty | 1 | Survives tribunal |
| BountyBob | Locked "strawberry", bounty = "apple" | 2 | Survive + bounty |
| TrapTom | Locked "apple" (trap word) | 0 | Trapped; bounty blocked |
| VetoVic | Locked "brick" (gets vetoed) | 0 | Eliminated by tribunal |
| DecoyDan | Voted for decoy "tomato" | 0 | Honeypot forfeits the survival bonus |
| MindReaderMax | Locked "tomato" (matches decoy) | 2 | Mind Reader (+2 + immunity) |
| DealerDan | Dealer | 2 | 1 trapped + 1 bounty farmed |

**Key things this test guards against:**
- Bounty loop running for only the last player (Bug 1 — detached `p` reference)
- Honeypot silently no-oping for players at score 0 (fixed: honeypot is now a
  scoring-branch flag that blocks the survival bonus, not a `max(0, s-1)` clamp)
- Mind Reader players not being treated as immune or being paid the wrong amount
  (Mind Reader is +2, not +1 — the highest non-bounty outcome in the game)
- Trapped players being awarded bounty points
- `resolve_round` idempotency guard firing on a fresh room (phase must be
  `"tribunal"` before calling `resolve_round`)

**Canonical scoring reference:** the docstring at the top of
`logic/round_manager.py::resolve_round` is the single source of truth for the
priority chain, streak table, and clamp behavior. The client
(`static/app.js` reveal card) and `_client_side_points` in
`tests/test_edge_cases.py` are both mirrors of that docstring and must be
kept in lockstep.

### Streak Reset Rules

| Scoring outcome | Streak effect |
|---|---|
| 🌽 AFK / timed_out | **reset to 0** |
| 💥 Trapped | reset to 0 |
| 🧠 Mind Reader | +1 |
| 🗑️ Vetoed | reset to 0 |
| 🍯 Honeypot | preserved (locked word survived — only the vote was wrong) |
| ✅ Survived | +1 |

**Note on DecoyDan's expected score:**
DecoyDan locks "raspberry" (safe) and votes for the decoy "tomato". Under the
old clamp logic they'd end at **1** — the -1 penalty clamped to 0 and the
+1 survival bonus fired afterwards, silently disabling the honeypot for anyone
at score 0. The fix treats the honeypot as a scoring branch (like `vetoed` or
`trapped`) that forfeits the survival bonus outright. Final score = **0**.

---

### `test_round_manager_rulesets.py`
Tests the tribunal elimination logic in isolation for both rulesets.
Also covers several edge cases added during the bug-fix sessions.

| Test | What it proves |
|---|---|
| `test_competitive_threshold_and_max_removals` | Dynamic cap stops at 2 eliminations even when 3 words meet the threshold |
| `test_classic_majority_behavior_unchanged` | Strict majority (> 50%) correctly spares a word at exactly 50% |
| `test_idempotency_guard` | Calling `resolve_round` twice on an already-resolved room does not double scores |
| `test_honeypot_blocks_survival_bonus` | Honeypot voter scores exactly 0 (was `>= 0` under old clamp) |
| `test_mind_reader_timed_out_player_gets_no_immunity` | A cornballed player whose word matches the decoy gets no immunity |

### `test_edge_cases.py`
The regression battery for every hardened edge case + the deep interaction test.

| Test | What it proves |
|---|---|
| `test_deep_scoring_matrix_15_players` | Full 15-player round exercising every scoring branch (2× each). Verifies server score deltas, streaks (incl. AFK reset), flags, dealer bounty cap, `decoy_hidden_in_pool_total`, and client-side reveal-card math parity via `_client_side_points`. Prints a full round breakdown under `pytest -s` for rule-influence review. |
| `test_afk_resets_streak` | Player on a 5-round streak who AFKs is dropped to streak 0. |
| Honeypot group | 5 tests covering the priority chain (`hp both zero`, `preserves prior`, `does not block bounty`, `mind reader beats`, `vetoed beats`). |
| Mind Reader group | `awards +2` and `immune to veto` even when their locked word is on the vetoed list. |
| Sanitizer group | 6 tests on `sanitize_game_word` / `sanitize_player_name` / `sanitize_prompt`. |
| Dealer purge group | `next_round skips purged`, `next_round ends game when queue empty`, `resolve_round survives missing dealer`. |
| Reset group | `return_to_lobby clears all state`, `play_again clears stale tribunal state`. |
| Host reassignment group | `prefers connected`, `falls back to disconnected`, `noop when current host present`. |
| Rate limit / gate group | `ping burst blocks`, `can_cast_tribunal_vote matrix`. |
| Attach player group | 5 tests on `attach_player_to_room` (bad name, mid-game audience, lobby, session replace, banned). |
| Metrics group | `snapshot has all fields`, `merge additive`, `simulator surfaces guardrail metrics`. |

---

### `test_simulation_stats.py`
A self-contained multi-round integration simulation. It does **not** require
`tools/simulate_lobbies.py` — that module does not exist yet, and the original
test that imported it would have errored at import time.

This file provides its own `simulate()` function that runs real
`round_manager` logic across multiple lobbies and rounds, then asserts on the
shape and sanity of the resulting stats dict.

| Test | What it proves |
|---|---|
| `test_simulation_summary_shape_and_values` | The summary dict has all expected keys and no negative values |
| `test_simulation_classic_vs_competitive_elimination_rate` | Competitive mode never eliminates more words per round than Classic |

If you build `tools/simulate_lobbies.py` in the future, the expected return
shape is documented at the bottom of `test_simulation_stats.py`.

---

## Adding new tests

Follow this pattern for any new scoring or logic test:

```python
class DummyWS:
    async def send_json(self, data):
        return None

def test_my_scenario():
    room = Room(code="TEST", host="Dealer")
    room.phase = "tribunal"   # always set this before calling resolve_round
    room.ruleset = "classic"
    room.trap_word = "..."
    room.decoy_word = "..."
    room.current_dealer = "Dealer"

    room.players = {
        "Dealer": Player("Dealer", DummyWS()),
        "Alice":  Player("Alice",  DummyWS()),
    }
    room.players["Dealer"].is_dealer = True
    room.players["Alice"].locked_word = "something"

    round_manager.resolve_round(room)
    assert room.players["Alice"].score == 1
```

**Rules to follow:**
- Always pass a `DummyWS()` instance, never `None`, to `Player()`
- Always set `room.phase = "tribunal"` before calling `resolve_round` directly,
  otherwise the idempotency guard may fire on the wrong phase
- If testing `advance_to_tribunal`, set `room.phase = "response_phase"` first
  and populate `room.locked_words` before calling it — the cornball loop depends
  on players having `locked_word = None` to flag timeouts
- Inject `room.veto_votes` **after** calling `advance_to_tribunal`, since that
  function clears `veto_votes` as part of its setup
