(function (global) {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const BACKOFF_SCHEDULE = [0.5, 1, 2, 4, 8];

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  class EventEmitter {
    constructor() {
      this._listeners = new Map();
    }

    on(event, handler) {
      if (!this._listeners.has(event)) {
        this._listeners.set(event, new Set());
      }
      this._listeners.get(event).add(handler);
      return () => this.off(event, handler);
    }

    off(event, handler) {
      const set = this._listeners.get(event);
      if (set) {
        set.delete(handler);
        if (set.size === 0) this._listeners.delete(event);
      }
    }

    emit(event, payload) {
      const set = this._listeners.get(event);
      if (!set) return;
      set.forEach((handler) => {
        try {
          handler(payload);
        } catch (err) {
          console.error('[sora-client] listener error', event, err);
        }
      });
    }
  }

  class SoraClient extends EventEmitter {
    constructor(options) {
      super();
      if (!global.Sora) {
        throw new Error('Sora JS SDK not loaded. Include sora.js before sora-client.js.');
      }
      const {
        signalingUrls,
        channelId,
        metadata = null,
        role = 'sendrecv',
        debug = false,
      } = options || {};

      this.signalingUrls = Array.isArray(signalingUrls) ? signalingUrls : [signalingUrls];
      this.channelId = channelId;
      this.metadata = metadata;
      this.role = role;
      this.debug = !!debug;

      this._sora = global.Sora;
      this._connection = null;
      this._session = null;
      this._running = false;
      this._disconnectResolver = null;
      this._channels = new Map(); // label -> { direction, options }
      this._channelState = new Map();
      this._connectionState = 'idle';
      this._lastHeartbeatAt = null;
      this._stateRateCounter = 0;
      this._stateRateWindowStart = performance.now();
      this._stateRateHz = 0;
      this._lastServerStatus = null;
      this._backoffIndex = 0;
    }

    registerChannel(label, config) {
      if (!label) {
        throw new Error('channel label is required');
      }
      const merged = Object.assign({ direction: 'sendrecv', ordered: true }, config || {});
      this._channels.set(label, merged);
      this._channelState.set(label, false);
    }

    isChannelOpen(label) {
      return this._channelState.get(label) || false;
    }

    getStateRateHz() {
      return this._stateRateHz;
    }

    getLastHeartbeatAgeMs() {
      if (!this._lastHeartbeatAt) return null;
      return Math.max(0, Date.now() - this._lastHeartbeatAt);
    }

    getLastServerStatus() {
      return this._lastServerStatus;
    }

    start() {
      if (this._running) return;
      if (!this.channelId) {
        throw new Error('channelId must be specified before start');
      }
      this._running = true;
      this._connectionLoop();
    }

    async stop() {
      this._running = false;
      if (this._session) {
        try {
          await this._session.disconnect();
        } catch (err) {
          if (this.debug) console.warn('[sora-client] disconnect error', err);
        }
        this._session = null;
      }
      if (this._disconnectResolver) {
        this._disconnectResolver();
        this._disconnectResolver = null;
      }
    }

    async _connectionLoop() {
      this._backoffIndex = 0;
      while (this._running) {
        try {
          this._setConnectionState('connecting', { attempt: this._backoffIndex });
          await this._connectOnce();
          this._backoffIndex = 0;
        } catch (err) {
          if (!this._running) break;
          const delay = BACKOFF_SCHEDULE[Math.min(this._backoffIndex, BACKOFF_SCHEDULE.length - 1)];
          this._backoffIndex += 1;
          this._setConnectionState('reconnecting', { error: err, backoff: delay });
          await wait(delay * 1000);
        }
      }
      this._setConnectionState('stopped');
    }

    async _connectOnce() {
      const connection = this._sora.connection(this.signalingUrls, this.debug);
      this._connection = connection;

      const dataChannels = [];
      for (const [label, cfg] of this._channels.entries()) {
        dataChannels.push({
          label,
          direction: cfg.direction || 'sendrecv',
          ordered: cfg.ordered !== false,
        });
      }

      const options = {
        audio: false,
        video: false,
        multistream: true,
        spotlight: false,
        dataChannelSignaling: true,
        dataChannels,
      };

      let session;
      switch (this.role) {
        case 'recvonly':
          session = connection.recvonly(this.channelId, this.metadata, options);
          break;
        case 'sendonly':
          session = connection.sendonly(this.channelId, this.metadata, options);
          break;
        default:
          session = connection.sendrecv(this.channelId, this.metadata, options);
          break;
      }

      this._wireSession(session);

      try {
        await session.connect();
      } catch (err) {
        this._unwireSession(session);
        this._setConnectionState('error', { error: err });
        throw err;
      }

      this._session = session;
      this._setConnectionState('connected');
      await new Promise((resolve) => {
        this._disconnectResolver = resolve;
      });
      this._disconnectResolver = null;
      this._unwireSession(session);
      this._session = null;
      for (const label of this._channels.keys()) {
        this._channelState.set(label, false);
      }
      this._setConnectionState('disconnected');
      if (!this._running) {
        return;
      }
      throw new Error('disconnected');
    }

    _wireSession(session) {
      session.on('disconnect', (event) => {
        if (this.debug) console.warn('[sora-client] disconnect', event);
        if (this._disconnectResolver) {
          this._disconnectResolver();
          this._disconnectResolver = null;
        }
      });
      session.on('timeout', (event) => {
        this.emit('timeout', event);
      });
      session.on('datachannel', (event) => {
        const channel = event.datachannel;
        if (!channel) return;
        const { label } = channel;
        channel.binaryType = 'arraybuffer';
        channel.onopen = () => {
          this._channelState.set(label, true);
          this.emit('channel-open', { label, channel });
          this._refreshStatus();
        };
        channel.onclose = () => {
          this._channelState.set(label, false);
          this.emit('channel-close', { label });
          this._refreshStatus();
        };
        channel.onerror = (err) => {
          this.emit('channel-error', { label, error: err });
        };
      });
      session.on('message', (event) => {
        this._handleIncoming(event);
      });
      session.on('notify', (event) => {
        this.emit('notify', event);
      });
      session.on('log', (event) => {
        if (this.debug) console.debug('[sora-client] log', event);
      });
    }

    _unwireSession(session) {
      try {
        session.on('disconnect', null);
        session.on('timeout', null);
        session.on('datachannel', null);
        session.on('message', null);
        session.on('notify', null);
        session.on('log', null);
      } catch (err) {
        if (this.debug) console.warn('[sora-client] unwire error', err);
      }
    }

    _handleIncoming(event) {
      const label = event.label;
      const data = event.data;
      let text;
      if (data instanceof ArrayBuffer) {
        text = decoder.decode(new Uint8Array(data));
      } else if (data instanceof Uint8Array) {
        text = decoder.decode(data);
      } else if (typeof data === 'string') {
        text = data;
      }
      if (!text) return;
      let payload;
      try {
        payload = JSON.parse(text);
      } catch (err) {
        if (this.debug) console.warn('[sora-client] invalid json', err);
        return;
      }

      if (payload.type === 'hb') {
        this._lastHeartbeatAt = Date.now();
        this.emit('heartbeat', Object.assign({ label }, payload));
        this._refreshStatus();
        return;
      }

      if (label && payload.type) {
        this.emit(`message:${label}`, payload);
      }
      this.emit('message', { label, payload });

      if (label === this._findStateLabel() && payload.type === 'state') {
        this._lastServerStatus = payload.status || null;
        const now = performance.now();
        this._stateRateCounter += 1;
        if (now - this._stateRateWindowStart >= 1000) {
          this._stateRateHz =
            (this._stateRateCounter * 1000) / (now - this._stateRateWindowStart);
          this._stateRateCounter = 0;
          this._stateRateWindowStart = now;
          this._refreshStatus();
        }
      }
    }

    _findStateLabel() {
      for (const [label, cfg] of this._channels.entries()) {
        if (label && /state/i.test(label)) return label;
      }
      return null;
    }

    _setConnectionState(state, extra) {
      this._connectionState = state;
      this.emit('connection-state', { state, extra });
      this._refreshStatus(extra);
      if (this.debug) {
        console.info('[sora-client] state', state, extra || {});
      }
    }

    _refreshStatus(extra) {
      this.emit('status', {
        state: this._connectionState,
        channels: Object.fromEntries(this._channelState.entries()),
        heartbeatAgeMs: this.getLastHeartbeatAgeMs(),
        stateRateHz: this.getStateRateHz(),
        serverStatus: this.getLastServerStatus(),
        extra,
      });
    }

    send(label, payload) {
      if (!label) throw new Error('label is required');
      if (!this.isChannelOpen(label)) return false;
      if (!this._session) return false;
      try {
        this._session.sendMessage(label, payload);
        return true;
      } catch (err) {
        this.emit('channel-error', { label, error: err });
        return false;
      }
    }

    sendJson(label, obj) {
      const text = JSON.stringify(obj);
      const bytes = encoder.encode(text);
      return this.send(label, bytes);
    }
  }

  global.JetsonNet = global.JetsonNet || {};
  global.JetsonNet.createSoraClient = function createSoraClient(options) {
    return new SoraClient(options || {});
  };
})(window);
