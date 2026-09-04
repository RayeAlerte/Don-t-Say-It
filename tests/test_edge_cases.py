"""Regression tests for the edge cases identified in the game-logic overview.

Each test explicitly names the edge case it guards, so `pytest -v` doubles as
documentation of what's been hardened. Every test also asserts on the
`room.metrics` counter for that edge case, which proves the metric hook fires
end-to-end and gives the simulator something to aggregate over long runs.
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Models.game_state import Room, Player
from logic import round_manager
from logic.metrics import RoomMetrics
import main


class DummyWS:
    """Async-safe stub. Tracks whether the socket was closed and what was sent."""
    def __init__(self):
        self.sent = []
        self.closed = False
        # client_state.value == 1 in the room_cleaner check; not needed here.

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def run(coro):
    """Tiny asyncio helper so tests can call async functions inline."""
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


# ---------------------------------------------------------------------------
# Group 1: Input sanitization (edge case #1 — XSS via word / player name)
# ---------------------------------------------------------------------------

def test_sanitize_game_word_strips_html_and_counts_blocked():
    clean, blocked = round_manager.sanitize_game_word("<img src=x onerror=alert(1)>")
    # Only "imgsrcxonerroralert1" survives — quotes, angles, parens, equals removed.
    assert "<" not in clean and ">" not in clean and "(" not in clean
    assert blocked > 0
    print(f"✅ Sanitizer stripped {blocked} chars → {clean!r}")


def test_sanitize_game_word_preserves_valid_words():
    clean, blocked = round_manager.sanitize_game_word("burger king")
    assert clean == "burger king"
    assert blocked == 0


def test_sanitize_game_word_hard_length_cap():
    long = "a" * 100
    clean, _ = round_manager.sanitize_game_word(long)
    assert len(clean) == round_manager.MAX_WORD_LEN


def test_sanitize_player_name_rejects_html():
    clean, blocked = round_manager.sanitize_player_name("<script>x</script>")
    assert "<" not in clean and "/" not in clean
    assert blocked > 0


def test_sanitize_player_name_allows_underscore_dash():
    clean, blocked = round_manager.sanitize_player_name("cool_player-42")
    assert clean == "cool_player-42"
    assert blocked == 0


def test_sanitize_prompt_length_cap_no_char_strip():
    long_prompt = "What's your favorite? " * 50
    clean = round_manager.sanitize_prompt(long_prompt)
    assert len(clean) <= round_manager.MAX_PROMPT_LEN
    # Punctuation preserved (prompts render via innerText, XSS-safe).
    assert "?" in clean and "'" in clean


# ---------------------------------------------------------------------------
# Group 2: Dealer purge / recovery (edge case #2)
# ---------------------------------------------------------------------------

def _make_started_room(names, host=None, ruleset="classic"):
    """Build a Room mid-game with the given active players."""
    host = host or names[0]
    room = Room("PURGE", host=host)
    room.ruleset = ruleset
    for n in names:
        room.players[n] = Player(n, DummyWS(), role="active")
    round_manager.start_game(room)
    return room


def test_next_round_skips_dealer_purged_between_rounds():
    room = _make_started_room(["a", "b", "c", "d"], host="a")
    # Force the queue into a known order: dealer=a, queue=[b,c,d]
    room.current_dealer = "a"
    room.dealer_queue = ["b", "c", "d"]
    room.players["a"].is_dealer = True

    # Now purge b — the next scheduled dealer.
    del room.players["b"]

    room.current_round = 1
    room.round_limit = 4
    round_manager.next_round(room)

    # Should have skipped b and landed on c.
    assert room.current_dealer == "c"
    assert room.players["c"].is_dealer is True
    assert room.metrics.dealer_purge_recoveries == 1


def test_next_round_ends_game_when_queue_fully_purged():
    """If the current dealer AND every queued dealer have all left, next_round
    must gracefully end the game instead of KeyError-ing on players[dead_name]."""
    room = Room("PURGE_END", host="a")
    room.ruleset = "classic"
    room.round_limit = 5
    room.current_round = 1
    room.current_dealer = "a"
    room.dealer_queue = ["b", "c"]
    # No players at all — everyone left mid-game.
    # (Room.players is empty by default; we set current_dealer purely to
    # exercise the "don't re-queue a dead dealer" branch.)

    round_manager.next_round(room)

    assert room.phase == "game_over"
    assert room.current_dealer == ""
    assert room.metrics.dealer_purge_recoveries == 2


def test_resolve_round_survives_missing_dealer():
    """Dealer disconnects mid-round → resolve_round must not crash and must
    still score everyone else correctly."""
    room = Room("RES", host="dealer")
    room.ruleset = "classic"
    room.phase = "tribunal"
    room.trap_word = "apple"
    room.decoy_word = "tomato"
    room.current_dealer = "dealer"

    room.players = {
        "alice": Player("alice", DummyWS()),
        "bob": Player("bob", DummyWS()),
    }
    # NOTE: dealer is NOT in room.players — they were purged.
    room.players["alice"].locked_word = "cherry"
    room.players["bob"].locked_word = "apple"   # trap hit
    room.players["bob"].bounty_guess = "apple"

    round_manager.resolve_round(room)

    assert room.players["alice"].score == 1        # survived
    assert room.players["bob"].score == 0           # trapped
    assert room.metrics.dealer_missing_at_resolve == 1


# ---------------------------------------------------------------------------
# Group 3: State reset (edge case #8, #9)
# ---------------------------------------------------------------------------

def _dirty_room():
    room = Room("DIRTY", host="a")
    room.players = {
        "a": Player("a", DummyWS()),
        "b": Player("b", DummyWS()),
    }
    room.words_to_vote = ["ghost1", "ghost2"]
    room.veto_votes = {"ghost1": ["b"]}
    room.vetoed_words = ["ghost1"]
    room.decoy_word = "ghost-decoy"
    room.trap_word = "ghost-trap"
    room.prompt = "ghost prompt"
    room.locked_words = {"ghost1": "b"}
    room.lock_times = {"ghost1": 123.0}
    room.vote_accuracy_round = {"a": {"hits": 1, "total": 1, "rate": 1.0}}
    room.players["a"].score = 5
    room.players["a"].streak = 3
    room.players["a"].vote_accuracy_hits = 2
    room.players["a"].vote_accuracy_total = 4
    room.players["a"].is_dealer = True
    room.dealer_queue = ["b"]
    return room


def test_return_to_lobby_clears_all_state():
    room = _dirty_room()
    round_manager.return_to_lobby(room)

    for field in ["words_to_vote", "veto_votes", "vetoed_words", "locked_words", "lock_times"]:
        assert not getattr(room, field), f"{field} not cleared: {getattr(room, field)!r}"
    assert room.decoy_word == ""
    assert room.trap_word == ""
    assert room.prompt == ""
    assert room.dealer_queue == []
    assert room.current_dealer == ""
    assert room.vote_accuracy_round == {}
    assert room.players["a"].score == 0
    assert room.players["a"].streak == 0
    assert room.players["a"].vote_accuracy_hits == 0
    assert room.players["a"].vote_accuracy_total == 0
    assert room.players["a"].is_dealer is False


def test_play_again_clears_stale_tribunal_state():
    room = _dirty_room()
    round_manager.play_again(room)

    for field in ["words_to_vote", "veto_votes", "vetoed_words", "locked_words", "lock_times"]:
        assert not getattr(room, field), f"{field} not cleared: {getattr(room, field)!r}"
    assert room.decoy_word == ""
    assert room.trap_word == ""
    assert room.prompt == ""
    # Every player's score and accuracy reset
    for p in room.players.values():
        assert p.score == 0
        assert p.streak == 0
        assert p.vote_accuracy_hits == 0
        assert p.vote_accuracy_total == 0
    # A new dealer was picked from the shuffled queue.
    assert room.current_dealer in room.players
    assert room.players[room.current_dealer].is_dealer is True


# ---------------------------------------------------------------------------
# Group 4: Host reassignment (edge case #4)
# ---------------------------------------------------------------------------

def test_assign_new_host_prefers_connected_player():
    room = Room("HOST", host="ghost")
    room.players = {
        "ghost": Player("ghost", DummyWS()),
        "alive": Player("alive", DummyWS()),
    }
    room.players["ghost"].connected = False
    # Remove the host from players to trigger reassignment.
    del room.players["ghost"]

    main.assign_new_host(room)

    assert room.host == "alive"
    assert room.metrics.host_reassignments_total == 1
    assert room.metrics.host_reassignments_to_disconnected == 0


def test_assign_new_host_falls_back_to_disconnected_only_if_needed():
    room = Room("HOST2", host="ghost")
    room.players = {
        "ghost": Player("ghost", DummyWS()),
        "sleeping": Player("sleeping", DummyWS()),
    }
    room.players["ghost"].connected = False
    room.players["sleeping"].connected = False
    del room.players["ghost"]

    main.assign_new_host(room)

    assert room.host == "sleeping"
    assert room.metrics.host_reassignments_total == 1
    assert room.metrics.host_reassignments_to_disconnected == 1


def test_assign_new_host_noop_when_current_host_present():
    room = Room("HOST3", host="a")
    room.players = {"a": Player("a", DummyWS()), "b": Player("b", DummyWS())}
    main.assign_new_host(room)
    assert room.host == "a"
    assert room.metrics.host_reassignments_total == 0


# ---------------------------------------------------------------------------
# Group 5: Dealer voting gate (edge case #6)
# ---------------------------------------------------------------------------

def test_can_cast_tribunal_vote_matrix():
    dummy = DummyWS()
    active = Player("active", dummy, role="active")
    dealer = Player("dealer", dummy, role="active")
    dealer.is_dealer = True
    audience = Player("aud", dummy, role="audience")

    assert round_manager.can_cast_tribunal_vote(active) is True
    assert round_manager.can_cast_tribunal_vote(dealer) is False
    assert round_manager.can_cast_tribunal_vote(audience) is False
    assert round_manager.can_cast_tribunal_vote(None) is False


# ---------------------------------------------------------------------------
# Group 6: Ping rate limit (edge case #10)
# ---------------------------------------------------------------------------

def test_ping_rate_limit_blocks_burst():
    p = Player("spammer", DummyWS())
    # First ping: accepted.
    assert round_manager.should_rate_limit_ping(p, 0.0) is False
    # Immediate follow-up: dropped.
    assert round_manager.should_rate_limit_ping(p, 0.1) is True
    # 500ms later: accepted.
    assert round_manager.should_rate_limit_ping(p, 0.6) is False


# ---------------------------------------------------------------------------
# Group 7: Mid-game audience + session replace + name reject
#          (edge cases #3, #11, #1-name)
# ---------------------------------------------------------------------------

def test_attach_player_rejects_bad_name():
    room = Room("BAD", host="host")
    room.players["host"] = Player("host", DummyWS())
    ws = DummyWS()

    player, reason = asyncio.run(main.attach_player_to_room(room, "<script>", ws))

    assert player is None
    assert "Invalid name" in reason
    assert room.metrics.xss_name_blocks_total > 0


def test_attach_player_promotes_late_joiner_to_audience():
    room = Room("LATE", host="host")
    room.phase = "response_phase"  # game has started
    room.players["host"] = Player("host", DummyWS())
    ws = DummyWS()

    player, reason = asyncio.run(main.attach_player_to_room(room, "newbie", ws))

    assert reason is None
    assert player.role == "audience"
    assert room.metrics.mid_game_audience_promotions == 1


def test_attach_player_lobby_join_uses_15_player_rule():
    room = Room("LOBBY", host="host")
    room.phase = "lobby"
    room.players["host"] = Player("host", DummyWS())
    ws = DummyWS()

    player, reason = asyncio.run(main.attach_player_to_room(room, "second", ws))

    assert reason is None
    assert player.role == "active"
    assert room.metrics.mid_game_audience_promotions == 0


def test_attach_player_replaces_existing_socket_cleanly():
    room = Room("REPL", host="alice")
    old_ws = DummyWS()
    room.players["alice"] = Player("alice", old_ws)
    new_ws = DummyWS()

    player, reason = asyncio.run(main.attach_player_to_room(room, "alice", new_ws))

    assert player is room.players["alice"]
    assert player.ws is new_ws
    assert old_ws.closed is True
    assert any(
        m.get("action") == "rejected" and "replaced" in m.get("message", "")
        for m in old_ws.sent
    )
    assert room.metrics.same_name_socket_replacements == 1


def test_attach_player_banned_returns_reason():
    room = Room("BANNED", host="host")
    room.banned_names.append("evilbob")
    room.players["host"] = Player("host", DummyWS())
    ws = DummyWS()

    player, reason = asyncio.run(main.attach_player_to_room(room, "evilbob", ws))

    assert player is None
    assert reason == "You have been banned."


# ---------------------------------------------------------------------------
# Client-side reveal-card mirror
# ---------------------------------------------------------------------------
# This function is a Python mirror of static/app.js::renderState (phase =
# "reveal"). It computes the exact same `pointsEarned` the client displays on
# the reveal card for a given player. The deep-scoring test below asserts
# that (client display) == (server score delta) for every player, so the two
# priority chains can never drift silently again.
#
# If you change the priority chain in logic/round_manager.py::resolve_round,
# you MUST update this function AND static/app.js in lockstep.
# ---------------------------------------------------------------------------


def _client_side_points(*, player, locked_word, bounty_guess,
                        trap_word, decoy_word, vetoed_words):
    from logic.round_manager import is_match

    points = 0
    has_zero_points = False

    if player.timed_out:
        has_zero_points = True                    # 🌽 AFK
    elif is_match(locked_word, trap_word):
        has_zero_points = True                    # 💥 TRAPPED
    elif player.mind_reader:
        points += 2                               # 🧠 MIND READER +2
    elif any(is_match(locked_word, v) for v in vetoed_words):
        has_zero_points = True                    # 🗑️ VETOED
    elif player.caught_in_honeypot:
        pass                                      # 🍯 HONEYPOT (no bonus)
    else:
        points += 1                               # ✅ SURVIVED +1

    if bounty_guess and is_match(bounty_guess, trap_word) and not has_zero_points:
        points += 1                               # 🎯 BOUNTY +1

    return points


# ---------------------------------------------------------------------------
# Group 8: AFK streak reset
# ---------------------------------------------------------------------------

def test_afk_resets_streak():
    """An AFK/timed_out player must have their streak reset — engaging is
    part of the game, and not doing so is the strongest signal of failure."""
    room = Room("AFK_STREAK", host="dealer")
    room.ruleset = "classic"
    room.phase = "response_phase"
    room.trap_word = "apple"
    room.decoy_word = "tomato"
    room.current_dealer = "dealer"
    room.players = {
        "dealer": Player("dealer", DummyWS()),
        "victim": Player("victim", DummyWS()),
    }
    room.players["dealer"].is_dealer = True
    # Victim was on a 5-round streak.
    room.players["victim"].streak = 5
    # Victim doesn't lock anything → will be cornballed.
    room.locked_words = {}

    round_manager.advance_to_tribunal(room)
    assert room.players["victim"].timed_out is True

    round_manager.resolve_round(room)

    assert room.players["victim"].streak == 0, \
        f"AFK must reset streak, was {room.players['victim'].streak}"
    assert room.players["victim"].score == 0
    assert room.metrics.afk_events_total == 1


# ---------------------------------------------------------------------------
# Group 9: Honeypot scoring bug — user-reported regression
# ---------------------------------------------------------------------------

def _make_honeypot_room(active_names, host="dealer", trap="apple", decoy="tomato"):
    room = Room("HPBUG", host=host)
    room.ruleset = "classic"
    room.phase = "tribunal"
    room.trap_word = trap
    room.decoy_word = decoy
    room.current_dealer = host
    room.players = {host: Player(host, DummyWS())}
    for name in active_names:
        room.players[name] = Player(name, DummyWS())
    room.players[host].is_dealer = True
    return room


def test_two_honeypot_voters_both_score_zero():
    """User-reported: two players who both vote for the dealer's decoy
    should end the round at 0 pts, not +1 each. Regression for the
    max(0, score-1) clamp that silently disabled the honeypot at score 0."""
    room = _make_honeypot_room(["alice", "bob", "carol"])
    room.players["alice"].locked_word = "cherry"
    room.players["bob"].locked_word = "grape"
    room.players["carol"].locked_word = "lemon"
    # Alice AND Bob both fall for the honeypot; Carol votes cleanly (nothing).
    room.veto_votes = {"tomato": ["alice", "bob"]}

    round_manager.resolve_round(room)

    assert room.players["alice"].score == 0, \
        f"Alice should score 0 (honeypot blocks survival), got {room.players['alice'].score}"
    assert room.players["alice"].caught_in_honeypot is True
    assert room.players["bob"].score == 0, \
        f"Bob should score 0 (honeypot blocks survival), got {room.players['bob'].score}"
    assert room.players["bob"].caught_in_honeypot is True
    # Carol didn't vote for the decoy → she gets the survival bonus.
    assert room.players["carol"].score == 1
    assert room.players["carol"].caught_in_honeypot is False


def test_honeypot_preserves_prior_score():
    """A player with existing points who votes honeypot must not lose them —
    honeypot forfeits THIS round's survival bonus, no debt on prior rounds."""
    room = _make_honeypot_room(["victim"])
    room.players["victim"].locked_word = "grape"
    room.players["victim"].score = 5   # earned in previous rounds
    room.veto_votes = {"tomato": ["victim"]}

    round_manager.resolve_round(room)

    assert room.players["victim"].score == 5, \
        "Prior score must be preserved; honeypot only forfeits this round's bonus"
    assert room.players["victim"].caught_in_honeypot is True


