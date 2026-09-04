from Models.game_state import Room
import random
import re
import os
import math

DEALER_BOUNTY_CAP = 2
COMP_MIN_VOTE_SHARE = 0.35
COMP_MIN_VOTES = 2
COMP_MAX_REMOVALS = 2

# Input validation limits (also enforced client-side in app.js — keep in sync).
MAX_WORD_LEN = 25
MAX_NAME_LEN = 20
MAX_PROMPT_LEN = 200

# Game words allow alphanumeric + single spaces (e.g. "burger king").
# Player names allow the same set — the client filters via enforceInputRule.
_GAME_WORD_RE = re.compile(r'[^a-zA-Z0-9 ]')
_PLAYER_NAME_RE = re.compile(r'[^a-zA-Z0-9 _\-]')


def sanitize_game_word(raw: str, max_len: int = MAX_WORD_LEN):
    """Strip non-safe chars from a game word.

    Returns (clean_word, blocked_chars_count). `blocked_chars_count`
    is the number of characters that were removed by sanitization — the
    caller uses this to increment `metrics.xss_word_blocks_total`.
    """
    if raw is None:
        return "", 0
    stripped = _GAME_WORD_RE.sub("", raw)
    blocked = len(raw) - len(stripped)
    # Collapse runs of whitespace, trim ends, then hard-truncate.
    cleaned = re.sub(r'\s+', ' ', stripped).strip()[:max_len]
    return cleaned, blocked


def sanitize_player_name(raw: str, max_len: int = MAX_NAME_LEN):
    """Strip non-safe chars from a player name.

    Returns (clean_name, blocked_chars_count).
    """
    if raw is None:
        return "", 0
    stripped = _PLAYER_NAME_RE.sub("", raw)
    blocked = len(raw) - len(stripped)
    cleaned = re.sub(r'\s+', ' ', stripped).strip()[:max_len]
    return cleaned, blocked


def sanitize_prompt(raw: str, max_len: int = MAX_PROMPT_LEN) -> str:
    """Prompts allow punctuation (client renders them via innerText, so
    XSS-safe). We just enforce a length cap.
    """
    if not raw:
        return ""
    return raw.strip()[:max_len]


def can_cast_tribunal_vote(player) -> bool:
    """Central gate: dealers see the trap and decoy, so they must not vote."""
    return (
        player is not None
        and player.role == "active"
        and not player.is_dealer
    )


PING_MIN_INTERVAL_S = 0.5  # 2 Hz cap — plenty for a 3-second client heartbeat


def should_rate_limit_ping(player, now_ts: float) -> bool:
    """Returns True if this ping should be dropped.

    A player's very first ping (last_ping_ts still at the None default) is
    always allowed. The sentinel is None rather than 0.0 so tests using small
    timestamps (e.g. t=0.0, 0.1) still trigger the rate limit correctly.
    """
    if player is None:
        return False
    if player.last_ping_ts is not None and now_ts - player.last_ping_ts < PING_MIN_INTERVAL_S:
        return True
    player.last_ping_ts = now_ts
    return False

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
    
    # Inject Decoy (Only if it doesn't already perfectly match a safe word).
    # If the decoy fuzzy-matches an existing safe word we DO NOT add it — the
    # matching safe word effectively becomes the ballot's honeypot. This is
    # intentional (players who locked it get Mind Reader, voters against it
    # get -1), but we track it so hosts can see how often it happens in
    # simulator output.
    if room.decoy_word:
        is_duplicate = any(is_match(room.decoy_word, w) for w in pool)
        if not is_duplicate:
            pool.append(room.decoy_word)
        else:
            room.metrics.decoy_hidden_in_pool_total += 1

    room.words_to_vote = pool
    random.shuffle(room.words_to_vote)

