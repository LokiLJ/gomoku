"""
五子棋在线对战服务器
使用 FastAPI + WebSocket 实现实时对战
"""

import json
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

# ============================================================
# 游戏状态管理
# ============================================================

class GomokuGame:
    """五子棋游戏核心逻辑"""

    def __init__(self):
        self.reset()

    def reset(self):
        """重置棋局"""
        self.board = [[0] * 15 for _ in range(15)]  # 0=空, 1=黑, 2=白
        self.current_turn = 1  # 1=黑方先手
        self.winner = 0        # 0=未结束, 1=黑胜, 2=白胜
        self.move_history = [] # 记录所有落子 [(row, col, color), ...]
        self.game_started = False

    def place_stone(self, row: int, col: int, color: int) -> dict:
        """
        落子
        返回: {"success": bool, "winner": int, "message": str}
        """
        # 检查是否轮到该玩家
        if color != self.current_turn:
            return {"success": False, "winner": 0, "message": "还没轮到你"}

        # 检查游戏是否已结束
        if self.winner != 0:
            return {"success": False, "winner": self.winner, "message": "游戏已结束"}

        # 检查位置是否合法
        if not (0 <= row < 15 and 0 <= col < 15):
            return {"success": False, "winner": 0, "message": "位置超出棋盘"}

        if self.board[row][col] != 0:
            return {"success": False, "winner": 0, "message": "该位置已有棋子"}

        # 落子
        self.board[row][col] = color
        self.move_history.append((row, col, color))

        # 检查是否获胜
        if self._check_win(row, col, color):
            self.winner = color
            return {"success": True, "winner": color, "message": f"{'黑' if color == 1 else '白'}方获胜！"}

        # 检查是否平局（棋盘满了）
        if len(self.move_history) >= 225:
            return {"success": True, "winner": -1, "message": "平局！"}

        # 切换回合
        self.current_turn = 3 - color  # 1->2, 2->1
        return {"success": True, "winner": 0, "message": ""}

    def _check_win(self, row: int, col: int, color: int) -> bool:
        """检查落子后是否形成五连"""
        directions = [
            (0, 1),   # 水平 →
            (1, 0),   # 垂直 ↓
            (1, 1),   # 对角线 ↘
            (1, -1),  # 对角线 ↙
        ]

        for dr, dc in directions:
            count = 1  # 包含当前落子

            # 正方向计数
            r, c = row + dr, col + dc
            while 0 <= r < 15 and 0 <= c < 15 and self.board[r][c] == color:
                count += 1
                r += dr
                c += dc

            # 反方向计数
            r, c = row - dr, col - dc
            while 0 <= r < 15 and 0 <= c < 15 and self.board[r][c] == color:
                count += 1
                r -= dr
                c -= dc

            if count >= 5:
                return True

        return False

    def get_state(self) -> dict:
        """获取完整游戏状态（用于新连接的玩家同步）"""
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
    """管理所有 WebSocket 连接"""

    def __init__(self):
        self.game = GomokuGame()
        self.players = {}       # {1: websocket, 2: websocket}  黑方=1, 白方=2
        self.spectators = []    # [websocket, ...]
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> dict:
        """
        新连接加入
        返回: {"role": "black"/"white"/"spectator", "color": int}
        """
        await websocket.accept()

        async with self.lock:
            if 1 not in self.players:
                self.players[1] = websocket
                role = {"role": "black", "color": 1, "message": "你是黑方（先手）"}
                # 检查是否两人都到齐了
                if 2 in self.players:
                    self.game.game_started = True
                return role

            elif 2 not in self.players:
                self.players[2] = websocket
                role = {"role": "white", "color": 2, "message": "你是白方（后手）"}
                self.game.game_started = True
                # 通知黑方游戏开始
                await self._notify_game_start()
                return role

            else:
                self.spectators.append(websocket)
                return {"role": "spectator", "color": 0, "message": "你正在观战"}

    async def disconnect(self, websocket: WebSocket):
        """处理断开连接"""
        async with self.lock:
            # 检查是否是玩家断开
            for color, ws in list(self.players.items()):
                if ws == websocket:
                    del self.players[color]
                    # 通知其他人
                    name = "黑方" if color == 1 else "白方"
                    await self.broadcast({
                        "type": "player_left",
                        "message": f"{name}已断开连接，等待重新连接...",
                        "color": color,
                    })
                    self.game.game_started = False
                    return

            # 检查是否是观众断开
            if websocket in self.spectators:
                self.spectators.remove(websocket)

    async def handle_move(self, websocket: WebSocket, row: int, col: int):
        """处理落子请求"""
        # 确认是哪个玩家
        color = None
        for c, ws in self.players.items():
            if ws == websocket:
                color = c
                break

        if color is None:
            await websocket.send_json({
                "type": "error",
                "message": "观战者不能落子"
            })
            return

        if not self.game.game_started:
            await websocket.send_json({
                "type": "error",
                "message": "等待对手加入..."
            })
            return

        # 执行落子
        result = self.game.place_stone(row, col, color)

        if result["success"]:
            # 广播落子给所有人
            await self.broadcast({
                "type": "move",
                "row": row,
                "col": col,
                "color": color,
                "current_turn": self.game.current_turn,
                "winner": result["winner"],
                "message": result["message"],
            })
        else:
            # 只通知当前玩家落子失败
            await websocket.send_json({
                "type": "error",
                "message": result["message"],
            })

    async def handle_reset(self, websocket: WebSocket):
        """处理重置棋局请求"""
        # 只有玩家可以重置
        is_player = any(ws == websocket for ws in self.players.values())
        if not is_player:
            return

        self.game.reset()
        if len(self.players) == 2:
            self.game.game_started = True

        await self.broadcast({
            "type": "reset",
            "message": "棋局已重置",
            "game_started": self.game.game_started,
        })

    async def broadcast(self, message: dict):
        """广播消息给所有连接的人"""
        dead_spectators = []

        # 发给玩家
        for color, ws in list(self.players.items()):
            try:
                await ws.send_json(message)
            except Exception:
                pass  # 玩家断线会在 disconnect 中处理

        # 发给观众
        for ws in self.spectators:
            try:
                await ws.send_json(message)
            except Exception:
                dead_spectators.append(ws)

        # 清理断线的观众
        for ws in dead_spectators:
            self.spectators.remove(ws)

    async def _notify_game_start(self):
        """通知所有人游戏开始"""
        await self.broadcast({
            "type": "game_start",
            "message": "双方已就位，游戏开始！黑方先手。",
        })

    def get_online_count(self) -> dict:
        """获取在线人数"""
        return {
            "players": len(self.players),
            "spectators": len(self.spectators),
        }


