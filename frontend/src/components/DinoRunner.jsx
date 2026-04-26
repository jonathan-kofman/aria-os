// DinoRunner.jsx — endless-runner loading animation, drawn on a tiny
// canvas. A small character hops over passing obstacles while the ARIA
// pipeline does its thing. Pure stand-alone implementation: random
// obstacle spacing, deterministic gravity, no external assets.
//
// Props:
//   message?: string  — optional caption shown next to the canvas
//   theme?:   { bg, fg, accent, muted } colour overrides
//   width?:   canvas width  (default 280)
//   height?:  canvas height (default 50)

import { useEffect, useRef } from "react";

const DEFAULT_THEME = {
  bg:     "transparent",
  fg:     "#1A1A18",
  accent: "#AE5630",
  muted:  "#8F8B85",
};

export function DinoRunner({
  message,
  theme = DEFAULT_THEME,
  width = 280,
  height = 50,
}) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // Match the device pixel ratio so the pixel-art stays crisp on HiDPI.
    const dpr = window.devicePixelRatio || 1;
    canvas.width  = width  * dpr;
    canvas.height = height * dpr;
    canvas.style.width  = width  + "px";
    canvas.style.height = height + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    // World state ----------------------------------------------------
    const groundY    = height - 8;            // pixels from top
    const charX      = 22;                    // fixed horizontal position
    const G          = 0.55;                  // gravity (px/frame²)
    const JUMP_V     = 9.2;                   // initial jump velocity
    const SPEED      = 2.6;                   // world scroll (px/frame)
    let   charY      = 0;                     // height above ground
    let   velY       = 0;
    let   frame      = 0;
    let   obstacles  = [];
    let   nextSpawnFrame = 30;
    let   raf;

    const spawn = () => {
      const isTall = Math.random() > 0.65;
      const w = isTall ? 6 : (8 + Math.random() * 4) | 0;
      const h = isTall ? (16 + Math.random() * 8) | 0
                       : (10 + Math.random() * 6) | 0;
      obstacles.push({ x: width + 6, w, h });
      // 50–110 frames between spawns; a bit of randomness keeps it from
      // settling into a metronome.
      nextSpawnFrame = frame + 50 + Math.floor(Math.random() * 60);
    };

    const tick = () => {
      frame++;

      // Physics: jump when an obstacle is close and we're on the ground.
      const lookAhead = obstacles.find(
        o => o.x > charX - 5 && o.x < charX + 38);
      if (lookAhead && charY === 0 && lookAhead.x - charX < 32) {
        velY = JUMP_V;
      }
      if (charY > 0 || velY > 0) {
        charY += velY;
        velY  -= G;
        if (charY < 0) { charY = 0; velY = 0; }
      }

      // Scroll obstacles + cull off-screen ones.
      obstacles.forEach(o => { o.x -= SPEED; });
      obstacles = obstacles.filter(o => o.x + o.w > -2);
      if (frame >= nextSpawnFrame) spawn();

      // Draw ----------------------------------------------------------
      ctx.clearRect(0, 0, width, height);
      if (theme.bg && theme.bg !== "transparent") {
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, width, height);
      }

      // Ground: dashed line that scrolls with the world.
      ctx.strokeStyle = theme.muted;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.lineDashOffset = -((frame * SPEED) % 7);
      ctx.beginPath();
      ctx.moveTo(0, groundY + 0.5);
      ctx.lineTo(width, groundY + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);

      // Obstacles.
      ctx.fillStyle = theme.muted;
      obstacles.forEach(o => {
        ctx.fillRect(o.x, groundY - o.h, o.w, o.h);
      });

      // Character: a 14×16 pixel-art block creature. Body + head + legs.
      const cy = groundY - 16 - charY;
      ctx.fillStyle = theme.accent;
      ctx.fillRect(charX,     cy + 4, 10, 12);             // body
      ctx.fillRect(charX + 8, cy,     10,  8);             // head
      // Eye (gap in head)
      ctx.fillStyle = theme.bg === "transparent" ? "#FFF" : theme.bg;
      ctx.fillRect(charX + 14, cy + 2, 2, 2);
      // Legs animate while running, lock together while airborne.
      ctx.fillStyle = theme.accent;
      if (charY === 0) {
        const legOffset = (frame >> 2) % 2;          // alternate every 4 frames
        ctx.fillRect(charX + 1, cy + 16, 3, 4 - legOffset * 2);
        ctx.fillRect(charX + 6, cy + 16, 3, 2 + legOffset * 2);
      } else {
        ctx.fillRect(charX + 1, cy + 16, 3, 2);
        ctx.fillRect(charX + 6, cy + 16, 3, 2);
      }

      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [width, height, theme]);

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12,
      padding: "8px 14px",
    }}>
      <canvas ref={canvasRef} style={{
        display: "block",
        imageRendering: "pixelated",
      }}/>
      {message && (
        <span style={{
          fontStyle: "italic",
          color: theme.muted,
          fontSize: 13,
        }}>{message}</span>
      )}
    </div>
  );
}
