"""
オンラインお絵描き当てゲーム - ゲーム状態管理

モード:
  - solo: 1ルーム内で完結する個人戦(3人以上)
  - team: 2ルームがマッチングして戦うチーム戦(各ルーム3人以上)

個人戦フロー:
  waiting -> topic_submit(お題提出) -> drawing(30秒) -> reveal(順番に見せる) -> guessing(回答) -> result -> (次ラウンド or 終了)

チーム戦フロー:
  matching(相手チーム待ち) -> topic_submit(先攻/後攻が交互にお題決定) -> drawing -> reveal -> guessing -> result
  -> 5点先取したチームの勝ち
"""
from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


DRAW_SECONDS = 30
WIN_SCORE = 5


def gen_code(n: int = 5) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


class Phase(str, Enum):
    LOBBY = "lobby"                 # 参加者待ち
    TOPIC_SUBMIT = "topic_submit"   # お題提出待ち
    DRAWING = "drawing"             # 30秒描画中
    REVEAL = "reveal"               # 一人ずつ絵を見せている
    GUESSING = "guessing"           # 回答者が回答中
    RESULT = "result"               # ラウンド結果表示
    MATCHING = "matching"           # チーム戦: 対戦相手待ち
    GAME_OVER = "game_over"         # ゲーム終了


@dataclass
class Player:
    id: str
    name: str
    ws: object = None
    is_host: bool = False

    def public(self):
        return {"id": self.id, "name": self.name, "is_host": self.is_host}


@dataclass
class Drawing:
    player_id: str
    player_name: str
    image_data: str = ""  # base64 PNG


@dataclass
class Room:
    code: str
    mode: str  # "solo" | "team"
    players: dict[str, Player] = field(default_factory=dict)
    phase: Phase = Phase.LOBBY

    # ラウンド進行用
    answerer_id: Optional[str] = None      # 回答者
    topic: Optional[str] = None
    topic_submitter_id: Optional[str] = None
    drawings: list[Drawing] = field(default_factory=list)
    reveal_index: int = 0
    draw_deadline: float = 0.0
    guess_history: list[dict] = field(default_factory=list)  # {player, text, correct}
    round_num: int = 0
    used_answerers: set[str] = field(default_factory=set)

    # チーム戦用
    team_score: int = 0
    opponent_room_code: Optional[str] = None
    is_leading_team: bool = False  # 先攻チームか
    my_turn_to_pick_topic: bool = False

    def alive_players(self) -> list[Player]:
        return list(self.players.values())

    def drawer_ids(self) -> list[str]:
        return [pid for pid in self.players if pid != self.answerer_id]

    def public_state(self, viewer_id: str, opp_score: Optional[int] = None) -> dict:
        current_drawing = None
        if self.phase == Phase.REVEAL and 0 <= self.reveal_index < len(self.drawings):
            d = self.drawings[self.reveal_index]
            current_drawing = {"player_name": d.player_name, "image_data": d.image_data}
        return {
            "code": self.code,
            "mode": self.mode,
            "phase": self.phase.value,
            "players": [p.public() for p in self.players.values()],
            "answerer_id": self.answerer_id,
            "topic": self.topic if (viewer_id != self.answerer_id or self.phase in (Phase.RESULT, Phase.GAME_OVER)) else None,
            "topic_submitter_id": self.topic_submitter_id,
            "round_num": self.round_num,
            "team_score": self.team_score,
            "opp_score": opp_score,
            "opponent_room_code": self.opponent_room_code,
            "is_leading_team": self.is_leading_team,
            "reveal_index": self.reveal_index,
            "drawings_count": len(self.drawings),
            "current_drawing": current_drawing,
            "guess_history": self.guess_history,
            "draw_seconds_left": max(0, int(self.draw_deadline - time.time())) if self.phase == Phase.DRAWING else None,
        }


class GameManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.team_queue: list[str] = []  # マッチング待ちのroom code

    # ---------- ルーム管理 ----------
    def create_room(self, mode: str) -> Room:
        code = gen_code()
        while code in self.rooms:
            code = gen_code()
        room = Room(code=code, mode=mode)
        self.rooms[code] = room
        return room

    def get_room(self, code: str) -> Optional[Room]:
        return self.rooms.get(code.upper())

    def add_player(self, room: Room, player: Player):
        if not room.players:
            player.is_host = True
        room.players[player.id] = player

    def remove_player(self, room: Room, player_id: str):
        room.players.pop(player_id, None)
        if not room.players:
            # ルーム削除
            self.rooms.pop(room.code, None)
            if room.code in self.team_queue:
                self.team_queue.remove(room.code)
            return
        if not any(p.is_host for p in room.players.values()):
            next(iter(room.players.values())).is_host = True

    # ---------- 個人戦ロジック ----------
    def pick_next_answerer(self, room: Room) -> Optional[str]:
        candidates = [pid for pid in room.players if pid not in room.used_answerers]
        if not candidates:
            room.used_answerers.clear()
            candidates = list(room.players.keys())
        chosen = random.choice(candidates)
        room.used_answerers.add(chosen)
        return chosen

    def start_round(self, room: Room):
        room.round_num += 1
        room.answerer_id = self.pick_next_answerer(room)
        room.topic = None
        room.topic_submitter_id = None
        room.drawings = []
        room.reveal_index = 0
        room.guess_history = []
        room.phase = Phase.TOPIC_SUBMIT

    def submit_topic(self, room: Room, submitter_id: str, topic: str):
        room.topic = topic.strip()[:40]
        room.topic_submitter_id = submitter_id
        room.phase = Phase.DRAWING
        room.draw_deadline = time.time() + DRAW_SECONDS

    def submit_drawing(self, room: Room, player_id: str, image_data: str) -> bool:
        """全員分揃ったらTrueを返す"""
        if any(d.player_id == player_id for d in room.drawings):
            return len(room.drawings) >= len(room.drawer_ids())
        pname = room.players[player_id].name
        room.drawings.append(Drawing(player_id=player_id, player_name=pname, image_data=image_data))
        if len(room.drawings) >= len(room.drawer_ids()):
            room.phase = Phase.REVEAL
            room.reveal_index = 0
            return True
        return False

    def force_finish_drawing(self, room: Room):
        """時間切れで未提出者を空絵として扱う"""
        submitted_ids = {d.player_id for d in room.drawings}
        for pid in room.drawer_ids():
            if pid not in submitted_ids:
                room.drawings.append(Drawing(player_id=pid, player_name=room.players[pid].name, image_data=""))
        room.phase = Phase.REVEAL
        room.reveal_index = 0

    def next_reveal(self, room: Room) -> bool:
        """次の絵へ。全部見終わったらGUESSINGに移行しTrueを返す"""
        room.reveal_index += 1
        if room.reveal_index >= len(room.drawings):
            room.phase = Phase.GUESSING
            return True
        return False

    def submit_guess(self, room: Room, guess_text: str) -> bool:
        correct = _normalize(guess_text) == _normalize(room.topic or "")
        room.guess_history.append({"text": guess_text, "correct": correct})
        if correct:
            room.phase = Phase.RESULT
            if room.mode == "team":
                room.team_score += 1
        return correct

    def give_up(self, room: Room):
        room.phase = Phase.RESULT

    # ---------- チーム戦マッチング ----------
    def enqueue_for_match(self, room: Room) -> Optional[Room]:
        room.phase = Phase.MATCHING
        if self.team_queue and self.team_queue[0] != room.code:
            opp_code = self.team_queue.pop(0)
            opp = self.rooms.get(opp_code)
            if opp is None:
                self.team_queue.append(room.code)
                return None
            # マッチ成立
            room.opponent_room_code = opp.code
            opp.opponent_room_code = room.code
            leading = random.choice([True, False])
            room.is_leading_team = leading
            opp.is_leading_team = not leading
            room.team_score = 0
            opp.team_score = 0
            return opp
        else:
            if room.code not in self.team_queue:
                self.team_queue.append(room.code)
            return None


def _normalize(s: str) -> str:
    return "".join(s.split()).lower()