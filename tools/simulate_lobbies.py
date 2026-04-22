import argparse
import json
import random
import time
from typing import Dict, List
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Models.game_state import Room, Player
from logic import round_manager


class DummyWS:
    async def send_json(self, data):
        return None


PROMPT = "Name a color in the rainbow."
WORD_BANK = ["red", "blue", "green", "yellow", "purple", "orange", "indigo", "violet"]


def make_room(code: str, player_count: int, ruleset: str) -> Room:
    host = "p1"
    room = Room(code, host)
    room.ruleset = ruleset
    for i in range(1, player_count + 1):
        name = f"p{i}"
        room.players[name] = Player(name, DummyWS(), role="active")
    return room


def pick_word(exclude: List[str] = None) -> str:
    exclude = exclude or []
    choices = [w for w in WORD_BANK if w not in exclude]
    return random.choice(choices if choices else WORD_BANK)


def run_round(room: Room, stats: Dict):
    round_manager.start_game(room)
    dealer_name = room.current_dealer
    dealer = room.players[dealer_name]
    trap = pick_word()
    decoy = pick_word([trap])
    round_manager.advance_to_response_phase(room, PROMPT, trap, decoy)

    round_actions = {
        "safe_locks": 0,
        "bounty_locks": 0,
        "tribunal_votes": 0,
    }
    round_start = time.time()

    # Response behavior model.
    for p in room.players.values():
        if p.is_dealer:
            continue
        if random.random() < 0.1:
            continue  # timeout

        # 20% chance to accidentally hit trap.
        safe_word = trap if random.random() < 0.2 else pick_word()
        p.locked_word = safe_word
        room.locked_words[f"{safe_word}_{p.name}"] = p.name
        round_actions["safe_locks"] += 1

        # 70% chance to attempt bounty.
        if random.random() < 0.7:
            p.bounty_guess = trap if random.random() < 0.25 else pick_word()
            round_actions["bounty_locks"] += 1

    round_manager.advance_to_tribunal(room)

    # Tribunal voting behavior.
    for p in room.get_active_players().values():
        if p.is_dealer:
            continue
        if not room.words_to_vote:
            continue

        if room.ruleset == "competitive":
            voted_word = random.choice(room.words_to_vote)
            room.veto_votes.setdefault(voted_word, []).append(p.name)
            round_actions["tribunal_votes"] += 1
        else:
            vote_count = random.randint(0, min(2, len(room.words_to_vote)))
            for voted_word in random.sample(room.words_to_vote, k=vote_count):
                room.veto_votes.setdefault(voted_word, []).append(p.name)
                round_actions["tribunal_votes"] += 1

    round_manager.resolve_round(room)

    round_duration_ms = int((time.time() - round_start) * 1000)
    tribunal_eliminations = len(room.vetoed_words)
    honeypot_hits = sum(1 for p in room.players.values() if p.caught_in_honeypot)

    stats["performance"]["round_durations_ms"].append(round_duration_ms)
    stats["player_actions"]["safe_locks"] += round_actions["safe_locks"]
    stats["player_actions"]["bounty_locks"] += round_actions["bounty_locks"]
    stats["player_actions"]["tribunal_votes"] += round_actions["tribunal_votes"]
    stats["tribunal_outcomes"]["eliminations"].append(tribunal_eliminations)
    stats["tribunal_outcomes"]["honeypot_hits"] += honeypot_hits
    stats["scoring"]["dealer_scores"].append(dealer.score)
    stats["scoring"]["total_points_awarded"].append(sum(p.score for p in room.players.values()))


def summarize_stats(stats: Dict) -> Dict:
    rounds = max(1, stats["meta"]["rounds"])
    durations = stats["performance"]["round_durations_ms"]
    eliminations = stats["tribunal_outcomes"]["eliminations"]
    points = stats["scoring"]["total_points_awarded"]
    dealer_scores = stats["scoring"]["dealer_scores"]

    return {
        "meta": stats["meta"],
        "performance": {
            "avg_round_duration_ms": sum(durations) / len(durations) if durations else 0,
            "p95_round_duration_ms": sorted(durations)[int(0.95 * (len(durations) - 1))] if durations else 0,
        },
        "player_actions": {
            "safe_locks_per_round": stats["player_actions"]["safe_locks"] / rounds,
            "bounty_locks_per_round": stats["player_actions"]["bounty_locks"] / rounds,
            "tribunal_votes_per_round": stats["player_actions"]["tribunal_votes"] / rounds,
        },
        "scoring": {
            "avg_total_points_per_round": sum(points) / len(points) if points else 0,
            "avg_dealer_points_per_round": sum(dealer_scores) / len(dealer_scores) if dealer_scores else 0,
        },
        "tribunal_outcomes": {
            "avg_eliminations_per_round": sum(eliminations) / len(eliminations) if eliminations else 0,
            "honeypot_hits_total": stats["tribunal_outcomes"]["honeypot_hits"],
        },
    }


def simulate(ruleset: str, lobbies: int, rounds_per_lobby: int, players: int) -> Dict:
    stats = {
        "meta": {
            "ruleset": ruleset,
            "lobbies": lobbies,
            "rounds": lobbies * rounds_per_lobby,
            "players_per_lobby": players,
        },
        "performance": {"round_durations_ms": []},
        "player_actions": {"safe_locks": 0, "bounty_locks": 0, "tribunal_votes": 0},
        "scoring": {"dealer_scores": [], "total_points_awarded": []},
        "tribunal_outcomes": {"eliminations": [], "honeypot_hits": 0},
    }

    for lobby_idx in range(lobbies):
        room = make_room(f"S{lobby_idx:03d}", players, ruleset)
        for _ in range(rounds_per_lobby):
            run_round(room, stats)
            round_manager.next_round(room)

    return summarize_stats(stats)


def main():
    parser = argparse.ArgumentParser(description="Simulate Don't Say It lobby outcomes.")
    parser.add_argument("--ruleset", choices=["classic", "competitive"], default="competitive")
    parser.add_argument("--lobbies", type=int, default=25)
    parser.add_argument("--rounds-per-lobby", type=int, default=8)
    parser.add_argument("--players", type=int, default=8)
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    summary = simulate(args.ruleset, args.lobbies, args.rounds_per_lobby, args.players)
    payload = json.dumps(summary, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(payload + "\n")
    else:
        print(payload)


if __name__ == "__main__":
    main()
