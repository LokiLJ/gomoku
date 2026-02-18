"""
五子棋在线对战服务器
功能：用户名、积分榜、管理员、认负、计时器、悔棋申请、暂停、总时间
"""

import json
import asyncio
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

VALID_ANSWERS = {"20051218", "20210620"}
ADMIN_PASSWORD = "230620"


class VerifyRequest(BaseModel):
    answer: str


@app.post("/verify")
async def verify_answer(req: VerifyRequest):
    cleaned = req.answer.strip()
    if cleaned in VALID_ANSWERS:
        return {"success": True}
    return {"success": False, "message": "回答错误，请重试"}


# ============================================================
# 游戏核心
# ============================================================

class GomokuGame:
    def __init__(self):
        self.reset()

    def reset(self):
        self.board = [[0] * 15 for _ in range(15)]
        self.current_turn = 1
        self.winner = 0
        self.move_history = []
        self.game_started = False

    def place_stone(self, row, col, color):
        if color != self.current_turn:
            return {"success": False, "winner": 0, "message": "还没轮到你"}
        if self.winner != 0:
            return {"success": False, "winner": self.winner, "message": "游戏已结束"}
        if not (0 <= row < 15 and 0 <= col < 15):
            return {"success": False, "winner": 0, "message": "位置超出棋盘"}
        if self.board[row][col] != 0:
            return {"success": False, "winner": 0, "message": "该位置已有棋子"}
        self.board[row][col] = color
        self.move_history.append((row, col, color))
        if self._check_win(row, col, color):
            self.winner = color
            return {"success": True, "winner": color,
                    "message": f"{'黑' if color == 1 else '白'}方获胜！"}
        if len(self.move_history) >= 225:
            return {"success": True, "winner": -1, "message": "平局！"}
        self.current_turn = 3 - color
        return {"success": True, "winner": 0, "message": ""}

    def resign(self, color):
        if self.winner != 0:
            return False
        self.winner = 3 - color
        return True

    def timeout(self, color):
        if self.winner != 0:
            return False
        self.winner = 3 - color
        return True

    def undo(self):
        if not self.move_history:
            return False
        row, col, color = self.move_history.pop()
        self.board[row][col] = 0
        self.current_turn = color
        self.winner = 0
        return True

    def _check_win(self, row, col, color):
        for dr, dc in [(0, 1), (1, 0), (1, 1), (1, -1)]:
            count = 1
            r, c = row + dr, col + dc
            while 0 <= r < 15 and 0 <= c < 15 and self.board[r][c] == color:
                count += 1; r += dr; c += dc
            r, c = row - dr, col - dc
            while 0 <= r < 15 and 0 <= c < 15 and self.board[r][c] == color:
                count += 1; r -= dr; c -= dc
            if count >= 5:
                return True
        return False

    def get_state(self):
        return {
            "board": self.board,
            "current_turn": self.current_turn,
            "winner": self.winner,
            "move_history": self.move_history,
            "game_started": self.game_started,
        }


