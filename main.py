from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from typing import Dict
import asyncio
import time

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
            print(f"Room '{code}' closed due to inactivity.")
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

# --- NEW: Server-Side Timer Enforcer ---
async def schedule_timeout(room_code: str, phase: str, current_round: int, delay: int):
    await asyncio.sleep(delay)
    room = active_rooms.get(room_code)
    if not room: return
    
    # If the room is still stuck in the same phase and round when the timer expires, FORCE RESOLVE IT
    if room.phase == phase and room.current_round == current_round:
        if phase == "trap_phase":
            # Dealer took too long! Auto-assign a prompt and trap.
            round_manager.advance_to_response_phase(room, "Dealer took too long!", "cornball")
            room.phase_deadline = time.time() + 10
            asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, 10))
            await broadcast_state(room)
            
        elif phase == "response_phase":
            # Responders took too long! Slam the door.
            round_manager.resolve_round(room)
            await broadcast_state(room)

async def broadcast_state(room: Room):
    state = {
        "action": "state_update",
        "room_code": room.code,
        "host": room.host,
        "phase": room.phase,
        "round": f"{room.current_round}/{room.round_limit}",
        "dealer": room.current_dealer,
        # Sync the remaining time to the frontend safely
        "time_left": max(0, int(room.phase_deadline - time.time())) if room.phase_deadline else 0,
        "prompt": room.prompt if room.phase != "lobby" else "",
        "players": [
            {
                "name": p.name, 
                "score": p.score, 
                "is_dealer": p.is_dealer,
                "locked": p.locked_word is not None,
                "bounty_locked": p.bounty_guess is not None
            } for p in room.players.values()
        ]
    }
    
    if room.phase == "reveal" or room.phase == "game_over":
        state["trap_word"] = room.trap_word
        state["revealed_words"] = {p.name: p.locked_word for p in room.players.values() if not p.is_dealer}
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
    
    if player_name in room.players:
        room.players[player_name].ws = websocket
        player = room.players[player_name]
    else:
        player = Player(player_name, websocket)
        room.players[player_name] = player

    await broadcast_state(room)

    try:
        while True:
            raw_data = await websocket.receive_json()
            room.last_activity = time.time() 
            payload = ActionPayload(**raw_data)
            
            if payload.action == "lock_trap" and player.is_dealer:
                if room.phase == "trap_phase" and payload.prompt and payload.word:
                    round_manager.advance_to_response_phase(room, payload.prompt, payload.word)
                    room.phase_deadline = time.time() + 10 # 10 SECONDS FOR RESPONDERS
                    asyncio.create_task(schedule_timeout(room.code, "response_phase", room.current_round, 10))
                    await broadcast_state(room)
                    
            elif payload.action == "lock_word" and not player.is_dealer:
                if room.phase == "response_phase" and payload.word:
                    word = payload.word.strip().lower()[:25]
                    if not word: continue
                        
                    if word in room.locked_words:
                        thief = room.locked_words[word]
                        await websocket.send_json({"action": "rejected", "message": f"Too slow! '{thief}' took that."})
                    else:
                        room.locked_words[word] = player.name
                        player.locked_word = word
                        await websocket.send_json({"action": "success", "message": "Word locked!"})
                        
                        if room.all_responders_locked():
                            round_manager.resolve_round(room)
                        await broadcast_state(room)

            elif payload.action == "bounty_guess" and not player.is_dealer:
                if room.phase == "response_phase" and player.bounty_guess is None and payload.word:
                    player.bounty_guess = payload.word.strip().lower()[:25]
                    await websocket.send_json({"action": "success", "message": "Bounty guess locked!"})
                    await broadcast_state(room)
                    
            elif payload.action == "next_round" and player.is_dealer:
                if room.phase == "reveal":
                    round_manager.next_round(room)
                    if room.phase == "trap_phase":
                        room.phase_deadline = time.time() + 30 # 30 SECONDS FOR DEALER
                        asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30))
                    await broadcast_state(room)
            
            elif payload.action == "start_game" and player.name == room.host:
                if room.phase == "lobby" and len(room.players) >= 3:
                    round_manager.start_game(room)
                    room.phase_deadline = time.time() + 30
                    asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30))
                    await broadcast_state(room)

            elif payload.action == "play_again" and player.name == room.host:
                if room.phase == "game_over":
                    round_manager.play_again(room)
                    room.phase_deadline = time.time() + 30
                    asyncio.create_task(schedule_timeout(room.code, "trap_phase", room.current_round, 30))
                    await broadcast_state(room)
                    
            elif payload.action == "return_lobby" and player.name == room.host:
                if room.phase == "game_over":
                    round_manager.return_to_lobby(room)
                    await broadcast_state(room)

    except WebSocketDisconnect:
        print(f"{player_name} disconnected.")