# ============================================================
# 创建全局实例
# ============================================================

manager = ConnectionManager()


# ============================================================
# 路由
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 主入口"""
    role_info = await manager.connect(websocket)

    try:
        # 发送角色信息
        await websocket.send_json({
            "type": "role_assigned",
            **role_info,
        })

        # 发送当前游戏状态（用于断线重连或观众同步）
        state = manager.game.get_state()
        await websocket.send_json({
            "type": "sync_state",
            **state,
        })

        # 发送在线人数
        await websocket.send_json({
            "type": "online_count",
            **manager.get_online_count(),
        })

        # 广播更新后的在线人数
        await manager.broadcast({
            "type": "online_count",
            **manager.get_online_count(),
        })

        # 持续接收消息
        while True:
            data = await websocket.receive_json()

            if data["type"] == "move":
                await manager.handle_move(websocket, data["row"], data["col"])

            elif data["type"] == "reset":
                await manager.handle_reset(websocket)

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
        # 广播更新后的在线人数
        await manager.broadcast({
            "type": "online_count",
            **manager.get_online_count(),
        })


# 挂载静态文件（前端页面）
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """访问首页时返回前端页面"""
    return FileResponse("static/index.html")


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    import uvicorn
    print("🎮 五子棋服务器启动中...")
    print("🌐 打开浏览器访问: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