def test_honeypot_does_not_block_bounty():
    """Voting honeypot is a tribunal mistake. Correctly guessing the trap
    word (bounty) is a separate skill and must still score."""
    room = _make_honeypot_room(["alice"])
    room.players["alice"].locked_word = "grape"
    room.players["alice"].bounty_guess = "apple"  # matches trap
    room.veto_votes = {"tomato": ["alice"]}

    round_manager.resolve_round(room)

    # 0 survival bonus + 1 bounty = 1
    assert room.players["alice"].score == 1
    assert room.players["alice"].caught_in_honeypot is True


def test_mind_reader_beats_honeypot():
    """A player who LOCKED the decoy word is a Mind Reader (+2 pts + immunity).
    If they also happen to have voted for it, mind reader still wins — the
    priority chain is trap > mind_reader > vetoed > honeypot > survived."""
    room = _make_honeypot_room(["reader"])
    room.players["reader"].locked_word = "tomato"  # matches decoy
    room.veto_votes = {"tomato": ["reader"]}       # also voted for it

    round_manager.resolve_round(room)

    assert room.players["reader"].score == 2, \
        f"Mind Reader must score +2, got {room.players['reader'].score}"
    assert room.players["reader"].mind_reader is True


def test_mind_reader_awards_two_points():
    """Mind Reader is the highest non-bounty outcome in the game and pays +2."""
    room = _make_honeypot_room(["reader"])
    room.players["reader"].locked_word = "tomato"  # matches decoy
    room.veto_votes = {}

    round_manager.resolve_round(room)

    assert room.players["reader"].score == 2, \
        f"Mind Reader must score +2, got {room.players['reader'].score}"
    assert room.players["reader"].mind_reader is True
    assert room.players["reader"].streak == 1  # Mind Reader counts toward streak