def resolve_round(room: Room):
    """Resolve a tribunal into per-player score deltas for one round.

    ─────────────────────────────────────────────────────────────────────────
    CANONICAL SCORING MODEL
    ─────────────────────────────────────────────────────────────────────────
    This docstring is the source of truth. The client (static/app.js reveal
    card) mirrors this priority chain; tests exercise it via a Python-side
    mirror in tests/test_edge_cases.py::_client_side_points. Any change here
    must move all three in lockstep.

    Non-dealer active player priority chain — exactly ONE branch runs per
    player per round:

        # | Branch              | Δ score | Streak     | Zeroed?
        1 | AFK / timed_out     |    0    | reset to 0 | yes
        2 | Trapped             |    0    | reset to 0 | yes  (dealer +1)
        3 | 🧠 Mind Reader      |   +2    | +1         | no
        4 | Vetoed              |    0    | reset to 0 | yes
        5 | 🍯 Honeypot         |    0    | preserved  | no
        6 | Survived (default)  |   +1    | +1         | no

    Independent additive pass (after the priority chain):
      • Bounty: +1 to guesser AND +1 to dealer (dealer cap = 2/round).
        Gated by `zero_point_players` — honeypot voters CAN still bounty.

    Point control features (no branch produces a negative delta):
      • DEALER_BOUNTY_CAP (=2): dealer earns at most 2 bounty points/round.
      • Honeypot is a scoring FLAG, not a score subtraction — voters forfeit
        the survival bonus rather than losing prior-round points. (History:
        the old max(0, s-1) clamp silently disabled the penalty at score 0.)
      • Idempotency: if phase is already "reveal", this function is a no-op.
      • Dealer missing at resolve (purged mid-round) is safe — trap/bounty
        credits to the dealer are skipped and room.metrics logs the event.

    Metrics fired per branch (see logic/metrics.py):
      afk_events_total, trap_hit_events_total, mind_reader_events_total,
      veto_events_total, honeypot_events_total, survive_events_total,
      bounty_events_total, dealer_bounty_cap_hits_total.
    ─────────────────────────────────────────────────────────────────────────
    """
    if room.phase == "reveal":   # Already resolved, bail out
        return
    room.phase = "reveal"
    # Dealer may have disconnected + been purged before the round resolved.
    # Score everyone else normally, but don't credit a missing dealer.
    dealer = room.players.get(room.current_dealer)
    if dealer is None:
        room.metrics.dealer_missing_at_resolve += 1
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

    # 2. Flag honeypot voters (does NOT touch score directly).
    #
    # HISTORY: the original design subtracted 1 point here and then survival
    # scoring added +1 back — so a player at score 0 who voted honeypot would
    # net +1 (max(0, -1) + 1 = 1). That silently disabled the honeypot for
    # every player at 0 points, i.e. most of the lobby early-game. The client's
    # reveal card also lied about it, showing "SURVIVED +1 / HONEYPOT -1 = 0"
    # while the server credited +1.
    #
    # The correct model: honeypot voters forfeit the SURVIVAL bonus itself.
    # We flag them here, and the standard-scoring block checks the flag and
    # skips the +1. Bounty is intentionally NOT blocked — guessing the trap
    # word is a separate skill from voting in the tribunal.
    if room.decoy_word:
        for word, voters in room.veto_votes.items():
            if is_match(room.decoy_word, word):
                for voter_name in voters:
                    if voter_name in room.players:
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
                # Failed to lock a Safe Word in time. Streak resets (this is
                # the harshest "failure to engage" outcome). See scoring
                # docstring at the top of resolve_round.
                p.streak = 0
                zero_point_players.add(p.name)
                room.metrics.afk_events_total += 1
            elif is_match(p.locked_word, room.trap_word):
                if dealer is not None:
                    dealer.score += 1
                p.streak = 0
                zero_point_players.add(p.name)
                room.metrics.trap_hit_events_total += 1
            elif is_mind_reader:
                # Mind reader beats honeypot: if you LOCKED the decoy word,
                # you earn +2 (highest non-bounty outcome in the game) plus
                # immunity from any veto — even if you also voted for the
                # decoy yourself.
                p.score += 2
                p.streak += 1
                p.mind_reader = True
                room.metrics.mind_reader_events_total += 1
            elif is_vetoed:
                p.streak = 0
                zero_point_players.add(p.name)
                room.metrics.veto_events_total += 1
            elif p.caught_in_honeypot:
                # Fell for the honeypot. No survival bonus. Streak preserved
                # (their locked word survived the tribunal). Bounty stays
                # available — see comment above the flagging pass.
                room.metrics.honeypot_events_total += 1
            else:
                p.score += 1
                p.streak += 1
                room.metrics.survive_events_total += 1
        
    # Bounties
    for p in room.players.values():
        if p.is_dealer: continue
        can_score_bounty = (
            p.role == "audience" or
            (p.role == "active" and p.name not in zero_point_players)
        )
        if can_score_bounty and p.bounty_guess and is_match(p.bounty_guess, room.trap_word):
            p.score += 1
            room.metrics.bounty_events_total += 1
            if dealer is not None:
                if dealer_bounty_awards < DEALER_BOUNTY_CAP:
                    dealer.score += 1
                    dealer_bounty_awards += 1
                else:
                    # Dealer would have earned +1 but the per-round cap
                    # prevented it. Useful signal for tuning DEALER_BOUNTY_CAP.
                    room.metrics.dealer_bounty_cap_hits_total += 1

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

    # Return the finished dealer to the queue *only* if they're still here.
    # A dealer purged mid-round would otherwise pollute the queue with a
    # dead name we'd hit again on wrap-around.
    if room.current_dealer and room.current_dealer in room.players:
        room.dealer_queue.append(room.current_dealer)

    # Skip past any queued dealers who have left the room since they were
    # queued. Every skip is a purge-recovery event.
    skipped = 0
    while room.dealer_queue and room.dealer_queue[0] not in room.players:
        room.dealer_queue.pop(0)
        skipped += 1
    if skipped:
        room.metrics.dealer_purge_recoveries += skipped

    if not room.dealer_queue:
        # No one left to deal — end the game gracefully instead of KeyError.
        room.phase = "game_over"
        room.current_dealer = ""
        return

    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True

def _clear_round_state(room: Room) -> None:
    """Nuke every field advance_to_tribunal/resolve_round writes to."""
    room.prompt = ""
    room.trap_word = ""
    room.decoy_word = ""
    room.locked_words.clear()
    room.lock_times.clear()
    room.words_to_vote.clear()
    room.veto_votes.clear()
    room.vetoed_words.clear()
    room.vote_accuracy_round = {}


def play_again(room: Room):
    room.current_round = 1
    room.phase = "trap_phase"
    _clear_round_state(room)

    active_players = room.get_active_players()
    room.round_limit = len(active_players)
    room.dealer_queue = list(active_players.keys())
    random.shuffle(room.dealer_queue)

    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
        p.streak = 0
        p.is_dealer = False
        p.vote_accuracy_hits = 0
        p.vote_accuracy_total = 0

    if not room.dealer_queue:
        room.phase = "game_over"
        room.current_dealer = ""
        return

    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True


def return_to_lobby(room: Room):
    room.current_round = 1
    room.phase = "lobby"
    _clear_round_state(room)
    room.dealer_queue = []
    room.current_dealer = ""
    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
        p.streak = 0
        p.is_dealer = False
        p.vote_accuracy_hits = 0
        p.vote_accuracy_total = 0