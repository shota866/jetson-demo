(function (global) {
  const MIN_FRAME_MS = 1000 / 60;
  const MIN_CHANGE = 0.01;
  const KEEPALIVE_MS = 1000;

  function clamp(value, min, max) {
    if (!Number.isFinite(value)) return 0;
    return value < min ? min : value > max ? max : value;
  }

  class CtrlSender {
    constructor(options) {
      const { client, label, getInput, onMetrics, debug = false } = options || {};
      if (!client) throw new Error('CtrlSender requires a Sora client');
      if (!label) throw new Error('CtrlSender requires a ctrl label');
      if (typeof getInput !== 'function') throw new Error('CtrlSender requires getInput function');
      this.client = client;
      this.label = label;
      this.getInput = getInput;
      this._onMetrics = onMetrics;
      this.debug = !!debug;

      this._rafId = null;
      this._channelOpen = false;
      this._lastSendAt = 0;
      this._lastHeartbeatAt = 0;
      this._lastPayload = null;
      this._seq = 0;
      this._sendCount = 0;
      this._lastRateLog = performance.now();

      this._onOpen = this._handleOpen.bind(this);
      this._onClose = this._handleClose.bind(this);

      client.on('channel-open', this._onOpen);
      client.on('channel-close', this._onClose);
    }

    start() {
      if (this._rafId !== null) return;
      const loop = () => {
        this._tick();
        this._rafId = global.requestAnimationFrame(loop);
      };
      this._rafId = global.requestAnimationFrame(loop);
    }

    stop() {
      if (this._rafId !== null) {
        global.cancelAnimationFrame(this._rafId);
        this._rafId = null;
      }
      this.client.off('channel-open', this._onOpen);
      this.client.off('channel-close', this._onClose);
    }

    forceBrake() {
      this._send({
        throttle: 0,
        steer: 0,
        brake: 1,
        mode: 'arcade',
        device: { keyboard: false, gamepad: false },
      }, true);
    }

    _handleOpen(evt) {
      if (evt.label === this.label) {
        this._channelOpen = true;
        if (this.debug) console.info('[ctrl] channel open');
      }
    }

    _handleClose(evt) {
      if (evt.label === this.label) {
        this._channelOpen = false;
        this._lastPayload = null;
        if (this.debug) console.warn('[ctrl] channel closed');
      }
    }

    _tick() {
      if (!this._channelOpen) {
        this._emitMetrics(false);
        return;
      }
      const now = performance.now();
      const input = this.getInput();
      if (!input) {
        this._maybeSendHeartbeat(now);
        this._emitMetrics(now);
        return;
      }
      const payload = this._normaliseInput(input);
      const shouldSend = this._shouldSend(payload, now);
      if (shouldSend) {
        this._send(payload, false);
        this._lastSendAt = now;
        this._lastPayload = payload;
        this._sendCount += 1;
      }
      this._maybeSendHeartbeat(now);
      this._emitMetrics(now);
    }

    _normaliseInput(raw) {
      const payload = {
        throttle: clamp(Number(raw.throttle) || 0, -1, 1),
        steer: clamp(Number(raw.steer) || 0, -1, 1),
        brake: clamp(Number(raw.brake) || 0, 0, 1),
        mode: raw.mode || 'arcade',
        device: {
          keyboard: !!(raw.device && raw.device.keyboard),
          gamepad: !!(raw.device && raw.device.gamepad),
        },
      };
      if (payload.brake < 0.01) payload.brake = 0;
      return payload;
    }

    _shouldSend(payload, now) {
      if (!this._lastPayload) return true;
      const elapsed = now - this._lastSendAt;
      if (elapsed < MIN_FRAME_MS) return false;
      const deltaThrottle = Math.abs(payload.throttle - this._lastPayload.throttle);
      const deltaSteer = Math.abs(payload.steer - this._lastPayload.steer);
      const deltaBrake = Math.abs(payload.brake - this._lastPayload.brake);
      const deviceChanged =
        payload.device.keyboard !== this._lastPayload.device.keyboard ||
        payload.device.gamepad !== this._lastPayload.device.gamepad;
      const modeChanged = payload.mode !== this._lastPayload.mode;

      if (deviceChanged || modeChanged) return true;
      if (deltaThrottle >= MIN_CHANGE) return true;
      if (deltaSteer >= MIN_CHANGE) return true;
      if (deltaBrake >= MIN_CHANGE) return true;
      if (elapsed >= KEEPALIVE_MS) return true;
      return false;
    }

    _maybeSendHeartbeat(now) {
      if (!this._channelOpen) return;
      if (now - this._lastHeartbeatAt < KEEPALIVE_MS) return;
      if (this.client.sendJson(this.label, { type: 'hb', role: 'ui', t: Date.now() })) {
        this._lastHeartbeatAt = now;
      }
    }

    _send(payload, isOverride) {
      if (!this._channelOpen) return;
      const message = {
        type: 'ctrl',
        seq: this._nextSeq(),
        t: Date.now(),
        source: 'ui',
        cmd: {
          throttle: Number(payload.throttle.toFixed(4)),
          steer: Number(payload.steer.toFixed(4)),
          brake: Number(payload.brake.toFixed(4)),
          mode: payload.mode,
        },
        device: payload.device,
        override: !!isOverride,
      };
      const ok = this.client.sendJson(this.label, message);
      if (ok && this.debug) console.debug('[ctrl] sendCtrl', message);
    }

    _nextSeq() {
      this._seq = (this._seq + 1) % 0x7fffffff;
      return this._seq;
    }

    _emitMetrics(now) {
      if (typeof this._onMetrics !== 'function') return;
      const ts = now || performance.now();
      if (ts - this._lastRateLog < 1000) return;
      this._onMetrics({ perSecond: this._sendCount, channelOpen: this._channelOpen });
      this._sendCount = 0;
      this._lastRateLog = ts;
    }
  }

  global.JetsonNet = global.JetsonNet || {};
  global.JetsonNet.createCtrlSender = function createCtrlSender(options) {
    const sender = new CtrlSender(options || {});
    sender.start();
    return sender;
  };
})(window);
