import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from Models.game_state import Room, Player
from logic import round_manager


class DummyWS:
    async def send_json(self, data):
        return None


def test_scoring():
    # 1. Setup a Mock Room
    room = Room(code="TEST", host="DealerDan")
    room.prompt = "A red fruit"
    room.trap_word = "apple"
    room.decoy_word = "tomato"
    room.ruleset = "classic"
    # Phase must be set to tribunal so resolve_round's idempotency guard
    # (which bails out if phase == "reveal") does not fire.
    room.phase = "tribunal"

    # 2. Setup Players with DummyWS (Player.__init__ requires a ws object)
    players = {
        "DealerDan":     Player("DealerDan",     DummyWS()),
        "SafeSam":       Player("SafeSam",        DummyWS()),  # Survives, no bounty
        "BountyBob":     Player("BountyBob",      DummyWS()),  # Survives + bounty
        "TrapTom":       Player("TrapTom",        DummyWS()),  # Hits trap word
        "VetoVic":       Player("VetoVic",        DummyWS()),  # Gets vetoed
        "DecoyDan":      Player("DecoyDan",       DummyWS()),  # Voted for the decoy
        "MindReaderMax": Player("MindReaderMax",  DummyWS()),  # Safe word IS the decoy
    }
    room.players = players

    room.current_dealer = "DealerDan"
    players["DealerDan"].is_dealer = True

    # 3. Simulate locked words & bounty guesses
    players["SafeSam"].locked_word = "cherry"

    players["BountyBob"].locked_word = "strawberry"
    players["BountyBob"].bounty_guess = "apple"   # Correct bounty, survives → counts

    players["TrapTom"].locked_word = "apple"      # Trapped — goes in zero_point_players
    players["TrapTom"].bounty_guess = "apple"     # Correct guess but trapped → blocked by can_score_bounty

    players["VetoVic"].locked_word = "brick"      # Will be vetoed by tribunal
    players["DecoyDan"].locked_word = "raspberry" # Survives tribunal
    players["MindReaderMax"].locked_word = "tomato"  # Matches decoy → Mind Reader immunity

    # 4. Simulate Tribunal Votes.
    #
    # Player count = 7 (including dealer). Classic threshold = total_voters / 2 = 3.5
    # A word needs STRICTLY MORE than 3.5 votes (i.e. >= 4) to be eliminated.
    #
    # "brick":  4 votes → eliminated (VetoVic is vetoed)
    # "tomato": 2 votes → NOT eliminated (MindReaderMax's word survives the tribunal)
    #           DecoyDan and VetoVic both voted for the decoy "tomato".
    #           - VetoVic already has locked_word="brick" which gets vetoed →
    #             the vetoed branch fires first, so honeypot is irrelevant for them.
    #           - DecoyDan locked a safe word ("raspberry") that survives, so
    #             their honeypot vote costs them the survival bonus (score 0).
    room.veto_votes = {
        "brick":  ["SafeSam", "BountyBob", "TrapTom", "DecoyDan"],
        "tomato": ["DecoyDan", "VetoVic"],
    }

    # 5. Run the scoring engine
    round_manager.resolve_round(room)

    # 6. Verify results
    print("--- SCORING RESULTS ---")

    # SafeSam: locked "cherry", not trapped, not vetoed → +1 survive
    assert players["SafeSam"].score == 1, \
        f"SafeSam expected 1, got {players['SafeSam'].score}"
    print("✅ SafeSam: Survived (+1) = 1")

    # BountyBob: survived (+1) + correct bounty (+1) = 2
    assert players["BountyBob"].score == 2, \
        f"BountyBob expected 2, got {players['BountyBob'].score}"
    print("✅ BountyBob: Survived (+1) + Bounty (+1) = 2")

    # TrapTom: hit trap word → zero_point_players; bounty blocked by can_score_bounty = 0
    assert players["TrapTom"].score == 0, \
        f"TrapTom expected 0, got {players['TrapTom'].score}"
    print("✅ TrapTom: Trapped (0), bounty denied = 0")

    # VetoVic: word "brick" is vetoed → zero_point_players, no survival point = 0
    assert players["VetoVic"].score == 0, \
        f"VetoVic expected 0, got {players['VetoVic'].score}"
    print("✅ VetoVic: Vetoed = 0")

    # DecoyDan: voted for decoy "tomato" → caught_in_honeypot = True.
    # Their locked word "raspberry" survives the tribunal, so under the OLD
    # clamp logic they'd get max(0, 0-1) + 1 = 1 (the honeypot did nothing).
    # Under the corrected logic, the honeypot flag forfeits the survival
    # bonus → 0 points for the round.
    assert players["DecoyDan"].score == 0, \
        f"DecoyDan expected 0 (honeypot blocks survival), got {players['DecoyDan'].score}"
    assert players["DecoyDan"].caught_in_honeypot == True
    print("✅ DecoyDan: Honeypot forfeits survival bonus = 0")

    # MindReaderMax: "tomato" matches decoy → Mind Reader (+2 pts + immunity),
    # mind_reader flag set. Mind Reader is the highest-skill outcome in the
    # game (divined the dealer's decoy) and pays double the survival bonus.
    assert players["MindReaderMax"].score == 2, \
        f"MindReaderMax expected 2, got {players['MindReaderMax'].score}"
    assert players["MindReaderMax"].mind_reader == True
    print("✅ MindReaderMax: Mind Reader (+2) = 2")

    # DealerDan: TrapTom hit the trap (+1), BountyBob farmed bounty (+1) = 2
    assert players["DealerDan"].score == 2, \
        f"DealerDan expected 2, got {players['DealerDan'].score}"
    print("✅ DealerDan: 1 Trapped (+1) + 1 Bounty Farmed (+1) = 2")

    print("\n🎉 ALL TESTS PASSED!")


if __name__ == "__main__":
    test_scoring()