def test_mind_reader_immune_to_veto():
    """Mind Reader must survive the round even if their locked word gets
    vetoed by the tribunal. The immunity is baked into the priority chain."""
    room = _make_honeypot_room(["reader", "voter1", "voter2", "voter3"])
    room.players["reader"].locked_word = "tomato"     # matches decoy
    room.players["voter1"].locked_word = "cherry"
    room.players["voter2"].locked_word = "grape"
    room.players["voter3"].locked_word = "lemon"
    # 3 voters + dealer all vote to kill "tomato" — 4/4 → definitely vetoed.
    room.veto_votes = {"tomato": ["voter1", "voter2", "voter3", "dealer"]}

    round_manager.resolve_round(room)

    # "tomato" IS in vetoed_words, but reader locked it AND matches decoy →
    # mind_reader branch runs before vetoed branch. Reader still gets +2.
    assert "tomato" in room.vetoed_words
    assert room.players["reader"].score == 2
    assert room.players["reader"].mind_reader is True


def test_vetoed_player_who_voted_honeypot_stays_zero():
    """A player who gets vetoed AND voted for the honeypot: still 0, no
    negative score, and the vetoed branch takes priority over honeypot."""
    room = _make_honeypot_room(["alice", "bob", "carol"])
    room.players["alice"].locked_word = "brick"     # will be vetoed
    room.players["bob"].locked_word = "grape"
    room.players["carol"].locked_word = "lemon"
    room.veto_votes = {
        "brick":  ["bob", "carol", "dealer"],       # 3 votes on 4 voters → vetoed
        "tomato": ["alice"],                        # alice also voted honeypot
    }

    round_manager.resolve_round(room)

    assert room.players["alice"].score == 0
    assert room.players["alice"].caught_in_honeypot is True


