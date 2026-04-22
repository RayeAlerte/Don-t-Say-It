from typing import Dict, List, Optional
from fastapi import WebSocket
import time

class Player:
    def __init__(self, name: str, ws: WebSocket, role: str = "active"):
        self.name = name
        self.ws = ws
        self.connected: bool = True
        self.latency_ms: float = 0.0
        self.score: int = 0
        self.streak: int = 0 
        self.role: str = role 
        self.vote_accuracy_hits: int = 0
        self.vote_accuracy_total: int = 0
        
        self.locked_word: Optional[str] = None
        self.bounty_guess: Optional[str] = None
        self.is_dealer: bool = False
        self.timed_out: bool = False 
        
        # NEW: Round specific UI flags
        self.caught_in_honeypot: bool = False
        self.mind_reader: bool = False

    def reset_for_round(self):
        self.locked_word = None
        self.bounty_guess = None
        self.timed_out = False
        self.caught_in_honeypot = False
        self.mind_reader = False

class Room:
    def __init__(self, code: str, host: str):
        self.code = code
        self.host = host
        self.last_activity = time.time()
        
        self.players: Dict[str, Player] = {}
        self.banned_names: List[str] = [] 
        self.dealer_queue: List[str] = []
        self.current_dealer: Optional[str] = None
        
        self.round_limit = 1 
        self.current_round = 1
        self.phase = "lobby" 
        self.phase_deadline: float = 0 
        
        self.prompt: str = ""
        self.trap_word: str = ""
        self.decoy_word: str = "" # NEW: The Honeypot trap
        
        self.locked_words: Dict[str, str] = {} 
        self.lock_times: Dict[str, float] = {}
        self.words_to_vote: List[str] = [] # NEW: Shuffled list for the Tribunal
        self.veto_votes: Dict[str, List[str]] = {} # NEW: Tracks who voted for what
        self.vetoed_words: List[str] = [] # NEW: Words that officially died
        self.vote_accuracy_round: Dict[str, Dict[str, float]] = {}

    def get_active_players(self):
        return {k: v for k, v in self.players.items() if v.role == "active"}

    def all_responders_locked(self) -> bool:
        responders = [p for p in self.get_active_players().values() if not p.is_dealer]
        if not responders: return False
        return all(p.locked_word is not None for p in responders)