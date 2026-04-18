# ARIA-OS dashboard — Railway Dockerfile
# Used in preference to nixpacks because nixpacks's multi-stage build
# strips libGL.so.1 from the runtime image even when it's installed
# at build time, breaking cadquery / OCP imports.
#
# kicad-cli installation history:
#   1. multi-stage COPY from kicad/kicad:9.0 — failed chasing transitive
#      deps (libkicommon, libwx_gtk3u_gl, libnss3, libTKV3d.so.7 symlink).
#   2. `apt install kicad` on bookworm-slim — the `kicad` metapackage
#      pulls the full IDE + 3D models + symbols + footprints (~1.8GB
#      unpacked). Railway builder ran out of memory/disk during unpack.
#   3. Current: switch base to Debian trixie (13) via python:3.11-slim-trixie.
#      Trixie ships a standalone `kicad-cli` package (~150MB) separate
#      from the full IDE — exactly what we need for headless Gerber export.

FROM python:3.11-slim-trixie AS runtime

# System libraries cadquery / OCP / VTK / matplotlib / kicad-cli need at
# runtime. `kicad-cli` on trixie is a small standalone package — it does
# NOT drag in wxGTK or the full IDE.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libxrender1 \
        libxi6 \
        libxext6 \
        libsm6 \
        libfontconfig1 \
        libgomp1 \
        libxkbcommon0 \
        libegl1 \
        libxcb1 \
        libxcb-glx0 \
        libxcb-render0 \
        libxcb-shape0 \
        libxcb-xfixes0 \
        libfreetype6 \
        ca-certificates \
        curl \
        kicad-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so changes to source don't bust the pip cache
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Then copy the app source
COPY . .

# Railway sets PORT at runtime
ENV PORT=8080
EXPOSE 8080

# Verify cadquery imports cleanly during build so Railway's deploy
# fails fast instead of producing a degraded healthcheck pass.
# Also verify kicad-cli is callable so Gerber export works at runtime.
RUN python -c "import cadquery; print('cadquery', cadquery.__version__, 'OK')" \
 && kicad-cli version

CMD ["python", "run_dashboard.py", "--no-browser"]