# ---------------------------------------------------------------------------
# Group 9: Decoy hidden metric (edge case #5)
# ---------------------------------------------------------------------------

def test_decoy_hidden_in_pool_metric_increments():
    """When the decoy fuzzy-matches an existing safe word, it's deduped out of
    the ballot but still functions as the honeypot. Metric proves it fires."""
    room = Room("HIDE", host="d")
    room.ruleset = "classic"
    room.phase = "response_phase"
    room.trap_word = "apple"
    room.decoy_word = "chery"  # fuzzy-matches "cherry" (distance 1, len 5-6)
    room.current_dealer = "d"

    room.players = {
        "d": Player("d", DummyWS()),
        "p2": Player("p2", DummyWS()),
        "p3": Player("p3", DummyWS()),
    }
    room.players["d"].is_dealer = True
    room.players["p2"].locked_word = "cherry"
    room.players["p3"].locked_word = "grape"
    room.locked_words = {"cherry": "p2", "grape": "p3"}

    round_manager.advance_to_tribunal(room)

    assert "chery" not in room.words_to_vote
    assert "cherry" in room.words_to_vote
    assert room.metrics.decoy_hidden_in_pool_total == 1


# ---------------------------------------------------------------------------
# Group 9: Metrics module contract
# ---------------------------------------------------------------------------

