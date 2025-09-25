#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manager.py
- '#ctrl' を受信して位置(x,y,theta)を更新
- 更新後 '#state' を {"t":"state","x":..,"y":..,"theta":..} で返信
- .env は user.py と同じ
"""
import json, os, time, math
from threading import Event
from typing import Any, Optional
from dotenv import load_dotenv
from sora_sdk import Sora, SoraConnection, SoraSignalingErrorCode

class ManagerNode:
    def __init__(self, signaling_urls, channel_id, ctrl_label, state_label, metadata=None):
        self.ctrl = ctrl_label
        self.state = state_label

        self._sora = Sora()
        self._conn: SoraConnection = self._sora.create_connection(
            signaling_urls=signaling_urls,
            role="sendrecv",
            channel_id=channel_id,
            metadata=metadata,
            audio=False,
            video=True,                 # 環境要件：メディア必須
            data_channel_signaling=True,
            data_channels=[
                {"label": self.ctrl,  "direction": "sendrecv"},
                {"label": self.state, "direction": "sendrecv"},
            ],
        )
        self._connection_id: Optional[str] = None
        self._connected = Event()
        self._closed = Event()
        self._ready = {self.ctrl: False, self.state: False}

        # pose
        self.x, self.y, self.theta = 240, 240, 0.0
        self.step = 20
        self.max_w, self.max_h = 480, 480

        self._conn.on_set_offer = self._on_set_offer
        self._conn.on_notify = self._on_notify
        self._conn.on_data_channel = self._on_dc_open
        self._conn.on_message = self._on_message
        self._conn.on_disconnect = self._on_disconnect

    def connect(self):
        print("[MGR] connecting ...")
        self._conn.connect()
        assert self._connected.wait(10.0), "Could not connect to Sora."
        print("[MGR] connected")

    # --- handlers ---
    def _on_set_offer(self, raw: str):
        m = json.loads(raw)
        if m.get("type") == "offer":
            self._connection_id = m.get("connection_id")

    def _on_notify(self, raw: str):
        m = json.loads(raw)
        if m.get("type")=="notify" and m.get("event_type")=="connection.created" and m.get("connection_id")==self._connection_id:
            self._connected.set()

    def _on_dc_open(self, label: str):
        if label in self._ready:
            self._ready[label] = True
        print(f"[MGR] dc ready: {label}")

    def _on_message(self, label: str, data: bytes):
        try:
            txt = data.decode("utf-8")
            msg = json.loads(txt)
        except Exception:
            print("[MGR][RX] bad payload:", label, data)
            return
        print("[MGR][RX]", label, msg)

        if label == self.ctrl and msg.get("t") == "cmd":
            v = msg.get("v")
            if   v == "UP":    self.y -= self.step; self.theta = -math.pi/2
            elif v == "DOWN":  self.y += self.step; self.theta =  math.pi/2
            elif v == "LEFT":  self.x -= self.step; self.theta =  math.pi
            elif v == "RIGHT": self.x += self.step; self.theta =  0.0
            # クリップ
            self.x = max(0, min(self.x, self.max_w))
            self.y = max(0, min(self.y, self.max_h))
            self._send_state()

    def _send_state(self):
        if not self._ready.get(self.state, False):
            print("[MGR][TX] drop (state dc not ready)")
            return
        payload = {"t":"state", "x": self.x, "y": self.y, "theta": self.theta}
        b = json.dumps(payload).encode("utf-8")
        self._conn.send_data_channel(self.state, b)
        print("[MGR][TX]", self.state, payload)

    def _on_disconnect(self, code: SoraSignalingErrorCode, msg: str):
        print(f"[MGR] disconnected: {code} {msg}")
        self._closed.set()

def main():
    load_dotenv()
    raw = os.getenv("SORA_SIGNALING_URLS")
    if not raw: raise ValueError("SORA_SIGNALING_URLS unset")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    chid = os.getenv("SORA_CHANNEL_ID") or "sora"
    ctrl = os.getenv("SORA_CTRL_LABEL", "#ctrl")
    state = os.getenv("SORA_STATE_LABEL", "#state")
    meta = json.loads(os.getenv("SORA_METADATA")) if os.getenv("SORA_METADATA") else None

    node = ManagerNode(urls, chid, ctrl, state, meta)
    node.connect()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
