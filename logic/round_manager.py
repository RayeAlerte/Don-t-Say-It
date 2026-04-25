from Models.game_state import Room
import random
import re
import os
import math

DEALER_BOUNTY_CAP = 2
COMP_MIN_VOTE_SHARE = 0.35
COMP_MIN_VOTES = 2
COMP_MAX_REMOVALS = 2

# --- Load Prompts from TXT ---
PROMPTS_DB = []

def load_prompts():
    global PROMPTS_DB
    PROMPTS_DB = []
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_dir = os.path.dirname(base_dir)
        prompt_candidates = [
            os.path.join(project_dir, "prompts.txt"),
            os.path.join(project_dir, "Prompts.txt")
        ]
        prompt_path = next((path for path in prompt_candidates if os.path.exists(path)), prompt_candidates[0])
        
        with open(prompt_path, "r") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            for i in range(0, len(lines), 2):
                prompt = lines[i]
                traps = [t.strip() for t in lines[i+1].split(",")]
                PROMPTS_DB.append({"prompt": prompt, "traps": traps})
    except Exception as e:
        print("Could not load prompts.txt. Using fallback.")
        PROMPTS_DB = [{"prompt": "Name a fast food chain.", "traps": ["mcdonalds", "wendys"]}]

load_prompts()

def get_random_prompt():
    if not PROMPTS_DB:
        load_prompts()
    p = random.choice(PROMPTS_DB)
    return p["prompt"], random.choice(p["traps"])

# --- Text Sanitization & Fuzzy Matching ---
def squash_word(word: str) -> str:
    if not word: return ""
    return re.sub(r'[^a-z0-9]', '', word.lower())

def levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2): return levenshtein(s2, s1)
    if len(s2) == 0: return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

# NOTE: This is the AUTHORITATIVE matching function.
# A mirror exists in app.js for client-side optimistic UI.
# Keep both in sync when changing thresholds.
def is_match(word1: str, word2: str) -> bool:
    w1, w2 = squash_word(word1), squash_word(word2)
    if not w1 or not w2: return False
    if w1 == w2: return True
    dist = levenshtein(w1, w2)
    length = min(len(w1), len(w2))
    if length <= 4 and dist == 0: return True
    if 5 <= length <= 8 and dist <= 1: return True
    if length >= 9 and dist <= 2: return True
    return False

# --- Core Logic ---
def start_game(room: Room):
    room.phase = "trap_phase"
    active_players = room.get_active_players()
    room.round_limit = len(active_players)
    room.dealer_queue = list(active_players.keys())
    random.shuffle(room.dealer_queue)
    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True

def advance_to_response_phase(room: Room, prompt: str, trap_word: str, decoy_word: str = ""):
    room.prompt = prompt
    room.trap_word = trap_word.strip()
    room.decoy_word = decoy_word.strip()
    room.phase = "response_phase"

def advance_to_tribunal(room: Room):
    room.phase = "tribunal"
    room.veto_votes.clear()
    room.vetoed_words.clear()
    
    # 1. Flag Cornballs
    for p in room.get_active_players().values():
        if not p.is_dealer and p.locked_word is None:
            p.locked_word = "cornball"
            p.timed_out = True
            room.locked_words["cornball"] = p.name

    # --- NEW: Automatic Honeypot Injection ---
    if not room.decoy_word:
        # Try to find the current prompt in our database
        db_entry = next((item for item in PROMPTS_DB if squash_word(item["prompt"]) == squash_word(room.prompt)), None)
        if db_entry:
            # Get all traps that are NOT the dealer's actual trap word
            possible_decoys = [t for t in db_entry["traps"] if not is_match(t, room.trap_word)]
            if possible_decoys:
                room.decoy_word = random.choice(possible_decoys)
    # -----------------------------------------

    # 2. Compile Voting List
    # Use fuzzy deduplication so multiple slightly different safe words consolidate into one
    pool = []
    for p in room.get_active_players().values():
        if not p.is_dealer and p.locked_word:
            if p.timed_out:
                continue  # Don't pollute the tribunal with "cornball"
            if is_match(p.locked_word, room.trap_word):
                continue
            if not any(is_match(p.locked_word, w) for w in pool):
                pool.append(p.locked_word)
    
    # Inject Decoy (Only if it doesn't already perfectly match a safe word)
    if room.decoy_word:
        is_duplicate = any(is_match(room.decoy_word, w) for w in pool)
        if not is_duplicate:
            pool.append(room.decoy_word)
            
    room.words_to_vote = pool
    random.shuffle(room.words_to_vote)