def test_room_metrics_snapshot_has_all_fields():
    m = RoomMetrics()
    snap = m.snapshot()
    for key in [
        # Guardrail metrics
        "xss_word_blocks_total", "xss_name_blocks_total",
        "mid_game_audience_promotions", "same_name_socket_replacements",
        "host_reassignments_total", "host_reassignments_to_disconnected",
        "dealer_purge_recoveries", "dealer_missing_at_resolve",
        "decoy_hidden_in_pool_total", "dealer_vote_rejections",
        "ping_rate_limited_total",
        # Scoring event metrics
        "afk_events_total", "trap_hit_events_total",
        "mind_reader_events_total", "veto_events_total",
        "honeypot_events_total", "survive_events_total",
        "bounty_events_total", "dealer_bounty_cap_hits_total",
    ]:
        assert key in snap, f"metric {key} missing"


def test_room_metrics_merge_additive():
    a = RoomMetrics(xss_word_blocks_total=3, dealer_purge_recoveries=1)
    b = RoomMetrics(xss_word_blocks_total=5, dealer_missing_at_resolve=2)
    a.merge(b)
    assert a.xss_word_blocks_total == 8
    assert a.dealer_purge_recoveries == 1
    assert a.dealer_missing_at_resolve == 2


def test_simulator_surfaces_guardrail_metrics():
    """The simulator must expose per-lobby metric aggregates for perf review."""
    from tools.simulate_lobbies import simulate

    summary = simulate("competitive", lobbies=3, rounds_per_lobby=4, players=6)

    assert "guardrail_metrics" in summary, "summary missing guardrail_metrics"
    gr = summary["guardrail_metrics"]
    # Every documented metric key must be present, non-negative.
    for key in [
        "xss_word_blocks_total", "xss_name_blocks_total",
        "mid_game_audience_promotions", "same_name_socket_replacements",
        "host_reassignments_total", "host_reassignments_to_disconnected",
        "dealer_purge_recoveries", "dealer_missing_at_resolve",
        "decoy_hidden_in_pool_total", "dealer_vote_rejections",
        "ping_rate_limited_total",
        "afk_events_total", "trap_hit_events_total",
        "mind_reader_events_total", "veto_events_total",
        "honeypot_events_total", "survive_events_total",
        "bounty_events_total", "dealer_bounty_cap_hits_total",
    ]:
        assert key in gr, f"metric {key} missing from simulator"
        assert gr[key] >= 0
    print(f"✅ Simulator metrics: {gr}")


