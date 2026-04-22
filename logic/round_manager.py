from Models.game_state import Room
import random

def start_game(room: Room):
    room.phase = "trap_phase"
    # NEW: 1 Round per player, randomized order
    room.round_limit = len(room.players)
    room.dealer_queue = list(room.players.keys())
    random.shuffle(room.dealer_queue)
    
    room.current_dealer = room.dealer_queue.pop(0)
    room.players[room.current_dealer].is_dealer = True

def advance_to_response_phase(room: Room, prompt: str, trap_word: str):
    room.prompt = prompt
    room.trap_word = trap_word.strip().lower()
    room.phase = "response_phase"

def resolve_round(room: Room):
    room.phase = "reveal"
    dealer = room.players[room.current_dealer]
    
    # 1. Handle Timeouts
    for p in room.players.values():
        if not p.is_dealer and p.locked_word is None:
            p.locked_word = "cornball"
            p.timed_out = True # Flag them as a timeout
            room.locked_words["cornball"] = p.name

    # 2. Scoring
    for p in room.players.values():
        if p.is_dealer:
            continue
            
        # Safe Word Scoring
        if p.locked_word == room.trap_word:
            dealer.score += 1 # Dealer caught them!
        elif not p.timed_out:
            p.score += 1 # They survived AND they didn't time out!
            
        # Bounty Scoring
        if p.bounty_guess == room.trap_word:
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
    room.locked_words.clear()
    
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
    
    # NEW: Recalculate and reshuffle for the new game
    room.round_limit = len(room.players)
    room.dealer_queue = list(room.players.keys())
    random.shuffle(room.dealer_queue)
    
    for p in room.players.values():
        p.reset_for_round()
        p.score = 0
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
        p.is_dealer = False