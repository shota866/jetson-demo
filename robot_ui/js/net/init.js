(function (global) {
  function initNet({ getInput, onState, onStatus }) {
    if (!global.JetsonConfig) throw new Error('JetsonConfig is not loaded');
    if (!global.JetsonNet || !global.JetsonNet.createSoraClient) {
      throw new Error('Sora client module not loaded');
    }

    const configOverrides = global.NET_CONFIG || {};
    const config = global.JetsonConfig.resolve(configOverrides);
    const metadata = global.JetsonConfig.createMetadata({
      pass: config.pass,
      metadata: configOverrides.metadata,
    });

    const client = global.JetsonNet.createSoraClient({
      signalingUrls: config.signalingUrls,
      channelId: config.room,
      metadata,
      role: 'sendrecv',
      debug: config.debug,
    });

    const status = {
      connection: 'idle',
      channels: {},
      ctrl: { rate: 0, open: false },
      state: { rate: 0, hbAgeMs: null, bufferSize: 0, lastFrame: null },
      serverStatus: null,
      debug: config.debug,
      config,
    };

    const hasCtrl = typeof getInput === 'function' && config.ctrlLabel;
    if (config.stateLabel) {
      client.registerChannel(config.stateLabel, { direction: 'recvonly' });
    }
    if (hasCtrl) {
      client.registerChannel(config.ctrlLabel, { direction: 'sendonly' });
    }

    let ctrlSender = null;
    if (hasCtrl) {
      ctrlSender = global.JetsonNet.createCtrlSender({
        client,
        label: config.ctrlLabel,
        getInput,
        debug: config.debug,
        onMetrics: (metrics) => {
          status.ctrl.rate = metrics.perSecond;
          status.ctrl.open = metrics.channelOpen;
          emitStatus('ctrl-rate');
        },
      });
    }

    const stateReceiver = global.JetsonNet.createStateReceiver({
      client,
      label: config.stateLabel,
      delayMs: config.delayMs,
      debug: config.debug,
      onState: (interp, raw, flags) => {
        status.state.lastFrame = { interp, raw, flags };
        if (typeof onState === 'function') onState(interp, raw, flags);
      },
      onStatus: (info) => {
        status.state.bufferSize = info.bufferSize;
        status.state.hbAgeMs = info.hbAgeMs;
        emitStatus('state-buffer');
      },
    });

    client.on('status', (info) => {
      status.connection = info.state;
      status.channels = info.channels || {};
      status.state.rate = info.stateRateHz || 0;
      status.state.hbAgeMs = info.heartbeatAgeMs;
      if (info.serverStatus) status.serverStatus = info.serverStatus;
      emitStatus('client-status');
    });

    client.on('channel-open', ({ label }) => {
      status.channels[label] = true;
      if (label === config.ctrlLabel) status.ctrl.open = true;
      emitStatus('channel-open');
    });

    client.on('channel-close', ({ label }) => {
      status.channels[label] = false;
      if (label === config.ctrlLabel) status.ctrl.open = false;
      emitStatus('channel-close');
    });

    client.on(`message:${config.stateLabel}`, (payload) => {
      if (payload && payload.status) {
        status.serverStatus = payload.status;
        emitStatus('state-message');
      }
    });

    client.on('heartbeat', (payload) => {
      if (payload && payload.label === config.stateLabel) {
        status.state.hbAgeMs = 0; // immediate heartbeat arrival
        emitStatus('heartbeat');
      }
    });

    client.start();

    function emitStatus(reason) {
      if (typeof onStatus !== 'function') return;
      const level = evaluateStatus();
      onStatus({ reason, level, snapshot: { ...status } });
    }

    function evaluateStatus() {
      if (status.connection === 'error' || status.connection === 'stopped') return 'disconnected';
      if (status.connection === 'reconnecting') return 'degraded';
      if (status.connection !== 'connected') return 'disconnected';
      if (hasCtrl && !status.ctrl.open) return 'disconnected';
      if (!status.channels[config.stateLabel]) return 'disconnected';
      if (status.state.hbAgeMs !== null && status.state.hbAgeMs > 2500) return 'degraded';
      if (status.serverStatus && status.serverStatus.ok === false) return 'degraded';
      return 'connected';
    }

    return {
      stop: async () => {
        stateReceiver.stop();
        if (ctrlSender) ctrlSender.stop();
        await client.stop();
      },
      forceBrake: () => ctrlSender && ctrlSender.forceBrake(),
      getStatus: () => ({ ...status }),
      client,
      stateReceiver,
      ctrlSender,
      config,
    };
  }

  global.initNet = initNet;
})(window);