# ============================================================
# 连接管理
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.game = GomokuGame()
        self.players = {}
        self.spectators = []
        self.usernames = {}
        self.scoreboard = {}
        self.max_capacity = 3
        self.lock = asyncio.Lock()

        # --- 计时系统 ---
        self.turn_time_limit = 20       # 每步时限（0=不限）
        self.total_time_setting = 300   # 总时间设置（秒），默认5分钟（0=不限）
        self.total_time = {1: 300, 2: 300}  # 各方剩余总时间
        self.turn_remaining = 0         # 当前步剩余秒数
        self.timer_task = None

        # --- 暂停系统 ---
        self.paused = False
        self.pause_by = 0               # 谁发起的暂停（颜色）
        self.pause_remaining = 0        # 暂停剩余秒数
        self.pause_counts = {1: 2, 2: 2}  # 各方剩余暂停次数
        self.pause_duration = 300       # 每次暂停时长（秒），默认5分钟
        self.pause_timer_task = None

        # --- 悔棋 ---
        self.pending_undo_from = None

    def _get_total_count(self):
        return len(self.players) + len(self.spectators)

    def reset_timers(self):
        """重置所有计时器状态"""
        self.total_time = {1: self.total_time_setting, 2: self.total_time_setting}
        self.turn_remaining = self.turn_time_limit
        self.paused = False
        self.pause_by = 0
        self.pause_remaining = 0
        self.pause_counts = {1: 2, 2: 2}
        self.pending_undo_from = None

    # =============== 主计时器 ===============

    async def start_timer(self):
        await self.cancel_timer()
        if self.game.winner != 0 or not self.game.game_started or self.paused:
            return
        self.turn_remaining = self.turn_time_limit
        self.timer_task = asyncio.create_task(self._timer_loop())

    async def cancel_timer(self):
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
        self.timer_task = None

    async def _timer_loop(self):
        """每秒 tick：递减步时 + 总时间"""
        try:
            await self._broadcast_timer()
            while True:
                await asyncio.sleep(1)
                color = self.game.current_turn

                # 递减步时
                if self.turn_time_limit > 0:
                    self.turn_remaining -= 1
                    if self.turn_remaining <= 0:
                        await self._handle_timeout(color, "turn")
                        return

                # 递减总时间
                if self.total_time_setting > 0:
                    self.total_time[color] -= 1
                    if self.total_time[color] <= 0:
                        self.total_time[color] = 0
                        await self._handle_timeout(color, "total")
                        return

                await self._broadcast_timer()

        except asyncio.CancelledError:
            pass

    async def _handle_timeout(self, color, reason):
        """处理超时"""
        if self.game.timeout(color):
            loser_name = self.usernames.get(self.players.get(color), "???")
            winner_color = 3 - color
            winner_ws = self.players.get(winner_color)
            if winner_ws:
                wn = self.usernames.get(winner_ws, "???")
                self.scoreboard[wn] = self.scoreboard.get(wn, 0) + 1

            reason_text = "步时超时" if reason == "turn" else "总时间耗尽"
            await self.broadcast({
                "type": "game_over",
                "winner": winner_color,
                "reason": reason,
                "message": f"{loser_name} {reason_text}，{'黑' if winner_color == 1 else '白'}方获胜！",
            })
            await self._broadcast_timer()
            await self.broadcast_scoreboard()

    async def _broadcast_timer(self):
        """广播完整计时状态"""
        await self.broadcast({
            "type": "timer_sync",
            "turn_remaining": self.turn_remaining if self.turn_time_limit > 0 else -1,
            "turn_total": self.turn_time_limit,
            "total_time": {str(k): v for k, v in self.total_time.items()},
            "total_time_setting": self.total_time_setting,
            "current_turn": self.game.current_turn,
            "paused": self.paused,
            "pause_by": self.pause_by,
            "pause_remaining": self.pause_remaining,
            "pause_counts": {str(k): v for k, v in self.pause_counts.items()},
        })

    # =============== 暂停系统 ===============

    async def handle_pause(self, websocket):
        """玩家申请暂停"""
        color = self._get_color(websocket)
        if color is None or not self.game.game_started or self.game.winner != 0:
            return
        if self.paused:
            await websocket.send_json({"type": "error", "message": "已在暂停中"})
            return
        if self.pause_counts.get(color, 0) <= 0:
            await websocket.send_json({"type": "error", "message": "你的暂停次数已用完"})
            return

        self.pause_counts[color] -= 1
        self.paused = True
        self.pause_by = color
        self.pause_remaining = self.pause_duration

        # 停掉主计时器
        await self.cancel_timer()

        pname = self.usernames.get(websocket, "???")
        await self.broadcast({
            "type": "admin_message",
            "message": f"⏸ {pname} 申请暂停（剩余{self.pause_counts[color]}次）",
        })

        # 启动暂停倒计时
        self.pause_timer_task = asyncio.create_task(self._pause_countdown())
        await self._broadcast_timer()

    async def handle_unpause(self, websocket):
        """任一棋手取消暂停"""
        color = self._get_color(websocket)
        if color is None or not self.paused:
            return

        await self._do_unpause()
        pname = self.usernames.get(websocket, "???")
        await self.broadcast({
            "type": "admin_message",
            "message": f"▶️ {pname} 取消了暂停",
        })

    async def _do_unpause(self):
        """执行取消暂停"""
        self.paused = False
        self.pause_by = 0
        self.pause_remaining = 0

        # 取消暂停倒计时
        if self.pause_timer_task and not self.pause_timer_task.done():
            self.pause_timer_task.cancel()
            try:
                await self.pause_timer_task
            except asyncio.CancelledError:
                pass
        self.pause_timer_task = None

        await self._broadcast_timer()

        # 恢复主计时器
        if self.game.game_started and self.game.winner == 0:
            await self.start_timer()

    async def _pause_countdown(self):
        """暂停倒计时，到 0 自动恢复"""
        try:
            while self.pause_remaining > 0:
                await asyncio.sleep(1)
                self.pause_remaining -= 1
                await self._broadcast_timer()

            # 暂停时间到，自动恢复
            if self.paused:
                await self.broadcast({
                    "type": "admin_message",
                    "message": "⏸ 暂停时间到，比赛继续",
                })
                await self._do_unpause()

        except asyncio.CancelledError:
            pass

    # =============== 连接管理 ===============

    def _get_color(self, websocket):
        for c, ws in self.players.items():
            if ws == websocket:
                return c
        return None

    async def connect(self, websocket: WebSocket) -> dict:
        await websocket.accept()
        async with self.lock:
            if self._get_total_count() >= self.max_capacity:
                return {"role": "rejected", "color": 0, "message": "房间已满"}
            if 1 not in self.players:
                self.players[1] = websocket
                role = {"role": "black", "color": 1, "message": "你是黑方（先手）"}
                if 2 in self.players:
                    self.game.game_started = True
                return role
            elif 2 not in self.players:
                self.players[2] = websocket
                role = {"role": "white", "color": 2, "message": "你是白方（后手）"}
                self.game.game_started = True
                await self._notify_game_start()
                return role
            else:
                self.spectators.append(websocket)
                return {"role": "spectator", "color": 0, "message": "你正在观战"}

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            for color, ws in list(self.players.items()):
                if ws == websocket:
                    del self.players[color]
                    uname = self.usernames.get(websocket, "???")
                    name = "黑方" if color == 1 else "白方"
                    await self.broadcast({
                        "type": "player_left",
                        "message": f"{name}（{uname}）已断开连接",
                        "color": color,
                    })
                    self.game.game_started = False
                    await self.cancel_timer()
                    if self.paused:
                        await self._do_unpause()
                    self.pending_undo_from = None
                    break
            else:
                if websocket in self.spectators:
                    self.spectators.remove(websocket)
            self.usernames.pop(websocket, None)

    def set_username(self, websocket, username):
        self.usernames[websocket] = username
        if username not in self.scoreboard:
            self.scoreboard[username] = 0

    # =============== 落子 ===============

    async def handle_move(self, websocket, row, col):
        color = self._get_color(websocket)
        if color is None:
            await websocket.send_json({"type": "error", "message": "观战者不能落子"})
            return
        if not self.game.game_started:
            await websocket.send_json({"type": "error", "message": "等待对手加入..."})
            return
        if self.paused:
            await websocket.send_json({"type": "error", "message": "比赛暂停中"})
            return

        self.pending_undo_from = None
        result = self.game.place_stone(row, col, color)

        if result["success"]:
            if result["winner"] > 0:
                winner_ws = self.players.get(result["winner"])
                if winner_ws:
                    wn = self.usernames.get(winner_ws, "???")
                    self.scoreboard[wn] = self.scoreboard.get(wn, 0) + 1
                await self.cancel_timer()

            await self.broadcast({
                "type": "move", "row": row, "col": col, "color": color,
                "current_turn": self.game.current_turn,
                "winner": result["winner"], "message": result["message"],
            })

            if result["winner"] != 0:
                await self.broadcast_scoreboard()
                await self._broadcast_timer()
            elif self.game.game_started:
                await self.start_timer()
        else:
            await websocket.send_json({"type": "error", "message": result["message"]})

    # =============== 认负 ===============

    async def handle_resign(self, websocket):
        color = self._get_color(websocket)
        if color is None or not self.game.game_started:
            return
        if self.game.resign(color):
            await self.cancel_timer()
            if self.paused:
                await self._do_unpause()
            winner_color = 3 - color
            loser_name = self.usernames.get(websocket, "???")
            winner_ws = self.players.get(winner_color)
            if winner_ws:
                wn = self.usernames.get(winner_ws, "???")
                self.scoreboard[wn] = self.scoreboard.get(wn, 0) + 1
            await self.broadcast({
                "type": "game_over", "winner": winner_color, "reason": "resign",
                "message": f"{loser_name} 投子认负，{'黑' if winner_color == 1 else '白'}方获胜！",
            })
            await self._broadcast_timer()
            await self.broadcast_scoreboard()

    # =============== 申请悔棋 ===============

    async def handle_undo_request(self, websocket):
        color = self._get_color(websocket)
        if color is None or not self.game.game_started or self.game.winner != 0:
            return
        if not self.game.move_history:
            await websocket.send_json({"type": "error", "message": "没有可以悔的棋"})
            return

        self.pending_undo_from = color
        rname = self.usernames.get(websocket, "???")
        await self.cancel_timer()

        opp = self.players.get(3 - color)
        if opp:
            await opp.send_json({
                "type": "undo_request", "from_color": color,
                "from_name": rname, "message": f"{rname} 请求悔棋，是否同意？",
            })
        await websocket.send_json({"type": "admin_message", "message": "已发送悔棋请求，等待回应..."})
        for ws in self.spectators:
            try:
                await ws.send_json({"type": "admin_message", "message": f"{rname} 请求悔棋..."})
            except:
                pass

    async def handle_undo_response(self, websocket, accepted):
        if self.pending_undo_from is None:
            return
        rc = self._get_color(websocket)
        if rc is None or rc == self.pending_undo_from:
            return
        self.pending_undo_from = None

        if accepted:
            if self.game.undo():
                state = self.game.get_state()
                await self.broadcast({"type": "sync_state", **state, "message": "对手同意了悔棋"})
                await self.broadcast({"type": "admin_message", "message": "悔棋成功"})
                if self.game.game_started and self.game.winner == 0 and not self.paused:
                    await self.start_timer()
        else:
            rname = self.usernames.get(websocket, "???")
            await self.broadcast({"type": "admin_message", "message": f"{rname} 拒绝了悔棋请求"})
            if self.game.game_started and self.game.winner == 0 and not self.paused:
                await self.start_timer()

    # =============== 重置 ===============

    async def handle_reset(self, websocket):
        is_player = any(ws == websocket for ws in self.players.values())
        if not is_player:
            return
        self.game.reset()
        self.reset_timers()
        if len(self.players) == 2:
            self.game.game_started = True
        await self.cancel_timer()
        await self.broadcast({
            "type": "reset", "message": "棋局已重置",
            "game_started": self.game.game_started,
        })
        await self._broadcast_timer()
        if self.game.game_started:
            await self.start_timer()

    # =============== 管理员 ===============

    async def admin_swap_colors(self):
        p1, p2 = self.players.get(1), self.players.get(2)
        if p1 and p2:
            self.players[1] = p2; self.players[2] = p1
        elif p1:
            self.players[2] = p1; del self.players[1]
        elif p2:
            self.players[1] = p2; del self.players[2]

        self.game.reset()
        self.reset_timers()
        if len(self.players) == 2:
            self.game.game_started = True

        for color, ws in self.players.items():
            role = "black" if color == 1 else "white"
            await ws.send_json({
                "type": "role_assigned", "role": role, "color": color,
                "message": f"你是{'黑方（先手）' if color == 1 else '白方（后手）'}",
            })
        for ws in self.spectators:
            await ws.send_json({
                "type": "role_assigned", "role": "spectator", "color": 0,
                "message": "你正在观战",
            })

        await self.cancel_timer()
        await self.broadcast({
            "type": "reset", "message": "管理员交换了黑白方，棋局已重置",
            "game_started": self.game.game_started,
        })
        await self.broadcast_player_info()
        await self._broadcast_timer()
        if self.game.game_started:
            await self.start_timer()

    async def admin_undo(self):
        if self.game.undo():
            await self.cancel_timer()
            state = self.game.get_state()
            await self.broadcast({"type": "sync_state", **state, "message": "管理员执行了悔棋"})
            await self.broadcast({"type": "admin_message", "message": "管理员执行了悔棋"})
            if self.game.game_started and self.game.winner == 0 and not self.paused:
                await self.start_timer()

    async def admin_change_capacity(self, new_cap):
        self.max_capacity = max(2, new_cap)
        await self.broadcast({"type": "admin_message", "message": f"房间人数上限已更改为 {self.max_capacity} 人"})
        await self.broadcast_room_info()

    async def admin_change_timer(self, seconds):
        self.turn_time_limit = max(0, seconds)
        label = f"{self.turn_time_limit}秒" if self.turn_time_limit > 0 else "无限制"
        await self.broadcast({"type": "admin_message", "message": f"步时限制已更改为 {label}"})
        await self.broadcast({"type": "timer_setting",
                              "turn_time_limit": self.turn_time_limit,
                              "total_time_setting": self.total_time_setting})
        if self.game.game_started and self.game.winner == 0 and not self.paused:
            await self.start_timer()

    async def admin_change_total_time(self, seconds):
        """更改总时间设置（同时重置双方剩余总时间）"""
        self.total_time_setting = max(0, seconds)
        self.total_time = {1: self.total_time_setting, 2: self.total_time_setting}
        label = f"{self.total_time_setting // 60}分{self.total_time_setting % 60}秒" if self.total_time_setting > 0 else "无限制"
        await self.broadcast({"type": "admin_message", "message": f"总时间已更改为 {label}（双方已重置）"})
        await self.broadcast({"type": "timer_setting",
                              "turn_time_limit": self.turn_time_limit,
                              "total_time_setting": self.total_time_setting})
        await self._broadcast_timer()

    async def admin_change_pause_duration(self, seconds):
        self.pause_duration = max(30, seconds)
        label = f"{self.pause_duration // 60}分{self.pause_duration % 60}秒"
        await self.broadcast({"type": "admin_message", "message": f"暂停时长已更改为 {label}"})

    async def admin_clear_scores(self):
        self.scoreboard = {}
        for ws, uname in self.usernames.items():
            self.scoreboard[uname] = 0
        await self.broadcast_scoreboard()
        await self.broadcast({"type": "admin_message", "message": "积分已清空"})

    async def admin_swap_spectator_player(self, spectator_index, player_color):
        if spectator_index < 0 or spectator_index >= len(self.spectators):
            return
        if player_color not in self.players:
            return
        spec_ws = self.spectators[spectator_index]
        player_ws = self.players[player_color]
        self.players[player_color] = spec_ws
        self.spectators[spectator_index] = player_ws

        role = "black" if player_color == 1 else "white"
        await spec_ws.send_json({
            "type": "role_assigned", "role": role, "color": player_color,
            "message": f"你是{'黑方（先手）' if player_color == 1 else '白方（后手）'}",
        })
        await player_ws.send_json({
            "type": "role_assigned", "role": "spectator", "color": 0,
            "message": "你现在是观战者",
        })

        self.game.reset()
        self.reset_timers()
        if len(self.players) == 2:
            self.game.game_started = True
        await self.cancel_timer()
        await self.broadcast({
            "type": "reset", "message": "管理员交换了棋手和观战者，棋局已重置",
            "game_started": self.game.game_started,
        })
        await self.broadcast_player_info()
        await self._broadcast_timer()
        if self.game.game_started:
            await self.start_timer()

    # =============== 广播工具 ===============

    async def broadcast(self, message):
        dead = []
        for color, ws in list(self.players.items()):
            try:
                await ws.send_json(message)
            except:
                pass
        for ws in self.spectators:
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.spectators.remove(ws)

    async def broadcast_scoreboard(self):
        ss = sorted(self.scoreboard.items(), key=lambda x: -x[1])
        await self.broadcast({"type": "scoreboard", "scores": ss})

    async def broadcast_player_info(self):
        pi = {}
        for color, ws in self.players.items():
            pi[color] = self.usernames.get(ws, "???")
        si = [{"index": i, "name": self.usernames.get(ws, "???")} for i, ws in enumerate(self.spectators)]
        await self.broadcast({"type": "player_info", "players": pi, "spectators": si})

    async def broadcast_room_info(self):
        await self.broadcast({
            "type": "room_info",
            "max_capacity": self.max_capacity,
            "current_count": self._get_total_count(),
        })

    async def _notify_game_start(self):
        await self.broadcast({
            "type": "game_start",
            "message": "双方已就位，游戏开始！黑方先手。",
        })

    def get_online_count(self):
        return {"players": len(self.players), "spectators": len(self.spectators)}


