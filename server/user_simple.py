#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
user.py
- Tk の ↑←→↓ ボタン/矢印キーで {"t":"cmd","v":"UP|DOWN|LEFT|RIGHT"} を DataChannel '#ctrl' に送信
- DataChannel '#state' を受信して、キャンバス上のロボット位置/向きを更新
- .env 例:
    SORA_SIGNALING_URLS=ws://sora2.uclab.jp:5000/signaling
    SORA_CHANNEL_ID=sora
    SORA_CTRL_LABEL="#ctrl"
    SORA_STATE_LABEL="#state"
"""
import json, os, math, queue
from threading import Event
from typing import Any, Optional, Callable

import tkinter as tk
from dotenv import load_dotenv
from sora_sdk import Sora, SoraConnection, SoraSignalingErrorCode


# ========== 描画パラメータ ==========
CANVAS_W = 480
CANVAS_H = 480
GRID_STEP = 40
ROBOT_R = 16           # 機体サイズ
ROBOT_COLOR = "#2a6ef7"
TRAIL_COLOR = "#999999" # 軌跡の色
TRAIL_WIDTH = 12         # 軌跡の太さ


# ========== Sora Messaging ==========
class Messaging:
    def __init__(
        self,
        signaling_urls: list[str],
        channel_id: str,
        data_channels: list[dict[str, Any]],
        metadata: Optional[dict[str, Any]] = None,
        app_on_message: Optional[Callable[[str, bytes], None]] = None,
    ):
        self._data_channels = data_channels
        self._app_on_message = app_on_message

        self._sora = Sora()
        self._conn: SoraConnection = self._sora.create_connection(
            signaling_urls=signaling_urls,
            role="sendrecv",
            channel_id=channel_id,
            metadata=metadata,
            audio=False,
            video=True,                   # あなたの環境では必須
            data_channels=self._data_channels,
            data_channel_signaling=True,
        )
        self._connection_id: Optional[str] = None
        self._connected = Event()
        self._closed = Event()
        self._default_connection_timeout_s = 10.0

        # ラベルごとの ready 状態
        self._sendable = {dc["label"]: False for dc in data_channels}

        # コールバック
        self._conn.on_set_offer = self._on_set_offer
        self._conn.on_notify = self._on_notify
        self._conn.on_data_channel = self._on_data_channel
        self._conn.on_message = self._on_message
        self._conn.on_disconnect = self._on_disconnect

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def data_channel_ready(self) -> bool:
        # どれか1本でも open していれば True
        return any(self._sendable.values())

    def connect(self):
        print("[SIG] connecting ...")
        self._conn.connect()
        assert self._connected.wait(self._default_connection_timeout_s), "Could not connect to Sora."

    def disconnect(self):
        print("[SIG] disconnecting ...")
        self._conn.disconnect()

    def send_json(self, label: str, obj: dict):
        if not self._sendable.get(label, False):
            print(f"[TX] drop (dc '{label}' not ready):", obj)
            return
        b = json.dumps(obj).encode("utf-8")
        head = b[:64].decode("utf-8", errors="replace")
        print(f"[TX] label={label}, bytes={len(b)}, head={head!r}")
        self._conn.send_data_channel(label, b)

    # --- handlers ---
    def _on_set_offer(self, raw: str):
        m = json.loads(raw)
        if m.get("type") == "offer":
            self._connection_id = m.get("connection_id")
            print(f"[SIG] set_offer: connection_id={self._connection_id}")

    def _on_notify(self, raw: str):
        m = json.loads(raw)
        if (
            m.get("type") == "notify"
            and m.get("event_type") == "connection.created"
            and m.get("connection_id") == self._connection_id
        ):
            print(f"[SIG] connected: connection_id={self._connection_id}")
            print(f"Connected Sora: connection_id={self._connection_id}")
            self._connected.set()

    def _on_data_channel(self, label: str):
        if label in self._sendable:
            self._sendable[label] = True
        print(f"[DC] ready: label={label}")

    def _on_message(self, label: str, data: bytes):
        try:
            txt = data.decode("utf-8", errors="replace")
        except Exception:
            txt = repr(data)
        print(f"[RX] label={label}, data={txt}")
        if self._app_on_message:
            self._app_on_message(label, data)

    def _on_disconnect(self, code: SoraSignalingErrorCode, msg: str):
        print(f"[SIG] disconnected: error_code='{code}' message='{msg}'")
        self._connected.clear()
        self._closed.set()


# ========== ユーザーUI ==========
class UserApp:
    def __init__(self, root: tk.Tk, messaging: Messaging, ctrl_label: str, state_label: str):
        self.root = root
        self.msg = messaging                 # ← ここで None ではなく実体を渡す
        self.ctrl = ctrl_label
        self.state = state_label

        root.title("User UI  (#ctrl send / #state recv)")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # 上段：ステータス表示
        self.status = tk.StringVar(value="DC: CONNECTING...")
        self.pose   = tk.StringVar(value="x=?, y=?, θ=?")
        top = tk.Frame(root)
        # UI更新用のキュー
        self.ui_queue = queue.Queue()

        top.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        tk.Label(top, textvariable=self.status).pack(anchor="w")
        tk.Label(top, textvariable=self.pose, fg="#333").pack(anchor="w")

        # 中段：Canvas にロボット描画
        self.canvas = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H, bg="white",
                                highlightthickness=1, highlightbackground="#d0d3da")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)
        self._draw_grid()

        # 位置（state 未受信の初期値）
        self.x = CANVAS_W * 0.5
        self.y = CANVAS_H * 0.5
        self.theta = 0.0
        self.robot_id = self.canvas.create_polygon(self._robot_points(),
                                                   fill=ROBOT_COLOR, outline="#123", width=1.5)
        # 軌跡用
        self.trail_points = [(self.x, self.y)]
        self.trail_id = None
        self._draw_trail()

        # 下段：ボタン
        bottom = tk.Frame(root)
        bottom.grid(row=2, column=0, pady=6)
        tk.Button(bottom, text="↑", width=6, command=lambda: self._send_cmd("UP")).grid(row=0, column=1, padx=4, pady=4)
        tk.Button(bottom, text="←", width=6, command=lambda: self._send_cmd("LEFT")).grid(row=1, column=0, padx=4, pady=4)
        tk.Button(bottom, text="→", width=6, command=lambda: self._send_cmd("RIGHT")).grid(row=1, column=2, padx=4, pady=4)
        tk.Button(bottom, text="↓", width=6, command=lambda: self._send_cmd("DOWN")).grid(row=2, column=1, padx=4, pady=4)

        # 矢印キー
        root.bind("<Up>",    lambda e: self._send_cmd("UP"))
        root.bind("<Left>",  lambda e: self._send_cmd("LEFT"))
        root.bind("<Right>", lambda e: self._send_cmd("RIGHT"))
        root.bind("<Down>",  lambda e: self._send_cmd("DOWN"))

        # DC 状態を定期更新
        self._tick()

    # ---- UI helpers ----
    def _tick(self):
        if self.msg.closed:
            self.status.set("DC: CLOSED")
            self._set_buttons_state(False)
        elif self.msg.data_channel_ready:
            self.status.set("DC: OPEN")
            self._set_buttons_state(True)
        else:
            self.status.set("DC: CONNECTING...")
            self._set_buttons_state(False)
        # UI更新キューを処理
        self._process_ui_queue()
        self.root.after(200, self._tick)

    def _set_buttons_state(self, enable: bool):
        state = tk.NORMAL if enable else tk.DISABLED
        # ボタンは親フレームの children から探す
        for child in self.root.grid_slaves(row=2, column=0):
            for w in child.grid_slaves():
                if isinstance(w, tk.Button):
                    w.config(state=state)

    def _send_cmd(self, direction: str):
        self.msg.send_json(self.ctrl, {"t": "cmd", "v": direction})

    def _draw_grid(self):
        for x in range(0, CANVAS_W + 1, GRID_STEP):
            self.canvas.create_line(x, 0, x, CANVAS_H, fill="#eef1f6")
        for y in range(0, CANVAS_H + 1, GRID_STEP):
            self.canvas.create_line(0, y, CANVAS_W, y, fill="#eef1f6")
        self.canvas.create_rectangle(1, 1, CANVAS_W - 1, CANVAS_H - 1, outline="#d0d3da")

    def _robot_points(self):
        # 三角形の向き: theta [rad]。先端 + 左 + 右 の3点
        r = ROBOT_R
        tip = (self.x + math.cos(self.theta) * r,  self.y + math.sin(self.theta) * r)
        left = (self.x + math.cos(self.theta + 2.5) * r * 0.75,
                self.y + math.sin(self.theta + 2.5) * r * 0.75)
        right = (self.x + math.cos(self.theta - 2.5) * r * 0.75,
                 self.y + math.sin(self.theta - 2.5) * r * 0.75)
        return (*tip, *left, *right)

    def _redraw_robot(self):
        self.canvas.coords(self.robot_id, *self._robot_points())

    def _draw_trail(self):
        """ロボットの軌跡を描画する"""
        if self.trail_id:
            self.canvas.delete(self.trail_id)

        if len(self.trail_points) < 2:
            return

        # 軌跡の線を描画し、IDを保存
        self.trail_id = self.canvas.create_line(
            self.trail_points, fill=TRAIL_COLOR, width=TRAIL_WIDTH, capstyle=tk.ROUND, smooth=True)
        # ロボットを最前面に表示
        self.canvas.tag_raise(self.robot_id)

    def _process_ui_queue(self):
        """UI更新キューを処理して、画面を更新する (メインスreadで実行)"""
        while not self.ui_queue.empty():
            try:
                msg = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if msg['type'] == 'state_update':
                st = msg['data']
                # x, y, theta を更新
                x = st.get("x", self.x)
                y = st.get("y", self.y)
                th = st.get("theta", self.theta)
                # 数値化
                try:
                    self.x = float(x)
                    self.y = float(y)
                    self.theta = float(th)
                except Exception:
                    continue # 数値化に失敗したらスキップ

                # 座標が変化した場合のみ軌跡の点を追加
                if self.x != self.trail_points[-1][0] or self.y != self.trail_points[-1][1]:
                    self.trail_points.append((self.x, self.y))

                # 画面更新
                self.pose.set(f"x={self.x:.1f}, y={self.y:.1f}, θ={self.theta:.2f}")
                self._redraw_robot()
                self._draw_trail()

    # ---- 受信処理（Messaging から呼ばれる）----
    def on_state(self, label: str, data: bytes):
        if label != self.state:
            return
        try:
            st = json.loads(data.decode("utf-8"))
        except Exception:
            return  # 不正なデータの場合は何もしない
        # UI更新は直接行わず、キューにタスクを投入する
        self.ui_queue.put({'type': 'state_update', 'data': st})

    def _on_close(self):
        try:
            self.msg.disconnect()
        finally:
            self.root.destroy()


# ========== エントリポイント ==========
def main():
    load_dotenv()

    raw = os.getenv("SORA_SIGNALING_URLS")
    if not raw:
        raise ValueError("SORA_SIGNALING_URLS unset")
    urls = [u.strip() for u in raw.split(",") if u.strip()]

    chid  = os.getenv("SORA_CHANNEL_ID") or "sora"
    ctrl  = os.getenv("SORA_CTRL_LABEL", "#ctrl")
    state = os.getenv("SORA_STATE_LABEL", "#state")
    meta  = json.loads(os.getenv("SORA_METADATA")) if os.getenv("SORA_METADATA") else None

    # 2本の DataChannel を静的定義
    dcs = [
        {"label": ctrl,  "direction": "sendrecv"},
        {"label": state, "direction": "sendrecv"},
    ]

    # ❶ Messaging を先に作る
    msg = Messaging(urls, chid, dcs, meta, app_on_message=None)

    # ❷ UI を作成し、Messaging を渡す
    root = tk.Tk()
    app = UserApp(root, messaging=msg, ctrl_label=ctrl, state_label=state)

    # ❸ 受信コールバックをUIにバインド
    msg._app_on_message = app.on_state

    # ❹ 接続してUI開始
    msg.connect()
    root.mainloop()


if __name__ == "__main__":
    main()