def resolve_round(room: Room):
    if room.phase == "reveal":   # Already resolved, bail out
        return
    room.phase = "reveal"
    dealer = room.players[room.current_dealer]
    zero_point_players = set()
    dealer_bounty_awards = 0
    room.vote_accuracy_round = {}
    
    # 1. Tally Tribunal Votes
    total_voters = len(room.get_active_players())
    if room.ruleset == "competitive":
        
        # --- NEW: Dynamic Rule of Thirds Cap ---
        dynamic_max_removals = max(2, math.ceil(total_voters * 0.3))
        
        threshold = max(COMP_MIN_VOTES, int((total_voters * COMP_MIN_VOTE_SHARE) + 0.9999))
        ranked_words = sorted(
            room.veto_votes.items(),
            key=lambda item: len(item[1]),
            reverse=True
        )
        for word, voters in ranked_words:
            # Swap out the static COMP_MAX_REMOVALS for our new dynamic cap
            if len(room.vetoed_words) >= dynamic_max_removals:
                break
            if len(voters) >= threshold:
                room.vetoed_words.append(word)
    else:
        threshold = total_voters / 2
        for word, voters in room.veto_votes.items():
            if len(voters) > threshold:
                room.vetoed_words.append(word)

    # 2. Spring the Honeypot Penalty (Individual Punishment)
    if room.decoy_word: # <-- This must be the only condition!
        for word, voters in room.veto_votes.items():
            if is_match(room.decoy_word, word):
                for voter_name in voters:
                    if voter_name in room.players:
                        room.players[voter_name].score = max(0, room.players[voter_name].score - 1)
                        room.players[voter_name].caught_in_honeypot = True
    
    # 2.5 Vote Accuracy Tracking
    for p in room.get_active_players().values():
        votes = [word for word, voters in room.veto_votes.items() if p.name in voters]
        accurate = 0
        for voted_word in votes:
            voted_decoy = room.decoy_word and is_match(voted_word, room.decoy_word)
            voted_vetoed = any(is_match(voted_word, w) for w in room.vetoed_words)
            if voted_vetoed and not voted_decoy:
                accurate += 1
        
        if votes:
            p.vote_accuracy_hits += accurate
            p.vote_accuracy_total += len(votes)
        
        room.vote_accuracy_round[p.name] = {
            "hits": accurate,
            "total": len(votes),
            "rate": (accurate / len(votes)) if votes else 0
        }

    # 3. Standard Scoring
    for p in room.players.values():
        if p.is_dealer: continue
        if p.role == "active":
            is_mind_reader = room.decoy_word and is_match(p.locked_word, room.decoy_word)
            is_vetoed = any(is_match(p.locked_word, w) for w in room.vetoed_words)
            
            if p.timed_out:
                zero_point_players.add(p.name)
            elif is_match(p.locked_word, room.trap_word):
                dealer.score += 1
                p.streak = 0
                zero_point_players.add(p.name)
            elif is_mind_reader:
                p.score += 1
                p.streak += 1
                p.mind_reader = True
            elif is_vetoed:
                p.streak = 0
                zero_point_players.add(p.name)
            else:
                p.score += 1
                p.streak += 1
        
    # Bounties
    for p in room.players.values():
        if p.is_dealer: continue
        can_score_bounty = (
            p.role == "audience" or
            (p.role == "active" and p.name not in zero_point_players)
        )
        if can_score_bounty and p.bounty_guess and is_match(p.bounty_guess, room.trap_word):
            p.score += 1
            if dealer_bounty_awards < DEALER_BOUNTY_CAP:
                dealer.score += 1
                dealer_bounty_awards += 1

def next_round(room: Room):
    if room.current_round >= room.round_limit:
        room.phase = "game_over"
        return

    room.current_round += 1
    room.phase = "trap_phase"
    room.prompt = ""
    room.trap_word = ""
    room.decoy_word = ""
    room.locked_words.clear()
    room.lock_times.clear()
    room.words_to_vote.clear()
    room.veto_votes.clear()
    room.vetoed_words.clear()
    room.vote_accuracy_round = {}
    
    for p in room.players.values():
        p.reset_for_round()
        p.is_dealer = False
        
    room.dealer_queue.append(room.current_dealer)
    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True

def play_again(room: Room):
    room.current_round = 1
    room.phase = "trap_phase"
    room.prompt = ""
    room.trap_word = ""
    room.locked_words.clear()
    room.lock_times.clear()
    room.vote_accuracy_round = {}
    
    active_players = room.get_active_players()
    room.round_limit = len(active_players)
    room.dealer_queue = list(active_players.keys())
    random.shuffle(room.dealer_queue)
    
    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
        p.streak = 0
        p.is_dealer = False
        
    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True

def return_to_lobby(room: Room):
    room.current_round = 1
    room.phase = "lobby"
    room.prompt = ""
    room.trap_word = ""
    room.locked_words.clear()
    room.lock_times.clear()
    room.vote_accuracy_round = {}
    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
        p.streak = 0
        p.is_dealer = False