"""
五子棋在线对战服务器
功能：用户名、积分榜、管理员控制面板
"""

import json
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

# ============================================================
# 配置
# ============================================================

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

    def undo(self):
        """悔棋：撤销最后一步"""
        if not self.move_history:
            return False
        row, col, color = self.move_history.pop()
        self.board[row][col] = 0
        self.current_turn = color  # 回到落子的那个人
        self.winner = 0
        return True

    def _check_win(self, row, col, color):
        for dr, dc in [(0,1),(1,0),(1,1),(1,-1)]:
            count = 1
            r, c = row+dr, col+dc
            while 0<=r<15 and 0<=c<15 and self.board[r][c]==color:
                count+=1; r+=dr; c+=dc
            r, c = row-dr, col-dc
            while 0<=r<15 and 0<=c<15 and self.board[r][c]==color:
                count+=1; r-=dr; c-=dc
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
        self.players = {}        # {1: ws, 2: ws}
        self.spectators = []     # [ws, ...]
        self.usernames = {}      # {ws: "用户名"}
        self.scoreboard = {}     # {"用户名": 分数}
        self.max_capacity = 3    # 默认：2棋手 + 1观众（可设为更大值）
        self.lock = asyncio.Lock()

    def _get_total_count(self):
        return len(self.players) + len(self.spectators)

    async def connect(self, websocket: WebSocket) -> dict:
        await websocket.accept()
        async with self.lock:
            # 检查房间是否已满
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
                    break
            else:
                if websocket in self.spectators:
                    self.spectators.remove(websocket)

            self.usernames.pop(websocket, None)

    def set_username(self, websocket, username):
        self.usernames[websocket] = username
        if username not in self.scoreboard:
            self.scoreboard[username] = 0

    async def handle_move(self, websocket, row, col):
        color = None
        for c, ws in self.players.items():
            if ws == websocket:
                color = c
                break
        if color is None:
            await websocket.send_json({"type": "error", "message": "观战者不能落子"})
            return
        if not self.game.game_started:
            await websocket.send_json({"type": "error", "message": "等待对手加入..."})
            return

        result = self.game.place_stone(row, col, color)
        if result["success"]:
            # 如果有人赢了，更新积分
            if result["winner"] > 0:
                winner_ws = self.players.get(result["winner"])
                if winner_ws:
                    winner_name = self.usernames.get(winner_ws, "???")
                    self.scoreboard[winner_name] = self.scoreboard.get(winner_name, 0) + 1

            await self.broadcast({
                "type": "move", "row": row, "col": col, "color": color,
                "current_turn": self.game.current_turn,
                "winner": result["winner"], "message": result["message"],
            })

            if result["winner"] != 0:
                await self.broadcast_scoreboard()
        else:
            await websocket.send_json({"type": "error", "message": result["message"]})

    async def handle_reset(self, websocket):
        is_player = any(ws == websocket for ws in self.players.values())
        if not is_player:
            return
        self.game.reset()
        if len(self.players) == 2:
            self.game.game_started = True
        await self.broadcast({
            "type": "reset", "message": "棋局已重置",
            "game_started": self.game.game_started,
        })

    # ---- 管理员操作 ----

    async def admin_swap_colors(self):
        """交换黑白方"""
        p1 = self.players.get(1)
        p2 = self.players.get(2)
        if p1 and p2:
            self.players[1] = p2
            self.players[2] = p1
        elif p1:
            self.players[2] = p1
            del self.players[1]
        elif p2:
            self.players[1] = p2
            del self.players[2]

        self.game.reset()
        if len(self.players) == 2:
            self.game.game_started = True

        # 通知每个人新角色
        for color, ws in self.players.items():
            role = "black" if color == 1 else "white"
            uname = self.usernames.get(ws, "???")
            await ws.send_json({
                "type": "role_assigned",
                "role": role, "color": color,
                "message": f"你是{'黑方（先手）' if color==1 else '白方（后手）'}",
            })

        for ws in self.spectators:
            await ws.send_json({
                "type": "role_assigned",
                "role": "spectator", "color": 0,
                "message": "你正在观战",
            })

        await self.broadcast({
            "type": "reset", "message": "管理员交换了黑白方，棋局已重置",
            "game_started": self.game.game_started,
        })
        await self.broadcast_player_info()

    async def admin_undo(self):
        """悔棋"""
        if self.game.undo():
            state = self.game.get_state()
            await self.broadcast({
                "type": "sync_state",
                **state,
                "message": "管理员执行了悔棋",
            })
            await self.broadcast({"type": "admin_message", "message": "管理员执行了悔棋"})

    async def admin_change_capacity(self, new_cap):
        """更改房间人数限制"""
        if new_cap < 2:
            new_cap = 2
        self.max_capacity = new_cap
        await self.broadcast({
            "type": "admin_message",
            "message": f"房间人数上限已更改为 {new_cap} 人",
        })
        await self.broadcast_room_info()

    async def admin_clear_scores(self):
        """清空积分"""
        self.scoreboard = {}
        # 重新为在线用户初始化 0 分
        for ws, uname in self.usernames.items():
            self.scoreboard[uname] = 0
        await self.broadcast_scoreboard()
        await self.broadcast({"type": "admin_message", "message": "积分已清空"})

    async def admin_swap_spectator_player(self, spectator_index, player_color):
        """交换观战者和棋手"""
        if spectator_index < 0 or spectator_index >= len(self.spectators):
            return
        if player_color not in self.players:
            return

        spec_ws = self.spectators[spectator_index]
        player_ws = self.players[player_color]

        # 交换
        self.players[player_color] = spec_ws
        self.spectators[spectator_index] = player_ws

        # 通知新棋手
        role = "black" if player_color == 1 else "white"
        await spec_ws.send_json({
            "type": "role_assigned",
            "role": role, "color": player_color,
            "message": f"你是{'黑方（先手）' if player_color==1 else '白方（后手）'}",
        })

        # 通知新观众
        await player_ws.send_json({
            "type": "role_assigned",
            "role": "spectator", "color": 0,
            "message": "你现在是观战者",
        })

        # 重置棋局
        self.game.reset()
        if len(self.players) == 2:
            self.game.game_started = True

        await self.broadcast({
            "type": "reset", "message": "管理员交换了棋手和观战者，棋局已重置",
            "game_started": self.game.game_started,
        })
        await self.broadcast_player_info()

    # ---- 广播工具 ----

    async def broadcast(self, message):
        dead = []
        for color, ws in list(self.players.items()):
            try: await ws.send_json(message)
            except: pass
        for ws in self.spectators:
            try: await ws.send_json(message)
            except: dead.append(ws)
        for ws in dead:
            self.spectators.remove(ws)

    async def broadcast_scoreboard(self):
        # 按分数排序
        sorted_scores = sorted(self.scoreboard.items(), key=lambda x: -x[1])
        await self.broadcast({
            "type": "scoreboard",
            "scores": sorted_scores,
        })

    async def broadcast_player_info(self):
        """广播当前玩家和观众信息"""
        players_info = {}
        for color, ws in self.players.items():
            players_info[color] = self.usernames.get(ws, "???")

        spectators_info = []
        for i, ws in enumerate(self.spectators):
            spectators_info.append({
                "index": i,
                "name": self.usernames.get(ws, "???"),
            })

        await self.broadcast({
            "type": "player_info",
            "players": players_info,
            "spectators": spectators_info,
        })

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
# WebSocket 路由
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    role_info = await manager.connect(websocket)

    # 房间已满，拒绝连接
    if role_info["role"] == "rejected":
        await websocket.send_json({"type": "rejected", "message": role_info["message"]})
        await websocket.close()
        return

    try:
        await websocket.send_json({"type": "role_assigned", **role_info})

        state = manager.game.get_state()
        await websocket.send_json({"type": "sync_state", **state})
        await websocket.send_json({"type": "online_count", **manager.get_online_count()})

        # 发送积分榜
        sorted_scores = sorted(manager.scoreboard.items(), key=lambda x: -x[1])
        await websocket.send_json({"type": "scoreboard", "scores": sorted_scores})

        # 发送房间信息
        await websocket.send_json({
            "type": "room_info",
            "max_capacity": manager.max_capacity,
            "current_count": manager._get_total_count(),
        })

        # 持续接收消息
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "set_username":
                manager.set_username(websocket, data["username"])
                await manager.broadcast({"type": "online_count", **manager.get_online_count()})
                await manager.broadcast_player_info()
                await manager.broadcast_scoreboard()

            elif msg_type == "move":
                await manager.handle_move(websocket, data["row"], data["col"])

            elif msg_type == "reset":
                await manager.handle_reset(websocket)

            # ---- 管理员操作 ----
            elif msg_type == "admin_swap_colors":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_swap_colors()
                else:
                    await websocket.send_json({"type": "error", "message": "管理员密码错误"})

            elif msg_type == "admin_undo":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_undo()
                else:
                    await websocket.send_json({"type": "error", "message": "管理员密码错误"})

            elif msg_type == "admin_change_capacity":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_change_capacity(int(data.get("capacity", 3)))
                else:
                    await websocket.send_json({"type": "error", "message": "管理员密码错误"})

            elif msg_type == "admin_clear_scores":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_clear_scores()
                else:
                    await websocket.send_json({"type": "error", "message": "管理员密码错误"})

            elif msg_type == "admin_swap_spectator":
                if data.get("password") == ADMIN_PASSWORD:
                    await manager.admin_swap_spectator_player(
                        int(data.get("spectator_index", 0)),
                        int(data.get("player_color", 1)),
                    )
                else:
                    await websocket.send_json({"type": "error", "message": "管理员密码错误"})

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
