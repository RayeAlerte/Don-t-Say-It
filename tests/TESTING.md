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
| DecoyDan | Voted for decoy "tomato" | 1 | Honeypot clamped to 0, then survives (+1) |
| MindReaderMax | Locked "tomato" (matches decoy) | 1 | Mind Reader immunity |
| DealerDan | Dealer | 2 | 1 trapped + 1 bounty farmed |

**Key things this test guards against:**
- Bounty loop running for only the last player (Bug 1 — detached `p` reference)
- Honeypot penalty going below 0 (Bug 1 — score clamp)
- Mind Reader players not being treated as immune
- Trapped players being awarded bounty points
- `resolve_round` idempotency guard firing on a fresh room (phase must be
  `"tribunal"` before calling `resolve_round`)

**Note on DecoyDan's expected score:**
DecoyDan starts at 0. The honeypot penalty fires first:
`max(0, 0 - 1) = 0` (clamped). Then survival scoring awards `+1`.
Final score = **1**, not 0. The original test had this wrong (`== 0`)
because it predated the score clamp fix.

---

### `test_round_manager_rulesets.py`
Tests the tribunal elimination logic in isolation for both rulesets.
Also covers several edge cases added during the bug-fix sessions.

| Test | What it proves |
|---|---|
| `test_competitive_threshold_and_max_removals` | Dynamic cap stops at 2 eliminations even when 3 words meet the threshold |
| `test_classic_majority_behavior_unchanged` | Strict majority (> 50%) correctly spares a word at exactly 50% |
| `test_idempotency_guard` | Calling `resolve_round` twice on an already-resolved room does not double scores |
| `test_honeypot_clamp_prevents_negative_score` | A player at 0 who hits the honeypot stays at 0, not -1 |
| `test_mind_reader_timed_out_player_gets_no_immunity` | A cornballed player whose word matches the decoy gets no immunity |

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
