import { useEffect, useRef, useState } from "react";
import { T } from "./theme.js";
import { loadThree } from "./loadThree.js";

export default function STLViewer({ stlUrl }) {
  const mountRef = useRef(null);
  const sceneRef = useRef(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!mountRef.current || !stlUrl) return;
    let cancelled = false;
    setLoading(true);

    loadThree().then(({ THREE, STLLoader, OrbitControls }) => {
      if (cancelled || !mountRef.current) return;
      setLoading(false);
      const w = mountRef.current.clientWidth, h = mountRef.current.clientHeight;

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setSize(w, h);
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setClearColor(0x000000, 0);
      mountRef.current.appendChild(renderer.domElement);

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 10000);
      camera.position.set(0, 0, 200);

      scene.add(new THREE.AmbientLight(0xffffff, 0.5));
      const dir = new THREE.DirectionalLight(0xffffff, 1.2);
      dir.position.set(100, 100, 100);
      scene.add(dir);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;

      const loader = new STLLoader();
      loader.load(stlUrl, (geometry) => {
        if (cancelled) return;
        geometry.computeBoundingBox();
        const box = geometry.boundingBox;
        const center = new THREE.Vector3();
        box.getCenter(center);
        geometry.translate(-center.x, -center.y, -center.z);
        const size = box.getSize(new THREE.Vector3()).length();
        camera.position.set(0, 0, size * 1.5);
        controls.update();

        const mesh = new THREE.Mesh(
          geometry,
          new THREE.MeshPhongMaterial({ color: 0x00d4ff, specular: 0x222222, shininess: 80, side: THREE.DoubleSide })
        );
        scene.add(mesh);
      });

      let animId;
      const animate = () => {
        if (cancelled) return;
        animId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
      };
      animate();
      sceneRef.current = { renderer, animId };
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
      if (sceneRef.current) {
        cancelAnimationFrame(sceneRef.current.animId);
        sceneRef.current.renderer.dispose();
      }
      if (mountRef.current) mountRef.current.innerHTML = "";
    };
  }, [stlUrl]);

  return (
    <div ref={mountRef} style={{ width: "100%", height: "100%", background: "transparent" }}>
      {!stlUrl && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "12px" }}>
          <div style={{ fontSize: "32px", opacity: 0.2 }}>◈</div>
          <div style={{ fontSize: "12px", color: T.text4 }}>No part selected</div>
        </div>
      )}
      {stlUrl && loading && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "12px" }}>
          <div style={{ fontSize: "11px", color: T.text3 }}>Loading 3D viewer...</div>
        </div>
      )}
    </div>
  );
}
