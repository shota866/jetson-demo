(function (global) {
  function fmt(num, digits) {
    if (!Number.isFinite(num)) return 'n/a';
    return Number.parseFloat(num).toFixed(digits || 2);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const config = global.JetsonConfig.resolve(global.NET_CONFIG || {});
    const debug = !!config.debug;
    if (debug) console.info('[viewer] debug logging enabled', config);

    const hudLabel = document.getElementById('netStatusLabel');
    const hudDetail = document.getElementById('netStatusDetail');
    const poseLabel = document.getElementById('poseInfo');

    const sceneRefs = global.JetsonScene.initScene({ mode: 'viewer' });
    const poseApplier = global.JetsonApp.createPoseApplier(sceneRefs.car);

    function updatePose(state) {
      if (!state || !poseLabel) return;
      poseApplier.apply(state.pose, state.vel || {});
      poseLabel.textContent = `pos: ${fmt(state.pose.x)} ${fmt(state.pose.y)} ${fmt(state.pose.z)} | yaw: ${fmt(state.pose.yaw, 3)} | vx: ${fmt(state.vel.vx)} | wz: ${fmt(state.vel.wz, 3)}`;
    }

    function updateStatus(info) {
      if (!hudLabel || !hudDetail) return;
      const snapshot = info.snapshot || {};
      hudLabel.textContent = info.level.toUpperCase();
      hudLabel.dataset.level = info.level;
      const lines = [];
      if (snapshot.connection) lines.push(`conn:${snapshot.connection}`);
      if (snapshot.state) {
        lines.push(`state:${(snapshot.state.rate || 0).toFixed(1)}Hz`);
        if (snapshot.state.hbAgeMs != null) lines.push(`hb:${snapshot.state.hbAgeMs.toFixed(0)}ms`);
      }
      if (snapshot.serverStatus) {
        const msg = snapshot.serverStatus.msg || (snapshot.serverStatus.ok ? 'ok' : 'ng');
        lines.push(`srv:${snapshot.serverStatus.ok ? 'ok' : 'ng'} ${msg}`);
      }
      hudDetail.textContent = lines.join(' | ');
      if (debug) console.debug('[viewer] status', info.reason, info.level, snapshot);
    }

    const net = global.initNet({
      onState: (interp) => updatePose(interp),
      onStatus: updateStatus,
    });

    window.addEventListener('beforeunload', () => {
      net.stop();
    });
  });
})(window);
