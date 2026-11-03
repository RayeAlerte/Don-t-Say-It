from fastapi import FastAPI, WebSocket, WebSocketDisconnect, staticfiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from typing import Dict
import asyncio
import time
import random
import traceback 

from Models.game_state import Room, Player
from Models.payloads import ActionPayload
from logic import round_manager

active_rooms: Dict[str, Room] = {}
MAX_LATENCY_COMP_MS = 400.0
LATENCY_GRACE_BASE_S = 0.08
LATENCY_GRACE_CAP_S = 0.35
MIN_PHASE_TIME = 5
MAX_PHASE_TIME = 120

def assign_new_host(room: Room):
    if room.host in room.players:
        return
    room.metrics.host_reassignments_total += 1
    connected = [n for n, p in room.players.items() if p.connected]
    if connected:
        room.host = connected[0]
        return
    if room.players:
        room.host = next(iter(room.players.keys()))
        room.metrics.host_reassignments_to_disconnected += 1
    else:
        room.host = ""

async def room_cleaner():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale_rooms = [
            code for code, r in active_rooms.items()
            if now - r.last_activity > 300 and not any(
                p.connected and p.ws.client_state.value == 1  # 1 = CONNECTED in Starlette
                for p in r.players.values()
            )
        ]
        for code in stale_rooms:
            del active_rooms[code]

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleaner_task = asyncio.create_task(room_cleaner())
    yield
    cleaner_task.cancel()

app = FastAPI(lifespan=lifespan)

# Mount the static directory to serve CSS and JS files
app.mount("/static", staticfiles.StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_index():
    return FileResponse("index.html")

# --- FIXED: Zombie Timer Prevention & Auto Transitions ---
async def schedule_timeout(room_code: str, phase: str, current_round: int, delay: int, target_deadline: float):
    await asyncio.sleep(delay)
    room = active_rooms.get(room_code)
    if not room: return
    
    if room.phase == phase and room.current_round == current_round and room.phase_deadline == target_deadline:
        
        if phase == "trap_phase":
            prompt, trap = round_manager.get_random_prompt()
            round_manager.advance_to_response_phase(room, prompt, trap, "")
            
            new_deadline = time.time() + room.response_time
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, room.response_time, new_deadline))
            await broadcast_state(room)
            
        elif phase == "response_phase":
            # NEW: Transition to Tribunal
            round_manager.advance_to_tribunal(room)
            new_deadline = time.time() + room.tribunal_time
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "tribunal", room.current_round, room.tribunal_time, new_deadline))
            await broadcast_state(room)
            
        elif phase == "tribunal":
            # NEW: Resolve Tribunal into Reveal
            round_manager.resolve_round(room)
            new_deadline = time.time() + room.reveal_time
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "reveal", room.current_round, room.reveal_time, new_deadline))
            await broadcast_state(room)
            
        elif phase == "reveal":
            round_manager.next_round(room)
            if room.phase == "trap_phase":
                new_deadline = time.time() + room.dealer_time
                room.phase_deadline = new_deadline
                asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, room.dealer_time, new_deadline))
            await broadcast_state(room)


async def broadcast_state(room: Room):
    state = {
        "action": "state_update",
        "room_code": room.code,
        "host": room.host,
        "phase": room.phase,
        "ruleset": room.ruleset,
        "dealer_time": room.dealer_time,
        "response_time": room.response_time,
        "tribunal_time": room.tribunal_time,
        "reveal_time": room.reveal_time,
        "round": f"{room.current_round}/{room.round_limit}",
        "dealer": room.current_dealer,
        "time_left": max(0, int(room.phase_deadline - time.time())) if room.phase_deadline else 0,
        "prompt": room.prompt if room.phase != "lobby" else "",
        "words_to_vote": getattr(room, 'words_to_vote', []) if room.phase == "tribunal" else [],
        "players": [
            {
                "name": p.name, 
                "score": p.score,
                "streak": p.streak,
                "role": p.role,
                "is_dealer": p.is_dealer,
                "locked": p.locked_word is not None,
                "bounty_locked": p.bounty_guess is not None,
                "caught_in_honeypot": p.caught_in_honeypot,
                "mind_reader": p.mind_reader,
                "timed_out": p.timed_out,
                "vote_accuracy_hits": getattr(p, 'vote_accuracy_hits', 0),
                "vote_accuracy_total": getattr(p, 'vote_accuracy_total', 0),
                "my_vetoes": [w for w, voters in room.veto_votes.items() if p.name in voters]
            } for p in room.players.values()
        ]
    }
    
    if room.phase == "reveal" or room.phase == "game_over":
        state["trap_word"] = room.trap_word
        state["decoy_word"] = room.decoy_word
        state["vetoed_words"] = room.vetoed_words
        state["revealed_words"] = {p.name: p.locked_word for p in room.players.values() if not p.is_dealer and getattr(p, 'role', 'active') == "active"}
        state["revealed_bounties"] = {p.name: p.bounty_guess for p in room.players.values() if not p.is_dealer and p.bounty_guess}
        state["vote_accuracy_round"] = room.vote_accuracy_round

    for player in room.players.values():
        try:
            await player.ws.send_json(state)
        except Exception:
            pass

