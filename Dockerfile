# ARIA-OS dashboard — Railway Dockerfile
# Used in preference to nixpacks because nixpacks's multi-stage build
# strips libGL.so.1 from the runtime image even when it's installed
# at build time, breaking cadquery / OCP imports.
#
# kicad-cli installation: switched from multi-stage COPY (kicad/kicad:9.0)
# to plain `apt install kicad`. The COPY approach failed 3 times in a row
# chasing transitive deps (libkicommon, libwx_gtk3u_gl, libnss3, …). The
# apt path adds ~600MB to the image but every dep KiCad needs is captured
# in one line. Image goes from ~1.5GB to ~2.1GB; Railway hobby tier has
# no hard cap so this is fine. ldconfig issues from the COPY approach
# (libTKV3d.so.7 not a symlink) also disappear.

FROM python:3.11-slim-bookworm AS runtime

# System libraries cadquery / OCP / VTK / matplotlib / kicad-cli need at
# runtime. `kicad` itself pulls wxGTK + libsecret + libnss3 + most other
# transitive deps automatically.
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
        kicad \
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
