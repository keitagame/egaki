"""
オンラインお絵描き当てゲーム サーバー

起動:
    pip install fastapi "uvicorn[standard]" websockets --break-system-packages
    python server.py
    ブラウザで http://localhost:8000 を開く(複数タブ/複数PCで参加可能)
"""
import asyncio
import json
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from game_state import GameManager, Player, Phase, DRAW_SECONDS, WIN_SCORE

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

gm = GameManager()

# room_code -> {player_id: WebSocket}
connections: dict[str, dict[str, WebSocket]] = {}
# player_id -> room_code  (どのルームに居るか逆引き)
player_room: dict[str, str] = {}
# ラウンドタイマー管理: room_code -> asyncio.Task
timers: dict[str, asyncio.Task] = {}


@app.get("/")
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


async def broadcast(room_code: str, msg: dict):
    conns = connections.get(room_code, {})
    dead = []
    for pid, ws in conns.items():
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(pid)
    for pid in dead:
        conns.pop(pid, None)


async def send_state(room_code: str):
    room = gm.get_room(room_code)
    if room is None:
        return
    opp = gm.get_room(room.opponent_room_code) if (room.mode == "team" and room.opponent_room_code) else None
    opp_score = opp.team_score if opp else None
    conns = connections.get(room_code, {})
    for pid, ws in list(conns.items()):
        try:
            await ws.send_json({"type": "state", "state": room.public_state(pid, opp_score=opp_score)})
        except Exception:
            pass


async def sync_team_pair(room_code: str):
    """チーム戦で片方の状態が変わったら両ルームに送る"""
    room = gm.get_room(room_code)
    if not room:
        return
    await send_state(room_code)
    if room.opponent_room_code:
        await send_state(room.opponent_room_code)


def cancel_timer(room_code: str):
    t = timers.pop(room_code, None)
    if t and not t.done():
        t.cancel()


async def start_draw_timer(room_code: str):
    cancel_timer(room_code)

    async def _run():
        try:
            await asyncio.sleep(DRAW_SECONDS + 0.5)
            room = gm.get_room(room_code)
            if room and room.phase == Phase.DRAWING:
                gm.force_finish_drawing(room)
                await broadcast_all_states(room)
        except asyncio.CancelledError:
            pass

    timers[room_code] = asyncio.create_task(_run())


async def broadcast_all_states(room):
    if room.mode == "team":
        await sync_team_pair(room.code)
    else:
        await send_state(room.code)


@app.websocket("/ws/{room_code}/{player_id}")
async def ws_endpoint(websocket: WebSocket, room_code: str, player_id: str):
    await websocket.accept()
    room_code = room_code.upper()
    connections.setdefault(room_code, {})[player_id] = websocket
    player_room[player_id] = room_code

    room = gm.get_room(room_code)
    if room:
        await broadcast_all_states(room)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_message(room_code, player_id, msg)
    except WebSocketDisconnect:
        pass
    finally:
        conns = connections.get(room_code, {})
        conns.pop(player_id, None)
        room = gm.get_room(room_code)
        if room and player_id in room.players:
            gm.remove_player(room, player_id)
            if room.code in gm.rooms:
                # ロビー中の離脱ならフェーズをlobbyに戻す等の簡易対応
                if len(room.players) < 3 and room.phase not in (Phase.LOBBY, Phase.MATCHING, Phase.GAME_OVER):
                    room.phase = Phase.LOBBY
                await broadcast_all_states(room)


