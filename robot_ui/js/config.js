(function (global) {
  const DEFAULTS = {
    signalingUrl: 'ws://sora2.uclab.jp:5000/signaling',
    room: 'sora',
    pass: '',
    ctrlLabel: '#ctrl',
    stateLabel: '#state',
    debug: false,
    delayMs: 80,
  };

  function parseSearch(search) {
    const params = new URLSearchParams(search || global.location.search || '');
    const out = {};
    if (params.has('signaling')) out.signalingUrl = params.get('signaling');
    if (params.has('room')) out.room = params.get('room');
    if (params.has('pass')) out.pass = params.get('pass');
    if (params.has('ctrl')) out.ctrlLabel = params.get('ctrl');
    if (params.has('state')) out.stateLabel = params.get('state');
    if (params.has('delayMs')) out.delayMs = Number(params.get('delayMs')) || DEFAULTS.delayMs;
    if (params.has('debug')) out.debug = params.get('debug') !== '0';
    return out;
  }

  function resolve(overrides) {
    const fromQuery = parseSearch();
    const merged = Object.assign({}, DEFAULTS, overrides || {}, fromQuery);
    merged.signalingUrls = normalizeUrls(merged.signalingUrl || DEFAULTS.signalingUrl);
    merged.signalingUrl = merged.signalingUrls[0];
    merged.delayMs = Number(merged.delayMs) || DEFAULTS.delayMs;
    merged.room = merged.room || DEFAULTS.room;
    merged.ctrlLabel = merged.ctrlLabel || DEFAULTS.ctrlLabel;
    merged.stateLabel = merged.stateLabel || DEFAULTS.stateLabel;
    merged.debug = Boolean(merged.debug);
    return merged;
  }

  function normalizeUrls(value) {
    if (!value) return [DEFAULTS.signalingUrl];
    if (Array.isArray(value)) return value.filter(Boolean);
    return String(value)
      .split(',')
      .map((v) => v.trim())
      .filter(Boolean);
  }

  function createMetadata(config) {
    const meta = {};
    if (config.pass) {
      meta.password = config.pass;
    }
    if (config.metadata && typeof config.metadata === 'object') {
      Object.assign(meta, config.metadata);
    }
    return Object.keys(meta).length > 0 ? meta : null;
  }

  global.JetsonConfig = Object.freeze({
    defaults: DEFAULTS,
    parseSearch,
    resolve,
    createMetadata,
  });
})(window);
