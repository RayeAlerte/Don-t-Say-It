from pydantic import BaseModel, Field
from typing import Optional

class ActionPayload(BaseModel):
    action: str = Field(..., description="The event type: join, lock_trap, lock_word, bounty_guess")
    prompt: Optional[str] = None
    word: Optional[str] = None
    decoy: Optional[str] = None
    ruleset: Optional[str] = None
    dealer_time: Optional[int] = None
    response_time: Optional[int] = None
    tribunal_time: Optional[int] = None
    reveal_time: Optional[int] = None
    latency_ms: Optional[float] = None
    client_ts: Optional[float] = None

class ErrorPayload(BaseModel):
    status: str = "error"
    message: str

class SuccessPayload(BaseModel):
    status: str = "success"
    message: str