manager = ConnectionManager()


# ============================================================
# WebSocket
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    role_info = await manager.connect(websocket)
    if role_info["role"] == "rejected":
        await websocket.send_json({"type": "rejected", "message": role_info["message"]})
        await websocket.close()
        return

    try:
        await websocket.send_json({"type": "role_assigned", **role_info})
        state = manager.game.get_state()
        await websocket.send_json({"type": "sync_state", **state})
        await websocket.send_json({"type": "online_count", **manager.get_online_count()})

        ss = sorted(manager.scoreboard.items(), key=lambda x: -x[1])
        await websocket.send_json({"type": "scoreboard", "scores": ss})
        await websocket.send_json({
            "type": "room_info",
            "max_capacity": manager.max_capacity,
            "current_count": manager._get_total_count(),
        })
        await websocket.send_json({
            "type": "timer_setting",
            "turn_time_limit": manager.turn_time_limit,
            "total_time_setting": manager.total_time_setting,
        })
        # 发送当前计时状态
        await websocket.send_json({
            "type": "timer_sync",
            "turn_remaining": manager.turn_remaining if manager.turn_time_limit > 0 else -1,
            "turn_total": manager.turn_time_limit,
            "total_time": {str(k): v for k, v in manager.total_time.items()},
            "total_time_setting": manager.total_time_setting,
            "current_turn": manager.game.current_turn,
            "paused": manager.paused,
            "pause_by": manager.pause_by,
            "pause_remaining": manager.pause_remaining,
            "pause_counts": {str(k): v for k, v in manager.pause_counts.items()},
        })

        while True:
            data = await websocket.receive_json()
            t = data.get("type")

            if t == "set_username":
                manager.set_username(websocket, data["username"])
                await manager.broadcast({"type": "online_count", **manager.get_online_count()})
                await manager.broadcast_player_info()
                await manager.broadcast_scoreboard()
                if (manager.game.game_started and manager.game.winner == 0
                        and len(manager.game.move_history) == 0
                        and len(manager.players) == 2
                        and not manager.paused):
                    await manager.start_timer()

            elif t == "move":
                await manager.handle_move(websocket, data["row"], data["col"])
            elif t == "reset":
                await manager.handle_reset(websocket)
            elif t == "resign":
                await manager.handle_resign(websocket)
            elif t == "undo_request":
                await manager.handle_undo_request(websocket)
            elif t == "undo_response":
                await manager.handle_undo_response(websocket, data.get("accepted", False))
            elif t == "pause":
                await manager.handle_pause(websocket)
            elif t == "unpause":
                await manager.handle_unpause(websocket)

            # 管理员
            elif t == "admin_swap_colors":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_swap_colors()
            elif t == "admin_undo":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_undo()
            elif t == "admin_change_capacity":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_change_capacity(int(data.get("capacity", 3)))
            elif t == "admin_change_timer":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_change_timer(int(data.get("seconds", 20)))
            elif t == "admin_change_total_time":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_change_total_time(int(data.get("seconds", 300)))
            elif t == "admin_change_pause_duration":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_change_pause_duration(int(data.get("seconds", 300)))
            elif t == "admin_clear_scores":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_clear_scores()
            elif t == "admin_swap_spectator":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_swap_spectator_player(
                        int(data.get("spectator_index", 0)),
                        int(data.get("player_color", 1)),
                    )

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
        await manager.broadcast({"type": "online_count", **manager.get_online_count()})
        await manager.broadcast_player_info()


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    print("🎮 五子棋服务器启动中...")
    print("🌐 打开浏览器访问: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