async def attach_player_to_room(room: Room, player_name: str, websocket):
    """Attach a player to a room after WebSocket.accept().

    Handles name sanitization, ban lookup, session replacement, and mid-game
    audience promotion. Returns (Player, None) on success or (None, reason)
    if the join was rejected (the caller must close the socket).

    Side effects:
    - Increments room.metrics counters for every guardrail that fires.
    - Sends a "rejected" frame to the *old* socket on session replacement.
    - Registers the new Player in room.players when it's a fresh join.
    """
    clean_name, name_blocked = round_manager.sanitize_player_name(player_name)
    if not clean_name or clean_name != player_name:
        room.metrics.xss_name_blocks_total += max(1, name_blocked)
        return None, "Invalid name. Use letters, numbers, spaces, _ or - (max 20)."

    if player_name in room.banned_names:
        return None, "You have been banned."

    if player_name in room.players:
        existing = room.players[player_name]
        if existing.connected and existing.ws is not None and existing.ws is not websocket:
            try:
                await existing.ws.send_json({
                    "action": "rejected",
                    "message": "Session replaced by another connection."
                })
                await existing.ws.close()
            except Exception:
                pass
            room.metrics.same_name_socket_replacements += 1
        existing.ws = websocket
        existing.connected = True
        return existing, None

    # Late joiners are ALWAYS audience — a fresh "active" mid-game would
    # never get a dealer slot and would get cornballed every round.
    if room.phase != "lobby":
        role = "audience"
        room.metrics.mid_game_audience_promotions += 1
    else:
        role = "audience" if len(room.get_active_players()) >= 15 else "active"
    player = Player(player_name, websocket, role=role)
    room.players[player_name] = player
    return player, None


