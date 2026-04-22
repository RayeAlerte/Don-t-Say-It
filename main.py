from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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

async def room_cleaner():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale_rooms = [code for code, r in active_rooms.items() if now - r.last_activity > 300]
        for code in stale_rooms:
            del active_rooms[code]

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleaner_task = asyncio.create_task(room_cleaner())
    yield
    cleaner_task.cancel()

app = FastAPI(lifespan=lifespan)

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
            
            new_deadline = time.time() + 10 
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, 10, new_deadline))
            await broadcast_state(room)
            
        elif phase == "response_phase":
            # NEW: Transition to Tribunal
            round_manager.advance_to_tribunal(room)
            new_deadline = time.time() + 10 
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "tribunal", room.current_round, 10, new_deadline))
            await broadcast_state(room)
            
        elif phase == "tribunal":
            # NEW: Resolve Tribunal into Reveal
            round_manager.resolve_round(room)
            new_deadline = time.time() + 10 
            room.phase_deadline = new_deadline
            asyncio.create_task(schedule_timeout(room.code, "reveal", room.current_round, 10, new_deadline))
            await broadcast_state(room)
            
        elif phase == "reveal":
            round_manager.next_round(room)
            if room.phase == "trap_phase":
                new_deadline = time.time() + 30
                room.phase_deadline = new_deadline
                asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30, new_deadline))
            await broadcast_state(room)


async def broadcast_state(room: Room):
    state = {
        "action": "state_update",
        "room_code": room.code,
        "host": room.host,
        "phase": room.phase,
        "round": f"{room.current_round}/{room.round_limit}",
        "dealer": room.current_dealer,
        "time_left": max(0, int(room.phase_deadline - time.time())) if room.phase_deadline else 0,
        "prompt": room.prompt if room.phase != "lobby" else "",
        "words_to_vote": getattr(room, 'words_to_vote', []) if room.phase == "tribunal" else [],
        "players": [
            {
                "name": p.name, 
                "score": p.score,
                "streak": getattr(p, 'streak', 0), 
                "role": getattr(p, 'role', 'active'),
                "is_dealer": p.is_dealer,
                "locked": p.locked_word is not None,
                "bounty_locked": p.bounty_guess is not None,
                "caught": getattr(p, 'caught_in_honeypot', False),
                "mind_reader": getattr(p, 'mind_reader', False),
                "timed_out": getattr(p, 'timed_out', False),
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

    for player in room.players.values():
        try:
            await player.ws.send_json(state)
        except Exception:
            pass

@app.websocket("/ws/{room_code}/{player_name}")
async def game_endpoint(websocket: WebSocket, room_code: str, player_name: str):
    await websocket.accept()
    
    if room_code not in active_rooms:
        active_rooms[room_code] = Room(room_code, host=player_name)
    
    room = active_rooms[room_code]
    room.last_activity = time.time()
    
    if player_name in room.banned_names:
        await websocket.send_json({"action": "rejected", "message": "You have been banned."})
        await websocket.close()
        return

    if player_name in room.players:
        room.players[player_name].ws = websocket
        player = room.players[player_name]
    else:
        role = "audience" if len(room.get_active_players()) >= 15 else "active"
        player = Player(player_name, websocket, role=role)
        room.players[player_name] = player

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
                        room.banned_names.append(target_name)
                        del room.players[target_name]
                        try:
                            await target_player.ws.send_json({"action": "rejected", "message": "Kicked by host."})
                            await target_player.ws.close()
                        except: pass
                        await broadcast_state(room)

                elif payload.action == "lock_trap" and player.is_dealer:
                    if room.phase == "trap_phase" and payload.prompt and payload.word:
                        decoy = getattr(payload, "decoy", "") # Safely get decoy
                        round_manager.advance_to_response_phase(room, payload.prompt, payload.word, decoy)
                        
                        new_deadline = time.time() + 10 
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, 10, new_deadline))
                        await broadcast_state(room)
                
                elif payload.action == "update_decoy" and player.is_dealer:
                    if room.phase == "response_phase" and payload.word:
                        room.decoy_word = payload.word.strip()
                        await websocket.send_json({"action": "success", "message": "Decoy updated!"})
                        # Don't broadcast, it's a secret

                elif payload.action == "lock_word" and not player.is_dealer and player.role == "active":
                    if room.phase == "response_phase" and payload.word:
                        word = payload.word.strip()[:25]
                        is_taken = False
                        thief = ""
                        for locked_word, user in room.locked_words.items():
                            if round_manager.is_match(word, locked_word):
                                is_taken = True
                                thief = user
                                break
                                
                        if is_taken:
                            await websocket.send_json({"action": "rejected", "message": f"Too slow! '{thief}' took something similar."})
                        else:
                            room.locked_words[word] = player.name
                            player.locked_word = word
                            await websocket.send_json({"action": "success", "message": "Word locked!"})
                            
                            if room.all_responders_locked():
                                round_manager.advance_to_tribunal(room)
                                new_deadline = time.time() + 10 
                                room.phase_deadline = new_deadline
                                asyncio.create_task(schedule_timeout(room.code, "tribunal", room.current_round, 10, new_deadline))
                            await broadcast_state(room)

                elif payload.action == "bounty_guess" and not player.is_dealer:
                    if room.phase == "response_phase" and player.bounty_guess is None and payload.word:
                        player.bounty_guess = payload.word.strip()[:25]
                        await websocket.send_json({"action": "success", "message": "Bounty guess locked!"})
                        await broadcast_state(room)

                elif payload.action == "toggle_veto" and room.phase == "tribunal":
                    if payload.word:
                        word = payload.word
                        if word not in room.veto_votes:
                            room.veto_votes[word] = []
                        
                        if player.name in room.veto_votes[word]:
                            room.veto_votes[word].remove(player.name)
                        else:
                            room.veto_votes[word].append(player.name)
                        await broadcast_state(room) # Send update so UI highlights it
                
                elif payload.action == "start_game" and player.name == room.host:
                    if room.phase == "lobby" and len(room.get_active_players()) >= 3:
                        round_manager.start_game(room)
                        new_deadline = time.time() + 30
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30, new_deadline))
                        await broadcast_state(room)

                elif payload.action == "play_again" and player.name == room.host:
                    if room.phase == "game_over":
                        round_manager.play_again(room)
                        new_deadline = time.time() + 30
                        room.phase_deadline = new_deadline
                        asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30, new_deadline))
                        await broadcast_state(room)
                        
                elif payload.action == "return_lobby" and player.name == room.host:
                    if room.phase == "game_over":
                        round_manager.return_to_lobby(room)
                        await broadcast_state(room)

            except Exception as e:
                print(f"🔥 Error processing payload: {e}")
                traceback.print_exc()

    except WebSocketDisconnect:
        pass