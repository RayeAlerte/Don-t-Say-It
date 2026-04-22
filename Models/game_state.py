from typing import Dict, List, Optional
from fastapi import WebSocket
import time

class Player:
    def __init__(self, name: str, ws: WebSocket):
        self.name = name
        self.ws = ws
        self.score: int = 0
        self.locked_word: Optional[str] = None
        self.bounty_guess: Optional[str] = None
        self.is_dealer: bool = False
        self.timed_out: bool = False # NEW: Tracks if they failed to submit in time

    def reset_for_round(self):
        self.locked_word = None
        self.bounty_guess = None
        self.timed_out = False

class Room:
    def __init__(self, code: str, host: str):
        self.code = code
        self.host = host
        self.last_activity = time.time()
        
        self.players: Dict[str, Player] = {}
        self.dealer_queue: List[str] = []
        self.current_dealer: Optional[str] = None
        
        self.round_limit = 1 
        self.current_round = 1
        self.phase = "lobby" 
        self.phase_deadline: float = 0 
        
        self.prompt: str = ""
        self.trap_word: str = ""
        self.locked_words: Dict[str, str] = {} 

    def all_responders_locked(self) -> bool:
        responders = [p for p in self.players.values() if not p.is_dealer]
        return all(p.locked_word is not None for p in responders)