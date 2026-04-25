import sys,os

# Go up one level from the 'test' folder and add the root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from tools.simulate_lobbies import simulate


def test_simulation_summary_shape_and_values():
    summary = simulate("competitive", lobbies=2, rounds_per_lobby=3, players=6)

    assert summary["meta"]["ruleset"] == "competitive"
    assert summary["meta"]["rounds"] == 6

    assert summary["performance"]["avg_round_duration_ms"] >= 0
    assert summary["player_actions"]["safe_locks_per_round"] >= 0
    assert summary["player_actions"]["bounty_locks_per_round"] >= 0
    assert summary["scoring"]["avg_total_points_per_round"] >= 0
    assert summary["tribunal_outcomes"]["avg_eliminations_per_round"] >= 0
