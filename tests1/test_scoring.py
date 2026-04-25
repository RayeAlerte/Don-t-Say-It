import sys,os

# Go up one level from the 'test' folder and add the root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from Models.game_state import Room, Player
from logic import round_manager

def test_scoring():
    # 1. Setup a Mock Room
    room = Room(code="TEST", host="DealerDan")
    room.prompt = "A red fruit"
    room.trap_word = "apple"
    room.decoy_word = "tomato"
    room.ruleset = "classic"

    # 2. Setup Players
    players = {
        "DealerDan": Player("DealerDan", None),
        "SafeSam": Player("SafeSam", None),       # Survives, misses bounty
        "BountyBob": Player("BountyBob", None),   # Survives, hits bounty
        "TrapTom": Player("TrapTom", None),       # Hits trap
        "VetoVic": Player("VetoVic", None),       # Gets vetoed
        "DecoyDan": Player("DecoyDan", None),     # Voted for the decoy (Honeypot)
        "MindReaderMax": Player("MindReaderMax", None) # Safe word IS the decoy
    }
    room.players = players
    
    room.current_dealer = "DealerDan"
    players["DealerDan"].is_dealer = True

    # 3. Simulate Game Actions
    players["SafeSam"].locked_word = "cherry"
    
    players["BountyBob"].locked_word = "strawberry"
    players["BountyBob"].bounty_guess = "apple" # Correct bounty
    
    players["TrapTom"].locked_word = "apple" # Trapped!
    players["TrapTom"].bounty_guess = "apple" # Correct bounty (should NOT count because he died)

    players["VetoVic"].locked_word = "brick" # Going to be vetoed
    
    players["DecoyDan"].locked_word = "raspberry"
    
    players["MindReaderMax"].locked_word = "tomato" # Secret Mind Reader

    # 4. Simulate Tribunal Votes
    room.veto_votes = {
        "brick": ["SafeSam", "BountyBob", "TrapTom", "DecoyDan"], # Vic gets eliminated
        "tomato": ["DecoyDan", "VetoVic"] # DecoyDan fell for the Honeypot trap!
    }

    # 5. Run the Engine
    round_manager.resolve_round(room)

    # 6. Verify Results
    print("--- SCORING RESULTS ---")
    assert players["SafeSam"].score == 1, f"Expected 1, got {players['SafeSam'].score}"
    print("✅ SafeSam: Survived (+1)")
    
    assert players["BountyBob"].score == 2, f"Expected 2, got {players['BountyBob'].score}"
    print("✅ BountyBob: Survived (+1) & Bounty (+1) = 2")

    assert players["TrapTom"].score == 0, f"Expected 0, got {players['TrapTom'].score}"
    print("✅ TrapTom: Trapped (0) & Denied Bounty = 0")

    assert players["VetoVic"].score == -1, f"Expected 0, got {players['VetoVic'].score}"
    print("✅ VetoVic: Vetoed (0)")

    assert players["DecoyDan"].score == 0, f"Expected 0, got {players['DecoyDan'].score}"
    print("✅ DecoyDan: Survived (+1) but Honeypot Penalty (-1) = 0")

    assert players["MindReaderMax"].score == 1, f"Expected 1, got {players['MindReaderMax'].score}"
    assert players["MindReaderMax"].mind_reader == True
    print("✅ MindReaderMax: Mind Reader Immunity (+1)")

    assert players["DealerDan"].score == 2, f"Expected 2, got {players['DealerDan'].score}"
    print("✅ DealerDan: 1 Trapped (+1) & 1 Bounty Farmed (+1) = 2")
    
    print("\n🎉 ALL TESTS PASSED! The round_manager math is bulletproof.")

if __name__ == "__main__":
    test_scoring()