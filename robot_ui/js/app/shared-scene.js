(function (global) {
  function initScene(options) {
    const mode = (options && options.mode) || 'viewer';
    const car = document.getElementById('car');
    if (!car) throw new Error('Car entity (#car) is required in the scene.');

    // Ensure physics does not drive the local pose. The authoritative state will overwrite.
    car.removeAttribute('car-drive');
    car.setAttribute('dynamic-body', 'type: kinematic; mass: 0; friction: 0');
    car.setAttribute('shadow', 'cast: true; receive: true');

    const trajectory = document.getElementById('trajectory');
    if (trajectory) trajectory.parentNode.removeChild(trajectory);

    const chase = document.getElementById('chasecam');
    if (chase) {
      chase.setAttribute(
        'chase-camera',
        'target: #car; dist: -3; height: 3.5; stiffness: 12; lookAhead: 0.45'
      );
    }

    const ambient = document.querySelector('[light][data-shared!=true]');
    if (!ambient) {
      const ambientLight = document.createElement('a-entity');
      ambientLight.setAttribute('light', 'type: ambient; intensity: 0.6');
      ambientLight.dataset.shared = 'true';
      car.sceneEl.appendChild(ambientLight);
      const dirLight = document.createElement('a-entity');
      dirLight.setAttribute('light', 'type: directional; intensity: 1.0');
      dirLight.setAttribute('position', '1 3 2');
      dirLight.dataset.shared = 'true';
      car.sceneEl.appendChild(dirLight);
    }

    if (mode === 'operator') {
      ensureEstopHook();
    }

    return {
      car,
      model: car.querySelector('[gltf-model]') || car,
    };
  }

  function ensureEstopHook() {
    const button = document.getElementById('eStopButton');
    if (!button) return;
    button.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('app:estop'));
    });
  }

  global.JetsonScene = Object.assign(global.JetsonScene || {}, {
    initScene,
  });
})(window);