@app.websocket("/ws/{room_code}/{player_name}")
async def game_endpoint(websocket: WebSocket, room_code: str, player_name: str):
    await websocket.accept()

    # Pre-validate name so we never spawn a room whose host is unusable.
    clean_probe, _ = round_manager.sanitize_player_name(player_name)
    if not clean_probe or clean_probe != player_name:
        await websocket.send_json({
            "action": "rejected",
            "message": "Invalid name. Use letters, numbers, spaces, _ or - (max 20)."
        })
        await websocket.close()
        return

    if room_code not in active_rooms:
        active_rooms[room_code] = Room(room_code, host=player_name)
    
    room = active_rooms[room_code]
    room.last_activity = time.time()

    player, reject_reason = await attach_player_to_room(room, player_name, websocket)
    if player is None:
        await websocket.send_json({"action": "rejected", "message": reject_reason})
        await websocket.close()
        return

    await broadcast_state(room)

    try:
        while True:
            raw_data = await websocket.receive_json()
            room.last_activity = time.time() 
            
            try:
                payload = ActionPayload(**raw_data)
                
                if payload.action == "random_prompt" and player.is_dealer and room.phase == "trap_phase":
                    prompt, _ = round_manager.get_random_prompt()
                    await websocket.send_json({"action": "fill_prompt", "prompt": prompt})

                elif payload.action == "kick_player" and player.name == room.host:
                    target_name = payload.word 
                    if target_name in room.players and target_name != room.host:
                        target_player = room.players[target_name]
                        target_player.connected = False
                        room.banned_names.append(target_name)
                        del room.players[target_name]
                        assign_new_host(room)
                        try:
                            await target_player.ws.send_json({"action": "rejected", "message": "Kicked by host."})
                            await target_player.ws.close()
                        except: pass
                        await broadcast_state(room)

                elif payload.action == "lock_trap" and player.is_dealer:
                    if room.phase == "trap_phase" and payload.prompt and payload.word:
                        prompt = round_manager.sanitize_prompt(payload.prompt)
                        trap, trap_blocked = round_manager.sanitize_game_word(payload.word)
                        decoy_raw = getattr(payload, "decoy", "") or ""
                        decoy, decoy_blocked = round_manager.sanitize_game_word(decoy_raw)
                        room.metrics.xss_word_blocks_total += trap_blocked + decoy_blocked
                        if not prompt or not trap:
                            await websocket.send_json({"action": "rejected", "message": "You need a prompt and a Trap."})
                            continue
                        if decoy and round_manager.is_match(trap, decoy):
                            await websocket.send_json({"action": "rejected", "message": "The Decoy has to be a different word than the Trap."})
                            continue
                        round_manager.advance_to_response_phase(room, prompt, trap, decoy)
                        
                        new_deadline = time.time() + room.response_time
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, room.response_time, new_deadline))
                        await broadcast_state(room)
                
                elif payload.action == "update_decoy" and player.is_dealer:
                    if room.phase == "response_phase" and payload.word:
                        decoy, decoy_blocked = round_manager.sanitize_game_word(payload.word)
                        room.metrics.xss_word_blocks_total += decoy_blocked
                        if not decoy:
                            await websocket.send_json({"action": "rejected", "message": "Decoy needs letters or numbers."})
                            continue
                        if round_manager.is_match(decoy, room.trap_word):
                            await websocket.send_json({"action": "rejected", "message": "The Decoy has to be a different word than the Trap."})
                            continue
                        room.decoy_word = decoy
                        await websocket.send_json({"action": "success", "message": "Decoy planted!"})
                        # Don't broadcast, it's a secret

                elif payload.action == "lock_word" and not player.is_dealer and player.role == "active":
                    if room.phase == "response_phase" and payload.word:
                        word, word_blocked = round_manager.sanitize_game_word(payload.word)
                        room.metrics.xss_word_blocks_total += word_blocked
                        if not word:
                            await websocket.send_json({"action": "rejected", "message": "Answers need letters or numbers."})
                            continue

                        # --- NEW: VIP Bypass for the Trap Word ---
                        if round_manager.is_match(word, room.trap_word):
                            player.locked_word = word
                            await websocket.send_json({"action": "trapped", "message": "💥 You said the Trap. No points this round."})
                                                    
                            # Check if this trap victim was the final player to lock in
                            if room.all_responders_locked():
                                round_manager.advance_to_tribunal(room)
                                new_deadline = time.time() + room.tribunal_time
                                room.phase_deadline = new_deadline
                                asyncio.create_task(schedule_timeout(room.code, "tribunal", room.current_round, room.tribunal_time, new_deadline))
                            
                            await broadcast_state(room)
                            continue # Skip the rest of the duplicate checking logic entirely!
                        # -----------------------------------------
                        
                        is_taken = False
                        thief = ""
                        steal_from = None
                        now = time.time()
                        player_adjusted_time = now - min(player.latency_ms, MAX_LATENCY_COMP_MS) / 1000
                        for locked_word, user in room.locked_words.items():
                            if round_manager.is_match(word, locked_word):
                                holder = room.players.get(user)
                                if not holder:
                                    continue
                                
                                holder_time = room.lock_times.get(locked_word, now)
                                holder_adjusted_time = holder_time - min(holder.latency_ms, MAX_LATENCY_COMP_MS) / 1000
                                dynamic_grace = min(
                                    LATENCY_GRACE_CAP_S,
                                    LATENCY_GRACE_BASE_S + abs(player.latency_ms - holder.latency_ms) / 1000
                                )
                                
                                if player_adjusted_time + dynamic_grace < holder_adjusted_time:
                                    steal_from = (locked_word, holder)
                                else:
                                    is_taken = True
                                    thief = user
                                break
                                
                        if is_taken:
                            await websocket.send_json({"action": "rejected", "message": f"Too slow! {thief} already locked something like that."})
                        else:
                            if steal_from:
                                old_word, holder = steal_from
                                room.locked_words.pop(old_word, None)
                                room.lock_times.pop(old_word, None)
                                holder.locked_word = None
                                try:
                                    await holder.ws.send_json({"action": "rejected", "message": "Too slow — someone beat you to it. Lock a new answer!"})
                                except Exception:
                                    pass
                            
                            room.locked_words[word] = player.name
                            room.lock_times[word] = now
                            player.locked_word = word
                            await websocket.send_json({"action": "success", "message": "Locked! Now guess the Trap."})
                            
                            if room.all_responders_locked():
                                round_manager.advance_to_tribunal(room)
                                new_deadline = time.time() + room.tribunal_time
                                room.phase_deadline = new_deadline
                                asyncio.create_task(schedule_timeout(room.code, "tribunal", room.current_round, room.tribunal_time, new_deadline))
                            await broadcast_state(room)

                elif payload.action == "bounty_guess" and not player.is_dealer:
                    if room.phase == "response_phase" and player.bounty_guess is None and payload.word:
                        if player.role == "active" and player.locked_word is None:
                            await websocket.send_json({"action": "rejected", "message": "Lock an answer before you guess the Trap."})
                            continue
                        bounty, bounty_blocked = round_manager.sanitize_game_word(payload.word)
                        room.metrics.xss_word_blocks_total += bounty_blocked
                        if not bounty:
                            await websocket.send_json({"action": "rejected", "message": "Guesses need letters or numbers."})
                            continue
                        player.bounty_guess = bounty
                        await websocket.send_json({"action": "success", "message": "Bounty locked!"})
                        await broadcast_state(room)
                
                elif payload.action == "ping_probe":
                    if round_manager.should_rate_limit_ping(player, time.time()):
                        room.metrics.ping_rate_limited_total += 1
                    else:
                        await websocket.send_json({"action": "pong_probe", "client_ts": payload.client_ts})
                
                elif payload.action == "latency_update":
                    if payload.latency_ms is not None:
                        sample = max(0.0, min(float(payload.latency_ms), MAX_LATENCY_COMP_MS))
                        player.latency_ms = (player.latency_ms * 0.7) + (sample * 0.3)

                elif payload.action == "toggle_veto" and room.phase == "tribunal":
                    if not round_manager.can_cast_tribunal_vote(player):
                        if player.is_dealer:
                            room.metrics.dealer_vote_rejections += 1
                        continue
                    if payload.word and payload.word in room.words_to_vote:
                        word = payload.word
                        if room.ruleset == "competitive":
                            currently_selected = player.name in room.veto_votes.get(word, [])
                            for target_word in room.veto_votes.keys():
                                if player.name in room.veto_votes[target_word]:
                                    room.veto_votes[target_word].remove(player.name)
                            
                            if not currently_selected:
                                if word not in room.veto_votes:
                                    room.veto_votes[word] = []
                                room.veto_votes[word].append(player.name)
                        else:
                            if word not in room.veto_votes:
                                room.veto_votes[word] = []
                            
                            if player.name in room.veto_votes[word]:
                                room.veto_votes[word].remove(player.name)
                            else:
                                room.veto_votes[word].append(player.name)
                        await broadcast_state(room) # Send update so UI highlights it
                
                elif payload.action == "set_ruleset" and player.name == room.host:
                    if room.phase == "lobby" and payload.ruleset in ("classic", "competitive"):
                        room.ruleset = payload.ruleset
                        mode_blurb = (
                            "Classic: veto as many as you want."
                            if room.ruleset == "classic"
                            else "Competitive: one veto."
                        )
                        await websocket.send_json({"action": "success", "message": mode_blurb})
                        await broadcast_state(room)
                
                elif payload.action == "set_timers" and player.name == room.host:
                    if room.phase == "lobby":
                        dealer_time = payload.dealer_time
                        response_time = payload.response_time
                        tribunal_time = payload.tribunal_time
                        reveal_time = payload.reveal_time
                        
                        if None in (dealer_time, response_time, tribunal_time, reveal_time):
                            await websocket.send_json({"action": "rejected", "message": "All timer fields are required."})
                            continue
                        
                        timer_values = [dealer_time, response_time, tribunal_time, reveal_time]
                        if any(not isinstance(v, int) or v < MIN_PHASE_TIME or v > MAX_PHASE_TIME for v in timer_values):
                            await websocket.send_json({"action": "rejected", "message": f"Timer values must be {MIN_PHASE_TIME}-{MAX_PHASE_TIME} seconds."})
                            continue
                        
                        room.dealer_time = dealer_time
                        room.response_time = response_time
                        room.tribunal_time = tribunal_time
                        room.reveal_time = reveal_time
                        await websocket.send_json({"action": "success", "message": "Lobby timers updated."})
                        await broadcast_state(room)
                
                elif payload.action == "start_game" and player.name == room.host:
                    if room.phase == "lobby" and len(room.get_active_players()) >= 3:
                        round_manager.start_game(room)
                        new_deadline = time.time() + room.dealer_time
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, room.dealer_time, new_deadline))
                        await broadcast_state(room)

                elif payload.action == "play_again" and player.name == room.host:
                    if room.phase == "game_over":
                        round_manager.play_again(room)
                        new_deadline = time.time() + room.dealer_time
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, room.dealer_time, new_deadline))
                        await broadcast_state(room)
                        
                elif payload.action == "return_lobby" and player.name == room.host:
                    if room.phase != "lobby":
                        round_manager.return_to_lobby(room)
                        await broadcast_state(room)

            except Exception as e:
                print(f"🔥 Error processing payload: {e}")
                traceback.print_exc()

    except WebSocketDisconnect:
        player.connected = False
        room.last_activity = time.time()

        if room.host == player.name:
            assign_new_host(room)

        if room.phase == "lobby":
            del room.players[player.name]
        else:
            async def delayed_removal(room_code, name, disconnect_time):
                await asyncio.sleep(120)
                r = active_rooms.get(room_code)
                if not r: return
                p = r.players.get(name)
                if p and not p.connected and p.disconnect_time == disconnect_time:
                    del r.players[name]
                    await broadcast_state(r)

            player.disconnect_time = time.time()
            asyncio.create_task(delayed_removal(room.code, player.name, player.disconnect_time))

        await broadcast_state(room)