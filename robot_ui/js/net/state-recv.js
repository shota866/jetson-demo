(function (global) {
  const DEFAULT_DELAY_MS = 80;
  const MAX_HISTORY_MS = 2000;
  const MAX_EXTRAPOLATE_MS = 150;
  const MOD = 0x80000000;
  const HALF = 0x40000000;

  function clamp(value, min, max) {
    return value < min ? min : value > max ? max : value;
  }

  function wrapAngle(rad) {
    const tau = Math.PI * 2;
    let r = rad % tau;
    if (r > Math.PI) r -= tau;
    if (r <= -Math.PI) r += tau;
    return r;
  }

  function seqAhead(current, previous) {
    if (previous === null || previous === undefined) return true;
    let diff = ((current - previous) % MOD + MOD) % MOD;
    if (diff > HALF) diff -= MOD;
    return diff > 0;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function lerpAngle(a, b, t) {
    const diff = wrapAngle(b - a);
    return wrapAngle(a + diff * t);
  }

  class StateReceiver {
    constructor(options) {
      const { client, label, delayMs = DEFAULT_DELAY_MS, onState, onStatus, debug = false } = options || {};
      if (!client) throw new Error('StateReceiver requires a Sora client');
      if (!label) throw new Error('StateReceiver requires a state label');

      this.client = client;
      this.label = label;
      this.delayMs = Number(delayMs) || DEFAULT_DELAY_MS;
      this.onState = onState;
      this.onStatus = onStatus;
      this.debug = !!debug;

      this._buffer = [];
      this._timeOffset = null;
      this._lastSeq = null;
      this._raf = null;
      this._running = false;
      this._lastFrame = null;
      this._lastHeartbeatPayload = null;
      this._lastStatusEmit = 0;
      this._lastMessageWall = null;

      this._onMessage = this._handleMessage.bind(this);
      this._onHeartbeat = this._handleHeartbeat.bind(this);
      this.client.on(`message:${label}`, this._onMessage);
      this.client.on('heartbeat', this._onHeartbeat);
    }

    start() {
      if (this._running) return;
      this._running = true;
      const loop = () => {
        if (!this._running) return;
        this._step();
        this._raf = global.requestAnimationFrame(loop);
      };
      this._raf = global.requestAnimationFrame(loop);
    }

    stop() {
      this._running = false;
      if (this._raf !== null) {
        global.cancelAnimationFrame(this._raf);
        this._raf = null;
      }
      this.client.off(`message:${this.label}`, this._onMessage);
      this.client.off('heartbeat', this._onHeartbeat);
    }

    _handleMessage(payload) {
      if (payload.type !== 'state') return;
      this._lastMessageWall = performance.now();
      if (payload.status) {
        this._lastHeartbeatPayload = payload.status;
      }
      const seq = payload.seq;
      if (typeof seq !== 'number') return;
      if (!seqAhead(seq, this._lastSeq)) return;
      this._lastSeq = seq;

      const now = performance.now();
      if (this._timeOffset === null && typeof payload.t === 'number') {
        this._timeOffset = now - payload.t;
      }

      const entry = {
        seq,
        time: typeof payload.t === 'number' && this._timeOffset !== null ? payload.t + this._timeOffset : now,
        state: payload,
      };
      this._buffer.push(entry);
      this._buffer.sort((a, b) => a.time - b.time);

      const minTime = now - MAX_HISTORY_MS;
      while (this._buffer.length > 0 && this._buffer[0].time < minTime) {
        this._buffer.shift();
      }
    }

    _handleHeartbeat(payload) {
      if (!payload || payload.label !== this.label) return;
      this._lastHeartbeatPayload = payload;
    }

    _step() {
      if (!this._running) return;
      if (!this.onState || this._buffer.length === 0) {
        this._emitStatus('idle');
        return;
      }
      const now = performance.now();
      const target = now - this.delayMs;
      let previous = null;
      let next = null;
      for (const entry of this._buffer) {
        if (entry.time <= target) previous = entry;
        if (entry.time >= target) {
          next = entry;
          break;
        }
      }
      if (!previous) previous = this._buffer[0];
      if (!next) next = this._buffer[this._buffer.length - 1];

      let frame;
      let flags = { extrapolated: false };
      if (!previous || !next) {
        frame = this._cloneState(next ? next.state : previous.state);
      } else if (next.time === previous.time) {
        frame = this._cloneState(next.state);
      } else if (target <= next.time) {
        const span = next.time - previous.time;
        const t = clamp((target - previous.time) / span, 0, 1);
        frame = this._interpolate(previous.state, next.state, t);
      } else {
        const dtMs = Math.min(target - next.time, MAX_EXTRAPOLATE_MS);
        frame = this._extrapolate(next.state, dtMs / 1000);
        flags.extrapolated = true;
      }

      this._lastFrame = {
        appliedAt: now,
        bufferSize: this._buffer.length,
        sourceSeq: frame.seq,
        flags,
      };

      this.onState(frame, next.state, flags);
      this._emitStatus('frame');
    }

    _cloneState(state) {
      return JSON.parse(JSON.stringify(state));
    }

    _interpolate(a, b, t) {
      return {
        type: 'state',
        seq: b.seq,
        t: lerp(a.t, b.t, t),
        pose: {
          x: lerp(a.pose.x, b.pose.x, t),
          y: lerp(a.pose.y, b.pose.y, t),
          z: lerp(a.pose.z, b.pose.z, t),
          yaw: lerpAngle(a.pose.yaw, b.pose.yaw, t),
        },
        vel: {
          vx: lerp(a.vel.vx, b.vel.vx, t),
          wz: lerp(a.vel.wz, b.vel.wz, t),
        },
        status: b.status ? JSON.parse(JSON.stringify(b.status)) : a.status ? JSON.parse(JSON.stringify(a.status)) : undefined,
        sim: b.sim ? JSON.parse(JSON.stringify(b.sim)) : a.sim ? JSON.parse(JSON.stringify(a.sim)) : undefined,
      };
    }

    _extrapolate(base, dt) {
      const yaw = wrapAngle(base.pose.yaw + (base.vel.wz || 0) * dt);
      const distance = (base.vel.vx || 0) * dt;
      return {
        type: 'state',
        seq: base.seq,
        t: base.t + dt * 1000,
        pose: {
          x: base.pose.x + Math.sin(yaw) * distance,
          y: base.pose.y,
          z: base.pose.z + Math.cos(yaw) * distance,
          yaw,
        },
        vel: { vx: base.vel.vx, wz: base.vel.wz },
        status: base.status ? JSON.parse(JSON.stringify(base.status)) : undefined,
        sim: base.sim ? JSON.parse(JSON.stringify(base.sim)) : undefined,
      };
    }

    _emitStatus(reason) {
      if (typeof this.onStatus !== 'function') return;
      const now = performance.now();
      if (now - this._lastStatusEmit < 200) return;
      this._lastStatusEmit = now;
      const hbMs = this._deriveHeartbeatAge();
      this.onStatus({
        reason,
        bufferSize: this._buffer.length,
        hbAgeMs: hbMs,
        lastFrame: this._lastFrame,
        lastHeartbeat: this._lastHeartbeatPayload,
        lastMessageWall: this._lastMessageWall,
      });
    }

    _deriveHeartbeatAge() {
      if (this._lastHeartbeatPayload && typeof this._lastHeartbeatPayload.hb_age === 'number') {
        return Math.max(0, this._lastHeartbeatPayload.hb_age * 1000);
      }
      const wall = this._lastMessageWall;
      if (!wall) return null;
      return Math.max(0, performance.now() - wall);
    }
  }

  global.JetsonNet = global.JetsonNet || {};
  global.JetsonNet.createStateReceiver = function createStateReceiver(options) {
    const receiver = new StateReceiver(options || {});
    receiver.start();
    return receiver;
  };
})(window);
