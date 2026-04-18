let _threeModulesPromise = null;

/** Three.js (~400KB). Lazy-load once when STLViewer mounts. */
export function loadThree() {
  if (_threeModulesPromise) return _threeModulesPromise;
  _threeModulesPromise = Promise.all([
    import("three"),
    import("three/addons/loaders/STLLoader.js"),
    import("three/addons/controls/OrbitControls.js"),
  ]).then(([THREE, { STLLoader }, { OrbitControls }]) => ({ THREE, STLLoader, OrbitControls }));
  return _threeModulesPromise;
}
