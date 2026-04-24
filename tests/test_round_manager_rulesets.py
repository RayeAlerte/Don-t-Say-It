import sys,os

# Go up one level from the 'test' folder and add the root to sys.path
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
    room.phase = "response_phase"
    room.prompt = "Name a color in the rainbow."
    room.trap_word = "red"
    room.decoy_word = "blue"
    room.current_dealer = room.host
    room.players[room.host].is_dealer = True
    room.locked_words = {
        "green": "p2",
        "yellow": "p3",
        "purple": "p4",
    }
    room.players["p2"].locked_word = "green"
    room.players["p3"].locked_word = "yellow"
    room.players["p4"].locked_word = "purple"
    round_manager.advance_to_tribunal(room)
    room.veto_votes = votes
    round_manager.resolve_round(room)


def test_competitive_threshold_and_max_removals():
    room = make_room(["p1", "p2", "p3", "p4", "p5"], host="p1")
    room.ruleset = "competitive"
    votes = {
        "green": ["p1", "p2", "p3"],   # 3 votes
        "yellow": ["p2", "p4", "p5"],  # 3 votes
        "purple": ["p1", "p2"],        # 2 votes
    }

    force_reveal_with_votes(room, votes)

    assert len(room.vetoed_words) == 2
    assert "green" in room.vetoed_words
    assert "yellow" in room.vetoed_words
    assert "purple" not in room.vetoed_words


def test_classic_majority_behavior_unchanged():
    room = make_room(["p1", "p2", "p3", "p4"], host="p1")
    room.ruleset = "classic"
    votes = {
        "green": ["p1", "p2"],       # 2 / 4 => not > 50%
        "yellow": ["p1", "p2", "p3"] # 3 / 4 => eliminated
    }

    force_reveal_with_votes(room, votes)

    assert "green" not in room.vetoed_words
    assert "yellow" in room.vetoed_words