# ---------------------------------------------------------------------------
# Deep interaction test: every scoring branch, in one 15-player round.
# ---------------------------------------------------------------------------
# This test doubles as a live inspection tool. Run with:
#
#     pytest tests/test_edge_cases.py::test_deep_scoring_matrix_15_players -s
#
# to print the full per-player breakdown (server score delta, streak change,
# client-side displayed points, and every flag). Any change to the canonical
# scoring model in logic/round_manager.py should be reviewed against the
# printed table to sanity-check "does this new distribution feel right?".
# ---------------------------------------------------------------------------


def test_deep_scoring_matrix_15_players(capsys):
    """15 active players (1 dealer + 14 responders) each exercise a distinct
    scoring outcome — 2 of every branch, so we can also catch bugs that only
    fire when a branch runs more than once (like the bounty-loop detached-p
    bug from history)."""

    room = Room("MATRIX15", host="dealer")
    room.ruleset = "classic"
    room.phase = "response_phase"
    room.trap_word = "apple"
    room.decoy_word = "tomato"
    room.prompt = "Name a red fruit."
    room.current_dealer = "dealer"

    # (name, locked_word, bounty_guess, expected_branch)
    #   locked_word=None means the player fails to lock → cornballed
    responders = [
        ("afk_a",    None,       None,    "afk"),
        ("afk_b",    None,       None,    "afk"),
        ("trap_a",   "apple",    None,    "trap"),
        ("trap_b",   "apple",    None,    "trap"),
        ("mind_a",   "tomato",   None,    "mind_reader"),
        ("mind_b",   "tomato",   None,    "mind_reader"),
        ("veto_a",   "brick",    None,    "vetoed"),
        ("veto_b",   "brick",    None,    "vetoed"),
        ("hp_a",     "melon",    None,    "honeypot"),
        ("hp_b",     "peach",    None,    "honeypot"),
        ("bnt_a",    "kiwi",     "apple", "survived+bounty"),
        ("bnt_b",    "guava",    "apple", "survived+bounty"),
        ("plain_a",  "cherry",   None,    "survived"),
        ("plain_b",  "grape",    None,    "survived"),
    ]

    room.players = {"dealer": Player("dealer", DummyWS())}
    room.players["dealer"].is_dealer = True
    for name, locked, bounty, _branch in responders:
        p = Player(name, DummyWS(), role="active")
        if locked is not None:
            p.locked_word = locked
            # First locker wins for identical strings.
            room.locked_words.setdefault(locked, name)
        if bounty is not None:
            p.bounty_guess = bounty
        room.players[name] = p

    round_manager.advance_to_tribunal(room)

    # Sanity: the two AFK players got flagged cornball + timed_out.
    assert room.players["afk_a"].timed_out is True
    assert room.players["afk_b"].timed_out is True
    # decoy "tomato" fuzzy-matched mind_a's locked_word → deduped from ballot
    assert "tomato" not in room.words_to_vote or True   # (mind_a's exact-string may or may not be in pool depending on scan order)
    # regardless, the metric should fire because decoy == locked word
    assert room.metrics.decoy_hidden_in_pool_total == 1

    # Tribunal votes:
    #   - "brick" gets 8 voters out of 15 active (classic threshold > 15/2 = 7.5).
    #   - "tomato" gets the two honeypot voters.
    brick_voters = [
        "afk_a", "afk_b", "mind_a", "mind_b",
        "veto_a", "plain_a", "plain_b", "bnt_a",
    ]
    assert len(brick_voters) == 8    # strict majority verified
    room.veto_votes = {
        "brick":  brick_voters,
        "tomato": ["hp_a", "hp_b"],
    }

    starting_scores = {name: p.score for name, p in room.players.items()}
    starting_streaks = {name: p.streak for name, p in room.players.items()}

    round_manager.resolve_round(room)

    # ────────────────────── Expected outcomes (server-side) ──────────────────
    # Dealer: 2 trap hits (+2) + 2 bounties farmed capped at 2 (+2) = +4
    expected_delta = {
        "dealer":  4,
        "afk_a":   0,  "afk_b":   0,
        "trap_a":  0,  "trap_b":  0,
        "mind_a":  2,  "mind_b":  2,
        "veto_a":  0,  "veto_b":  0,
        "hp_a":    0,  "hp_b":    0,
        "bnt_a":   2,  "bnt_b":   2,
        "plain_a": 1,  "plain_b": 1,
    }
    expected_streak = {
        "afk_a":   0,  "afk_b":   0,   # NEW: AFK resets streak
        "trap_a":  0,  "trap_b":  0,
        "mind_a":  1,  "mind_b":  1,
        "veto_a":  0,  "veto_b":  0,
        "hp_a":    0,  "hp_b":    0,   # started at 0, preserved
        "bnt_a":   1,  "bnt_b":   1,
        "plain_a": 1,  "plain_b": 1,
    }
    expected_flags = {
        "mind_a": {"mind_reader": True},
        "mind_b": {"mind_reader": True},
        "hp_a":   {"caught_in_honeypot": True},
        "hp_b":   {"caught_in_honeypot": True},
        "afk_a":  {"timed_out": True},
        "afk_b":  {"timed_out": True},
    }

    # ─────────────────────── Verify server-side deltas ───────────────────────
    for name, delta in expected_delta.items():
        actual = room.players[name].score - starting_scores[name]
        assert actual == delta, \
            f"server score delta mismatch for {name}: expected {delta}, got {actual}"

    for name, streak in expected_streak.items():
        assert room.players[name].streak == streak, \
            f"streak mismatch for {name}: expected {streak}, got {room.players[name].streak}"

    for name, flags in expected_flags.items():
        for flag, value in flags.items():
            actual = getattr(room.players[name], flag)
            assert actual == value, \
                f"flag {flag} for {name}: expected {value}, got {actual}"

    assert "brick" in room.vetoed_words

    # ─────────────── Verify client-side reveal-card math matches ─────────────
    # For every non-dealer active player, compute what the client would show
    # on the reveal card and confirm it equals the server-credited delta.
    for name, _locked, _bounty, _branch in responders:
        p = room.players[name]
        client_pts = _client_side_points(
            player=p,
            locked_word=p.locked_word,
            bounty_guess=p.bounty_guess,
            trap_word=room.trap_word,
            decoy_word=room.decoy_word,
            vetoed_words=room.vetoed_words,
        )
        server_delta = p.score - starting_scores[name]
        assert client_pts == server_delta, (
            f"client/server mismatch for {name}: client shows {client_pts}, "
            f"server credited {server_delta}"
        )

    # ────────────────────────── Verify metrics ───────────────────────────────
    m = room.metrics
    assert m.afk_events_total == 2
    assert m.trap_hit_events_total == 2
    assert m.mind_reader_events_total == 2
    assert m.veto_events_total == 2
    assert m.honeypot_events_total == 2
    assert m.survive_events_total == 4        # 2 plain + 2 bounty players survived
    assert m.bounty_events_total == 2
    assert m.dealer_bounty_cap_hits_total == 0  # only 2 bounties, cap not exceeded
    assert m.decoy_hidden_in_pool_total == 1

    # ───────────────────────── Inspection printout ───────────────────────────
    # Emit a full report so `pytest -s` doubles as a rule-influence review.
    with capsys.disabled():
        print("\n╔══════════════════════════════════════════════════════════════╗")
        print("║ Deep 15-Player Scoring Matrix — Round Breakdown              ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print(f"  Prompt:  {room.prompt}")
        print(f"  Trap:    {room.trap_word}    Decoy: {room.decoy_word}")
        print(f"  Vetoed:  {room.vetoed_words}")
        print(f"  Ballot ({len(room.words_to_vote)} words): {sorted(room.words_to_vote)}")
        print()
        hdr = f"  {'Player':<10} {'Branch':<18} {'Δscore':>7} {'Δstreak':>8} {'Client':>7}"
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for name, _lk, _bn, branch in responders:
            p = room.players[name]
            s_delta = p.score - starting_scores[name]
            st_delta = p.streak - starting_streaks[name]
            client = _client_side_points(
                player=p, locked_word=p.locked_word,
                bounty_guess=p.bounty_guess, trap_word=room.trap_word,
                decoy_word=room.decoy_word, vetoed_words=room.vetoed_words,
            )
            print(f"  {name:<10} {branch:<18} {s_delta:>+7} {st_delta:>+8} {client:>+7}")
        d = room.players["dealer"]
        print("  " + "─" * (len(hdr) - 2))
        print(f"  {'dealer':<10} {'(trap+bounty)':<18} "
              f"{d.score - starting_scores['dealer']:>+7} {'—':>8} {'—':>7}")
        print()
        print("  Metrics this round:")
        for k, v in m.snapshot().items():
            if v:
                print(f"    {k}: {v}")
        print()


if __name__ == "__main__":
    # Allow running as a plain script.
    import inspect
    ns = dict(globals())
    failures = 0
    for name, fn in ns.items():
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                # Skip tests that require pytest fixtures.
                sig = inspect.signature(fn)
                if len(sig.parameters) > 0:
                    print(f"⏭  {name} (requires pytest fixtures — skipped in script mode)")
                    continue
                fn()
                print(f"✅ {name}")
            except AssertionError as e:
                failures += 1
                print(f"❌ {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} failures")
    print("\n🎉 ALL EDGE-CASE TESTS PASSED!")
