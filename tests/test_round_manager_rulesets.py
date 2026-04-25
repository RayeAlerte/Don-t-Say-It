import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from Models.game_state import Room, Player
from logic import round_manager


class DummyWS:
    async def send_json(self, data):
        return None


def make_room(player_names, host="p1"):
    room = Room("ABCD", host)
    for name in player_names:
        room.players[name] = Player(name, DummyWS(), role="active")
    return room


def force_reveal_with_votes(room, votes):
    """
    Simulates a full response phase → tribunal transition, then
    manually injects tribunal votes before resolving the round.

    Locked words: p2="green", p3="yellow", p4="purple"
    Trap word:    "red"
    Decoy word:   "blue"
    Any player without a locked word (e.g. p5) is cornballed by
    advance_to_tribunal and excluded from the voting pool.
    """
    room.phase = "response_phase"
    room.prompt = "Name a color in the rainbow."
    room.trap_word = "red"
    room.decoy_word = "blue"
    room.current_dealer = room.host
    room.players[room.host].is_dealer = True

    room.locked_words = {
        "green":  "p2",
        "yellow": "p3",
        "purple": "p4",
    }
    room.players["p2"].locked_word = "green"
    room.players["p3"].locked_word = "yellow"
    room.players["p4"].locked_word = "purple"
    # p5 (if present) has no locked_word → advance_to_tribunal cornballs them

    # advance_to_tribunal sets phase = "tribunal", clears veto_votes, builds words_to_vote
    round_manager.advance_to_tribunal(room)

    # Inject votes AFTER advance_to_tribunal (which clears veto_votes)
    room.veto_votes = votes

    # resolve_round checks phase == "reveal" as its idempotency guard.
    # Phase is currently "tribunal" so the guard does not fire.
    round_manager.resolve_round(room)


def test_competitive_threshold_and_max_removals():
    """
    5 active players, competitive ruleset.
    threshold  = max(2, ceil(5 * 0.35 + 0.9999)) = max(2, 2) = 2 votes required
    dynamic_max_removals = max(2, ceil(5 * 0.3))  = max(2, 2) = 2 words max eliminated

    "green":  3 votes ≥ 2 → eliminated (1st)
    "yellow": 3 votes ≥ 2 → eliminated (2nd — hits cap)
    "purple": 2 votes ≥ 2 but cap already reached → NOT eliminated
    """
    room = make_room(["p1", "p2", "p3", "p4", "p5"], host="p1")
    room.ruleset = "competitive"
    votes = {
        "green":  ["p1", "p2", "p3"],
        "yellow": ["p2", "p4", "p5"],
        "purple": ["p1", "p2"],
    }

    force_reveal_with_votes(room, votes)

    assert len(room.vetoed_words) == 2, \
        f"Expected 2 vetoed words, got {len(room.vetoed_words)}: {room.vetoed_words}"
    assert "green" in room.vetoed_words
    assert "yellow" in room.vetoed_words
    assert "purple" not in room.vetoed_words
    print("✅ Competitive: 2 words eliminated, cap correctly blocked purple")


def test_classic_majority_behavior_unchanged():
    """
    4 active players, classic ruleset.
    threshold = total_voters / 2 = 4 / 2 = 2.0
    A word needs STRICTLY MORE than 2.0 votes to be eliminated.

    "green":  2 votes → 2 > 2.0 is False → NOT eliminated
    "yellow": 3 votes → 3 > 2.0 is True  → eliminated
    """
    room = make_room(["p1", "p2", "p3", "p4"], host="p1")
    room.ruleset = "classic"
    votes = {
        "green":  ["p1", "p2"],
        "yellow": ["p1", "p2", "p3"],
    }

    force_reveal_with_votes(room, votes)

    assert "green" not in room.vetoed_words, \
        f"green should not be vetoed, got: {room.vetoed_words}"
    assert "yellow" in room.vetoed_words, \
        f"yellow should be vetoed, got: {room.vetoed_words}"
    print("✅ Classic: strict majority correctly eliminates yellow, spares green")


def test_idempotency_guard():
    """
    resolve_round must be a no-op if called a second time (phase already == "reveal").
    Scores must not double-count on the second call.
    """
    room = make_room(["p1", "p2", "p3","p4"], host="p1")
    room.ruleset = "classic"
    force_reveal_with_votes(room, {})

    # Capture scores after first resolution
    scores_after_first = {name: p.score for name, p in room.players.items()}

    # Call again — idempotency guard should bail out immediately
    round_manager.resolve_round(room)

    scores_after_second = {name: p.score for name, p in room.players.items()}
    assert scores_after_first == scores_after_second, \
        f"resolve_round is not idempotent!\nBefore: {scores_after_first}\nAfter: {scores_after_second}"
    print("✅ Idempotency: second resolve_round call is a safe no-op")


def test_honeypot_clamp_prevents_negative_score():
    """
    A player who votes for the decoy starts at score 0.
    Honeypot penalty: max(0, 0 - 1) = 0 — score must not go below zero.
    """
    room = make_room(["p1", "p2", "p3"], host="p1")
    room.ruleset = "classic"
    room.phase = "response_phase"
    room.prompt = "Name a color."
    room.trap_word = "red"
    room.decoy_word = "blue"
    room.current_dealer = "p1"
    room.players["p1"].is_dealer = True
    room.players["p2"].locked_word = "green"
    room.players["p3"].locked_word = "yellow"
    room.locked_words = {"green": "p2", "yellow": "p3"}

    round_manager.advance_to_tribunal(room)
    # p2 votes for the decoy "blue"
    room.veto_votes = {"blue": ["p2"]}
    round_manager.resolve_round(room)

    assert room.players["p2"].score >= 0, \
        f"Score went negative: {room.players['p2'].score}"
    assert room.players["p2"].caught_in_honeypot == True
    print(f"✅ Honeypot clamp: p2 score = {room.players['p2'].score} (≥ 0)")


def test_mind_reader_timed_out_player_gets_no_immunity():
    """
    A timed-out (cornballed) player whose locked_word happens to match the decoy
    must NOT receive Mind Reader immunity — timed_out takes priority.
    """
    room = make_room(["p1", "p2", "p3"], host="p1")
    room.ruleset = "classic"
    room.phase = "response_phase"
    room.prompt = "Name a color."
    room.trap_word = "red"
    room.decoy_word = "blue"
    room.current_dealer = "p1"
    room.players["p1"].is_dealer = True
    room.players["p2"].locked_word = "green"
    # p3 has no locked_word → cornballed by advance_to_tribunal
    room.locked_words = {"green": "p2"}

    round_manager.advance_to_tribunal(room)
    # Manually force p3's cornball word to "blue" to simulate the edge case
    room.players["p3"].locked_word = "blue"
    room.players["p3"].timed_out = True

    round_manager.resolve_round(room)

    assert room.players["p3"].score == 0, \
        f"Timed-out player should score 0, got {room.players['p3'].score}"
    assert room.players["p3"].mind_reader == False, \
        "Timed-out player should not be marked mind_reader"
    print("✅ Mind Reader immunity correctly denied to timed-out player")


if __name__ == "__main__":
    test_competitive_threshold_and_max_removals()
    test_classic_majority_behavior_unchanged()
    test_idempotency_guard()
    test_honeypot_clamp_prevents_negative_score()
    test_mind_reader_timed_out_player_gets_no_immunity()
    print("\n🎉 ALL RULESET TESTS PASSED!")