async def handle_message(room_code: str, player_id: str, msg: dict):
    action = msg.get("action")
    room = gm.get_room(room_code)

    if action == "join":
        # ルーム作成 or 参加はREST的にHTTPでなくWSメッセージで処理(シンプル化)
        return

    if room is None:
        return

    if action == "start_game":
        if len(room.players) < 3:
            await send_error(room_code, player_id, "3人以上必要です")
            return
        if room.mode == "solo":
            gm.start_round(room)
            await broadcast_all_states(room)
        else:
            # チーム戦: マッチング開始
            opp = gm.enqueue_for_match(room)
            if opp:
                room.round_num = 1
                opp.round_num = 1
                _begin_team_round(room)
                _begin_team_round(opp)
                room.phase = Phase.TOPIC_SUBMIT
                opp.phase = Phase.TOPIC_SUBMIT
                room.answerer_id = gm.pick_next_answerer(room)
                opp.answerer_id = gm.pick_next_answerer(opp)
            await sync_team_pair(room.code)

    elif action == "submit_topic":
        topic = msg.get("topic", "")
        if room.phase != Phase.TOPIC_SUBMIT:
            return
        if room.mode == "team":
            leading_room = room if room.is_leading_team else gm.get_room(room.opponent_room_code)
            if leading_room and leading_room.code != room.code:
                # 後攻チームは topic を出せない
                await send_error(room_code, player_id, "相手チームの手番です")
                return
        if player_id == room.answerer_id:
            await send_error(room_code, player_id, "回答者はお題を出せません")
            return
        gm.submit_topic(room, player_id, topic)
        await start_draw_timer(room.code)
        await broadcast_all_states(room)

    elif action == "submit_drawing":
        image_data = msg.get("image", "")
        if room.phase != Phase.DRAWING:
            return
        done = gm.submit_drawing(room, player_id, image_data)
        if done:
            cancel_timer(room.code)
        await broadcast_all_states(room)

    elif action == "next_reveal":
        if room.phase != Phase.REVEAL:
            return
        gm.next_reveal(room)
        await broadcast_all_states(room)

    elif action == "submit_guess":
        if room.phase != Phase.GUESSING:
            return
        if player_id != room.answerer_id:
            return
        guess = msg.get("guess", "")
        correct = gm.submit_guess(room, guess)
        await broadcast_all_states(room)
        if correct and room.mode == "team":
            await _handle_team_point(room)
        elif not correct:
            pass  # 誤答してもguessingのまま続行(何度でも回答可)
        else:
            pass

    elif action == "give_up":
        if room.phase != Phase.GUESSING:
            return
        gm.give_up(room)
        await broadcast_all_states(room)

    elif action == "next_round":
        if room.mode != "solo":
            return
        gm.start_round(room)
        await broadcast_all_states(room)

    elif action == "team_next_topic_phase":
        # チーム戦: 結果画面から次のお題フェーズへ(手番交代)
        if room.mode != "team":
            return
        await _advance_team_turn(room)


def _begin_team_round(room):
    room.drawings = []
    room.reveal_index = 0
    room.guess_history = []
    room.topic = None


async def _handle_team_point(room):
    opp = gm.get_room(room.opponent_room_code) if room.opponent_room_code else None
    if room.team_score >= WIN_SCORE:
        room.phase = Phase.GAME_OVER
        if opp:
            opp.team_score = room.team_score
            opp.phase = Phase.GAME_OVER
        await sync_team_pair(room.code)
        return
    if opp:
        opp.team_score = room.team_score
    await sync_team_pair(room.code)


async def _advance_team_turn(room):
    opp = gm.get_room(room.opponent_room_code) if room.opponent_room_code else None
    if not opp:
        return
    # 手番交代: 先攻/後攻を入れ替える
    room.is_leading_team = not room.is_leading_team
    opp.is_leading_team = not opp.is_leading_team
    room.round_num += 1
    opp.round_num += 1
    for r in (room, opp):
        _begin_team_round(r)
        r.answerer_id = gm.pick_next_answerer(r)
        r.phase = Phase.TOPIC_SUBMIT
    await sync_team_pair(room.code)


async def send_error(room_code: str, player_id: str, text: str):
    ws = connections.get(room_code, {}).get(player_id)
    if ws:
        try:
            await ws.send_json({"type": "error", "message": text})
        except Exception:
            pass


# ---------- REST的な補助エンドポイント(ルーム作成/参加はJSON APIで簡潔に) ----------
from fastapi import Body


@app.post("/api/create_room")
async def api_create_room(mode: str = Body(...), name: str = Body(...)):
    room = gm.create_room(mode)
    pid = str(uuid.uuid4())[:8]
    player = Player(id=pid, name=name[:16])
    gm.add_player(room, player)
    return {"room_code": room.code, "player_id": pid}


@app.post("/api/join_room")
async def api_join_room(room_code: str = Body(...), name: str = Body(...)):
    room = gm.get_room(room_code)
    if room is None:
        return {"error": "ルームが見つかりません"}
    if room.phase != Phase.LOBBY:
        return {"error": "このルームは既にゲーム中です"}
    pid = str(uuid.uuid4())[:8]
    player = Player(id=pid, name=name[:16])
    gm.add_player(room, player)
    return {"room_code": room.code, "player_id": pid}


@app.get("/api/rooms")
async def api_rooms():
    return {
        "rooms": [
            {"code": r.code, "mode": r.mode, "players": len(r.players), "phase": r.phase.value}
            for r in gm.rooms.values()
            if r.phase == Phase.LOBBY
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)