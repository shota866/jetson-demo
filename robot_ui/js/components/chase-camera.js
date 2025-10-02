// js/components/chase-camera.js
(function () {
  if (typeof AFRAME === 'undefined') {
    console.error('[chase-camera] AFRAME not found. Load A-Frame before this script.');
    return;
  }

  AFRAME.registerComponent('chase-camera', {
    schema: {
      target: { type: 'selector' },
      dist: { default: -3 },
      height: { default: 3.5 },
      stiffness: { default: 12 },
      lookAhead: { default: 0.45 },
    },
    init() {
      this.pos = new THREE.Vector3();
      this.q = new THREE.Quaternion();
      this.fwd = new THREE.Vector3();
      this.back = new THREE.Vector3();
      this.up = new THREE.Vector3(0, 1, 0);
      this.des = new THREE.Vector3();
      this.look = new THREE.Vector3();
    },
    tick(t, dtms) {
      const tgt = this.data.target && this.data.target.object3D;
      if (!tgt) return;

      tgt.getWorldPosition(this.pos);
      tgt.getWorldQuaternion(this.q);

      const fwd = this.fwd.set(0, 0, -1).applyQuaternion(this.q).normalize();
      const back = this.back.copy(fwd).negate();

      this.des
        .copy(this.pos)
        .addScaledVector(this.up, this.data.height)
        .addScaledVector(back, this.data.dist);

      this.look
        .copy(this.pos)
        .addScaledVector(this.up, 0.6)
        .addScaledVector(fwd, this.data.lookAhead);

      const dt = Math.min(dtms / 1000, 0.05);
      const a = 1 - Math.exp(-this.data.stiffness * dt);

      this.el.object3D.position.lerp(this.des, a);

      const camObj = this.el.getObject3D('camera') || this.el.object3D;
      camObj.up.set(0, 1, 0);
      camObj.lookAt(this.look);
    },
  });
})();
