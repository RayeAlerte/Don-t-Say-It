from Models.game_state import Room
import random
import re
import os

# --- Load Prompts from TXT ---
PROMPTS_DB = []

def load_prompts():
    global PROMPTS_DB
    PROMPTS_DB = []
    try:
        with open("Prompts.txt", "r") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            for i in range(0, len(lines), 2):
                prompt = lines[i]
                traps = [t.strip() for t in lines[i+1].split(",")]
                PROMPTS_DB.append({"prompt": prompt, "traps": traps})
    except Exception as e:
        print("Could not load Prompts.txt. Using fallback.")
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
    pool = set(room.locked_words.keys())
    
    # Inject Decoy (Only if it doesn't already perfectly match a safe word)
    if room.decoy_word:
        is_duplicate = any(is_match(room.decoy_word, w) for w in pool)
        if not is_duplicate:
            pool.add(room.decoy_word)
            
    room.words_to_vote = list(pool)
    random.shuffle(room.words_to_vote)

def resolve_round(room: Room):
    room.phase = "reveal"
    dealer = room.players[room.current_dealer]
    
    # 1. Tally Tribunal Votes (>50% Rule)
    total_voters = len(room.get_active_players()) + 1 # Active players + Dealer
    threshold = total_voters / 2
    
    for word, voters in room.veto_votes.items():
        if len(voters) > threshold:
            room.vetoed_words.append(word)

    # 2. Spring the Honeypot Penalty
    if room.decoy_word and any(is_match(room.decoy_word, w) for w in room.vetoed_words):
        # Find which exact string they voted out
        target_decoy = next(w for w in room.vetoed_words if is_match(room.decoy_word, w))
        for voter_name in room.veto_votes.get(target_decoy, []):
            if voter_name in room.players:
                room.players[voter_name].score -= 1
                room.players[voter_name].caught_in_honeypot = True

    # 3. Standard Scoring
    for p in room.players.values():
        if p.is_dealer: continue
            
        if p.role == "active":
            is_mind_reader = room.decoy_word and is_match(p.locked_word, room.decoy_word)
            is_vetoed = any(is_match(p.locked_word, w) for w in room.vetoed_words)
            
            if is_match(p.locked_word, room.trap_word):
                dealer.score += 1 
                p.streak = 0 
            elif is_mind_reader:
                # The Alliance! They survive even if vetoed
                p.score += 1
                p.streak += 1
                p.mind_reader = True
            elif is_vetoed:
                p.streak = 0 # 0 points
            elif not p.timed_out:
                p.score += 1 
                p.streak += 1 
            
        # Bounties
        if p.bounty_guess and is_match(p.bounty_guess, room.trap_word):
            p.score += 1 
            dealer.score += 1 

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
    room.words_to_vote.clear()
    room.veto_votes.clear()
    room.vetoed_words.clear()
    
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
    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
        p.streak = 0
        p.is_dealer = False