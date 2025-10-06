#!/usr/bin/env python3
"""Authoritative Sora data-channel manager.

Receives #ctrl messages over Sora data channels, integrates a lightweight
vehicle model at 60 Hz, and broadcasts authoritative #state snapshots up to 30
Hz. Heartbeats monitor liveness in both directions. The connection automatically
reconnects if Sora drops. An emergency-stop message can be initiated by either
UI or server and propagates to all listeners via the state stream.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from dotenv import load_dotenv
from sora_sdk import Sora, SoraConnection, SoraSignalingErrorCode


LOGGER = logging.getLogger("manager")
CTRL_HOLD_SEC = 0.2
CTRL_DAMP_SEC = 1.0
STATE_RATE_HZ = 30.0
PHYSICS_RATE_HZ = 60.0
HEARTBEAT_SEC = 1.0


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def wrap_angle(rad: float) -> float:
    while rad > math.pi:
        rad -= math.tau
    while rad <= -math.pi:
        rad += math.tau
    return rad


@dataclass
class ControlSnapshot:
    seq: int
    throttle: float
    steer: float
    brake: float
    mode: str
    received_at: float
    client_timestamp_ms: Optional[int]

    def age(self, now: float) -> float:
        return now - self.received_at


class VehicleModel:
    """Planar vehicle integrator suitable for network replay."""

    MAX_SPEED = 20.0  # m/s
    MAX_ACCEL = 9.0   # m/s^2 forward/back
    BRAKE_DECEL = 14.0
    COAST_DECEL = 2.0
    IDLE_DECEL = 1.5
    YAW_RATE_MAX = 2.5  # rad/s
    YAW_SLEW = 6.0      # rad/s^2
    ANGULAR_DAMP = 4.0

    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.yaw = 0.0
        self.vx = 0.0
        self.wz = 0.0
        self._last_dt = 1.0 / PHYSICS_RATE_HZ
        self._last_ctrl_age = float("inf")
        self._estop_active = False

    def step(self, ctrl: Optional[ControlSnapshot], dt: float, now: float) -> None:
        self._last_dt = dt
        throttle = steer = brake = 0.0
        age = float("inf")
        if ctrl:
            age = ctrl.age(now)
            if age <= CTRL_HOLD_SEC:
                throttle = ctrl.throttle
                steer = ctrl.steer
                brake = ctrl.brake
            else:
                decay = clamp((age - CTRL_HOLD_SEC) / CTRL_DAMP_SEC, 0.0, 1.0)
                throttle = ctrl.throttle * (1.0 - decay)
                steer = ctrl.steer * (1.0 - decay)
                brake = max(ctrl.brake, decay)
        self._last_ctrl_age = age

        if self._estop_active:
            throttle = 0.0
            brake = 1.0

        accel = throttle * self.MAX_ACCEL
        if math.isclose(throttle, 0.0, abs_tol=1e-3):
            if abs(self.vx) > 1e-3:
                accel -= math.copysign(self.COAST_DECEL, self.vx)
            else:
                accel = 0.0
        if brake > 0.0 and abs(self.vx) > 1e-3:
            accel -= math.copysign(self.BRAKE_DECEL * brake, self.vx)
        if not ctrl and not self._estop_active:
            if abs(self.vx) > 1e-3:
                accel -= math.copysign(self.IDLE_DECEL, self.vx)
            else:
                self.vx = 0.0

        self.vx += accel * dt
        if abs(self.vx) < 1e-3:
            self.vx = 0.0
        self.vx = clamp(self.vx, -self.MAX_SPEED, self.MAX_SPEED)

        target_wz = steer * self.YAW_RATE_MAX
        slew = self.YAW_SLEW * dt
        if ctrl:
            delta = clamp(target_wz - self.wz, -slew, slew)
            self.wz += delta
        else:
            damping = clamp(self.ANGULAR_DAMP * dt, 0.0, 1.0)
            self.wz *= 1.0 - damping
        if abs(self.wz) < 1e-3:
            self.wz = 0.0

        yaw_now = wrap_angle(self.yaw + self.wz * dt)
        heading_x = math.sin(yaw_now)
        heading_z = math.cos(yaw_now)
        self.x += self.vx * heading_x * dt
        self.z += self.vx * heading_z * dt
        self.yaw = yaw_now

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        return {
            "pose": {"x": self.x, "y": self.y, "z": self.z, "yaw": self.yaw},
            "vel": {"vx": self.vx, "wz": self.wz},
            "sim": {"dt": self._last_dt},
        }

    @property
    def ctrl_age(self) -> float:
        return self._last_ctrl_age

    def estop(self) -> None:
        self._estop_active = True
        self.vx = 0.0
        self.wz = 0.0

    def clear_estop(self) -> None:
        self._estop_active = False

    @property
    def estop_active(self) -> bool:
        return self._estop_active


class ManagerNode:
    def __init__(
        self,
        signaling_urls,
        channel_id,
        ctrl_label,
        state_label,
        metadata=None,
    ) -> None:
        self._sora = Sora()
        self.signaling_urls = signaling_urls
        self.channel_id = channel_id
        self.ctrl_label = ctrl_label
        self.state_label = state_label
        self.metadata = metadata

        self._stop_event = threading.Event()
        self._reconnect_event = threading.Event()
        self._disconnected_event = threading.Event()
        self._connected_event = threading.Event()
        self._connection_alive = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn: Optional[SoraConnection] = None
        self._connection_id: Optional[str] = None
        self._dc_ready: Dict[str, bool] = {self.ctrl_label: False, self.state_label: False}

        self._vehicle = VehicleModel()
        self._vehicle_lock = threading.Lock()
        self._state_seq = 0

        self._ctrl_lock = threading.Lock()
        self._last_ctrl: Optional[ControlSnapshot] = None
        self._last_ctrl_latency_ms: Optional[float] = None
        self._last_ctrl_recv_wall: Optional[float] = None

        self._last_hb_from_ui: Optional[float] = None
        self._last_hb_sent: float = time.time()
        self._estop_triggered: bool = False

        self._threads: list[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self._ctrl_recv_count = 0
        self._ctrl_drop_count = 0
        self._state_sent_count = 0

    # --- connection management ------------------------------------------
    def start(self) -> None:
        self._stop_event.clear()
        self._reconnect_event.set()
        self._threads = [
            threading.Thread(target=self._connection_loop, name="sora-conn", daemon=True),
            threading.Thread(target=self._physics_loop, name="physics", daemon=True),
            threading.Thread(target=self._state_loop, name="state", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self._stat_loop, name="stats", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._connection_alive.clear()
        self._reconnect_event.set()
        self._disconnected_event.set()
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
        for thread in self._threads:
            thread.join(timeout=1.0)

    def _connection_loop(self) -> None:
        while not self._stop_event.is_set():
            self._reconnect_event.wait()
            if self._stop_event.is_set():
                break
            self._reconnect_event.clear()
            try:
                conn = self._create_connection()
                with self._conn_lock:
                    self._conn = conn
                    self._dc_ready = {self.ctrl_label: False, self.state_label: False}
                    self._connection_id = None
                    self._connected_event.clear()
                    self._connection_alive.clear()
                    self._disconnected_event.clear()
                LOGGER.info("connecting to Sora %s", self.signaling_urls)
                LOGGER.info("channel_id %s", self.channel_id)
                conn.connect()
                if not self._connected_event.wait(timeout=10.0):
                    LOGGER.error("Sora connect timeout")
                    conn.disconnect()
                    time.sleep(2.0)
                    self._reconnect_event.set()
                    continue
                self._connection_alive.set()
                LOGGER.info("Sora connected: connection_id=%s", self._connection_id)
                self._disconnected_event.wait()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("connection loop error: %s", exc)
                time.sleep(2.0)
            finally:
                with self._conn_lock:
                    if self._conn is not None:
                        try:
                            self._conn.disconnect()
                        except Exception:  # noqa: BLE001
                            pass
                        self._conn = None
                self._connection_alive.clear()
                if not self._stop_event.is_set():
                    time.sleep(1.0)
                    self._reconnect_event.set()

    def _create_connection(self) -> SoraConnection:
        conn = self._sora.create_connection(
            signaling_urls=self.signaling_urls,
            role="sendrecv",
            channel_id=self.channel_id,
            metadata=self.metadata,
            audio=False,
            video=True,
            data_channel_signaling=True,
            data_channels=[
                {"label": self.ctrl_label, "direction": "recvonly", "ordered": True},
                {"label": self.state_label, "direction": "sendonly", "ordered": True},
            ],
        )

        def on_set_offer(raw: str, *, ref=conn) -> None:
            self._on_set_offer(ref, raw)

        def on_notify(raw: str, *, ref=conn) -> None:
            self._on_notify(ref, raw)

        def on_data_channel(label: str, *, ref=conn) -> None:
            self._on_data_channel(ref, label)

        def on_message(label: str, data: bytes, *, ref=conn) -> None:
            self._on_message(ref, label, data)

        def on_disconnect(code: SoraSignalingErrorCode, msg: str, *, ref=conn) -> None:
            self._on_disconnect(ref, code, msg)

        conn.on_set_offer = on_set_offer
        conn.on_notify = on_notify
        conn.on_data_channel = on_data_channel
        conn.on_message = on_message
        conn.on_disconnect = on_disconnect
        return conn

    # --- Sora callbacks ---------------------------------------------------
    def _on_set_offer(self, conn: SoraConnection, raw: str) -> None:
        with self._conn_lock:
            if conn is not self._conn:
                return
        msg = json.loads(raw)
        if msg.get("type") == "offer":
            self._connection_id = msg.get("connection_id")

    def _on_notify(self, conn: SoraConnection, raw: str) -> None:
        with self._conn_lock:
            if conn is not self._conn:
                return
        msg = json.loads(raw)
        if (
            msg.get("type") == "notify"
            and msg.get("event_type") == "connection.created"
            and msg.get("connection_id") == self._connection_id
        ):
            self._connected_event.set()

    def _on_data_channel(self, conn: SoraConnection, label: str) -> None:
        with self._conn_lock:
            if conn is not self._conn:
                return
        if label in self._dc_ready:
            self._dc_ready[label] = True
            LOGGER.info("data channel ready: %s", label)

    def _on_message(self, conn: SoraConnection, label: str, data: bytes) -> None:
        with self._conn_lock:
            if conn is not self._conn:
                return
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("drop malformed json on %s", label)
            return
        msg_type = payload.get("type")
        if msg_type == "ctrl" and label == self.ctrl_label:
            self._handle_ctrl(payload)
        elif msg_type == "hb":
            self._handle_heartbeat(payload)
        elif msg_type == "estop":
            self._handle_estop(payload)
        else:
            LOGGER.debug("ignore message type=%s label=%s", msg_type, label)

    def _on_disconnect(self, conn: SoraConnection, code: SoraSignalingErrorCode, msg: str) -> None:
        with self._conn_lock:
            if conn is not self._conn:
                return
        LOGGER.warning("Sora disconnected: %s %s", code, msg)
        self._connection_alive.clear()
        self._disconnected_event.set()

    # --- message handlers -------------------------------------------------
    def _handle_ctrl(self, msg: Dict[str, object]) -> None:
        seq = msg.get("seq")
        if not isinstance(seq, int):
            LOGGER.warning("ctrl without seq: %s", msg)
            return
        cmd = msg.get("cmd") or {}
        throttle = clamp(float(cmd.get("throttle", 0.0)), -1.0, 1.0)
        steer = clamp(float(cmd.get("steer", 0.0)), -1.0, 1.0)
        brake = clamp(float(cmd.get("brake", 0.0)), 0.0, 1.0)
        mode = str(cmd.get("mode", "arcade"))
        now_mono = time.perf_counter()
        client_ts_ms = msg.get("t") if isinstance(msg.get("t"), (int, float)) else None

        with self._ctrl_lock:
            if self._last_ctrl and seq <= self._last_ctrl.seq:
                self._ctrl_drop_count += 1
                return
            self._last_ctrl = ControlSnapshot(
                seq=seq,
                throttle=throttle,
                steer=steer,
                brake=brake,
                mode=mode,
                received_at=now_mono,
                client_timestamp_ms=int(client_ts_ms) if client_ts_ms is not None else None,
            )
            self._ctrl_recv_count += 1
            self._last_ctrl_recv_wall = time.time()
            if client_ts_ms is not None:
                latency = time.time() * 1000.0 - float(client_ts_ms)
                self._last_ctrl_latency_ms = latency
        if brake >= 0.99 and not math.isclose(throttle, 0.0, abs_tol=1e-3):
            LOGGER.debug("brake override detected, clearing throttle")

    def _handle_heartbeat(self, msg: Dict[str, object]) -> None:
        self._last_hb_from_ui = time.time()

    def _handle_estop(self, msg: Dict[str, object]) -> None:
        LOGGER.warning("estop requested via data channel: %s", msg)
        with self._vehicle_lock:
            self._vehicle.estop()
        self._estop_triggered = True

    # --- loops -------------------------------------------------------------
    def _physics_loop(self) -> None:
        target_dt = 1.0 / PHYSICS_RATE_HZ
        last = time.perf_counter()
        while not self._stop_event.is_set():
            now = time.perf_counter()
            dt = now - last
            if dt <= 0.0:
                dt = target_dt
            last = now
            ctrl = None
            with self._ctrl_lock:
                if self._last_ctrl:
                    ctrl = self._last_ctrl
            with self._vehicle_lock:
                self._vehicle.step(ctrl, dt, now)
            elapsed = time.perf_counter() - now
            sleep_for = target_dt - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _state_loop(self) -> None:
        target_dt = 1.0 / STATE_RATE_HZ
        while not self._stop_event.is_set():
            start = time.perf_counter()
            if self._connection_alive.is_set() and self._dc_ready.get(self.state_label, False):
                payload = self._build_state_payload()
                if payload:
                    self._send_state(payload)
            elapsed = time.perf_counter() - start
            sleep_for = target_dt - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            if now - self._last_hb_sent >= HEARTBEAT_SEC:
                self._send_heartbeat()
                self._last_hb_sent = now
            time.sleep(0.1)

    def _stat_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(5.0)
            with self._stats_lock:
                recv = self._ctrl_recv_count
                drop = self._ctrl_drop_count
                sent = self._state_sent_count
                self._ctrl_recv_count = 0
                self._ctrl_drop_count = 0
                self._state_sent_count = 0
            hb_ms = (time.time() - self._last_hb_from_ui) * 1000.0 if self._last_hb_from_ui else None
            hb_text = f"{hb_ms:.0f}ms" if hb_ms is not None else "n/a"
            LOGGER.info(
                "rates ctrl=+%d drop=%d state_sent=%d hb_age=%s",
                recv,
                drop,
                sent,
                hb_text,
            )

    # --- helpers -----------------------------------------------------------
    def _build_state_payload(self) -> Optional[Dict[str, object]]:
        with self._vehicle_lock:
            data = self._vehicle.snapshot()
            ctrl_age = self._vehicle.ctrl_age
            estop = self._vehicle.estop_active
        now_ms = int(time.time() * 1000.0)
        status_ok = not estop
        status_msg = "estop" if estop else ""
        if not estop:
            if math.isinf(ctrl_age):
                status_ok = False
                status_msg = "waiting ctrl"
            elif ctrl_age > CTRL_HOLD_SEC + CTRL_DAMP_SEC:
                status_ok = False
                status_msg = f"ctrl timeout {int(ctrl_age * 1000)}ms"
            elif ctrl_age > 0.4:
                status_msg = f"ctrl stale {int(ctrl_age * 1000)}ms"

        hb_age = None
        if self._last_hb_from_ui:
            hb_age = time.time() - self._last_hb_from_ui
            if hb_age > 3.0:
                status_ok = False
                status_msg = "ui heartbeat lost"

        payload: Dict[str, object] = {
            "type": "state",
            "seq": self._next_state_seq(),
            "t": now_ms,
            "pose": data["pose"],
            "vel": data["vel"],
            "status": {"ok": status_ok, "msg": status_msg},
            "sim": data["sim"],
        }
        if hb_age is not None:
            payload["status"]["hb_age"] = hb_age
        if self._last_ctrl_latency_ms is not None:
            payload["status"]["ctrl_latency_ms"] = self._last_ctrl_latency_ms
        if self._estop_triggered:
            payload["status"]["estop"] = True
        return payload

    def _next_state_seq(self) -> int:
        self._state_seq = (self._state_seq + 1) % (1 << 31)
        return self._state_seq

    def _send_state(self, obj: Dict[str, object]) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        with self._conn_lock:
            conn = self._conn
        if not conn:
            return
        try:
            conn.send_data_channel(self.state_label, data)
            with self._stats_lock:
                self._state_sent_count += 1
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("failed to send state: %s", exc)

    def _send_heartbeat(self) -> None:
        if not self._connection_alive.is_set():
            return
        payload = json.dumps(
            {"type": "hb", "role": "server", "t": int(time.time() * 1000.0), "label": self.state_label}
        ).encode("utf-8")
        with self._conn_lock:
            conn = self._conn
        if not conn:
            return
        try:
            conn.send_data_channel(self.state_label, payload)
        except Exception:  # noqa: BLE001
            LOGGER.debug("heartbeat send failed")

    def trigger_estop(self) -> None:
        LOGGER.warning("estop triggered locally")
        with self._vehicle_lock:
            self._vehicle.estop()
        self._estop_triggered = True

    def wait_forever(self) -> None:
        try:
            while not self._stop_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            LOGGER.info("interrupt received; stopping")
            self.stop()


# --- entry -----------------------------------------------------------------


def load_config(args: argparse.Namespace):
    urls = os.getenv("VITE_SORA_SIGNALING_URLS") or os.getenv("SORA_SIGNALING_URL")
    if not urls:
        raise ValueError("SORA_SIGNALING_URL or VITE_SORA_SIGNALING_URLS must be set")
    signaling_urls = [u.strip() for u in urls.split(",") if u.strip()]
    channel_id = args.room or os.getenv("VITE_SORA_CHANNEL_ID") or "sora"
    ctrl_label = os.getenv("VITE_CTRL_LABEL", "#ctrl")
    state_label = os.getenv("SORA_STATE_LABEL", "#state")
    metadata = os.getenv("SORA_METADATA")
    parsed_meta = json.loads(metadata) if metadata else {}
    if getattr(args, "password", None):
        parsed_meta["password"] = args.password
    return signaling_urls, channel_id, ctrl_label, state_label, parsed_meta or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Sora Data-Channel Manager")
    parser.add_argument("--room", help="Sora room ID (overrides VITE_SORA_CHANNEL_ID)")
    parser.add_argument("--password", help="Room password (injects into metadata)")
    parser.add_argument("--estop", action="store_true", help="Trigger immediate estop on start")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    load_dotenv("/Users/tsunogayashouta/aframe-manager-demo/ui/.env")
    cfg = load_config(args)
    node = ManagerNode(*cfg)

    if args.estop:
        node.trigger_estop()

    def handle_signal(_sig, _frame) -> None:  # noqa: ANN001
        LOGGER.info("signal received; shutting down")
        node.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    node.start()
    node.wait_forever()


if __name__ == "__main__":
    main()
