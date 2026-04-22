# Don't Say It
A game where you don't say it.

## Rulesets
- `classic`: original tribunal behavior (multi-vote, strict majority elimination)
- `competitive`: host-enabled A/B mode with:
  - one vote per active player in tribunal
  - dynamic threshold: `max(2, ceil(35% of active voters))`
  - max `2` vetoed words per round (Option C)
  - dealer bounty bonus cap (already enforced in backend)

Hosts can switch rulesets in the lobby using the `Ruleset` dropdown.

## Local Test Run
1. Install dependencies:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`
2. Run tests:
   - `pytest -q`

## Outcome Simulation / Lobby Statistics
Use the simulation harness to produce repeatable lobby-level metrics for analysis:

- Run competitive simulation:
  - `python tools/simulate_lobbies.py --ruleset competitive --lobbies 25 --rounds-per-lobby 8 --players 8`
- Save to JSON:
  - `python tools/simulate_lobbies.py --ruleset classic --output artifacts/classic-summary.json`

Metrics include:
- performance (`avg_round_duration_ms`, `p95_round_duration_ms`)
- player actions (`safe_locks_per_round`, `bounty_locks_per_round`, `tribunal_votes_per_round`)
- scoring (`avg_total_points_per_round`, `avg_dealer_points_per_round`)
- tribunal outcomes (`avg_eliminations_per_round`, `honeypot_hits_total`)

## CI Example (GitHub Actions)
```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install --upgrade pip
      - run: pip install -r requirements.txt
      - run: pytest -q
      - run: python tools/simulate_lobbies.py --ruleset competitive --output competitive-summary.json
      - run: python tools/simulate_lobbies.py --ruleset classic --output classic-summary.json
```
