"""Per-room counters for edge-case handling.

Each Room owns one RoomMetrics instance. Counters increment whenever the
server takes a defensive action (blocks XSS, promotes a late joiner to
audience, skips a purged dealer, etc.) so we can measure how often each
guardrail actually fires in real lobbies.

The simulator (`tools/simulate_lobbies.py`) aggregates these across lobbies
for a fleet-wide view, and the test suite asserts on them directly to prove
each guardrail wires all the way through.
"""

from dataclasses import dataclass, asdict


@dataclass
class RoomMetrics:
    # Input sanitization
    xss_word_blocks_total: int = 0        # game-word chars stripped
    xss_name_blocks_total: int = 0        # player-name chars stripped

    # Lobby / lifecycle
    mid_game_audience_promotions: int = 0 # late joiners forced to audience
    same_name_socket_replacements: int = 0
    host_reassignments_total: int = 0
    host_reassignments_to_disconnected: int = 0  # subset with no connected fallback

    # Round mechanics
    dealer_purge_recoveries: int = 0      # dealer_queue entries skipped
    dealer_missing_at_resolve: int = 0    # resolve_round ran without a dealer
    decoy_hidden_in_pool_total: int = 0   # decoy deduped into an existing safe word
    dealer_vote_rejections: int = 0       # dealer tried to vote in tribunal

    # Networking
    ping_rate_limited_total: int = 0

    # Scoring event distribution — one increment per non-dealer active player
    # per round, exactly one of these branches fires. `dealer_bounty_cap_hits`
    # counts bounty guesses that would have paid the dealer but were denied
    # by DEALER_BOUNTY_CAP.
    afk_events_total: int = 0
    trap_hit_events_total: int = 0
    mind_reader_events_total: int = 0
    veto_events_total: int = 0
    honeypot_events_total: int = 0
    survive_events_total: int = 0
    bounty_events_total: int = 0
    dealer_bounty_cap_hits_total: int = 0

    def snapshot(self) -> dict:
        return asdict(self)

    def merge(self, other: "RoomMetrics") -> None:
        """In-place additive merge, used by the simulator."""
        for key, value in asdict(other).items():
            setattr(self, key, getattr(self, key) + value)
