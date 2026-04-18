# ARIA-OS dashboard — Railway Dockerfile
#
# kicad-cli installation history (4 failed attempts before this one):
#   1. Multi-stage COPY from kicad/kicad:9.0 — chased transitive deps
#      one at a time (libkicommon, libwx_gtk3u_gl, libnss3, libTKV3d).
#   2. `apt install kicad` on bookworm-slim — IDE metapackage ~1.8GB
#      OOM'd Railway builder during apt unpack.
#   3. `python:3.13-slim-trixie` + `apt install kicad-cli` — failed
#      with apt exit code 100 (`kicad-cli` not in trixie main repo, OR
#      package isn't built for slim base, can't tell which).
#   4. (this attempt) FROM kicad/kicad:9.0 directly — flip the polarity:
#      use KiCad's official image as the BASE, then add Python + cadquery
#      system libs on top. KiCad's image already has every kicad dep +
#      working OS — we only need to layer the python runtime onto it.
#
# Base: kicad/kicad:9.0 is Ubuntu 24.04 noble. python3.12 ships in apt.

FROM kicad/kicad:9.0 AS runtime

# Become root for install steps (kicad image runs as `kicad` user by default)
USER root

# Python 3.12 + cadquery / OCP / VTK / matplotlib system libs.
# kicad-cli is already at /usr/bin/kicad-cli — pre-installed in the base.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
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
    && rm -rf /var/lib/apt/lists/*

# Symlink python → python3 so existing CMD/RUN entries don't break
RUN ln -sf /usr/bin/python3 /usr/local/bin/python

WORKDIR /app

# Copy requirements first so changes to source don't bust the pip cache.
# Ubuntu 24.04 enforces PEP 668 (externally-managed env) — use
# --break-system-packages since this is a container; we own the env.
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages --upgrade pip \
    && pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Then copy the app source
COPY . .

# Railway sets PORT at runtime
ENV PORT=8080
EXPOSE 8080

# Verify both runtimes work during build so Railway's deploy fails fast
# instead of producing a degraded healthcheck pass.
RUN python -c "import cadquery; print('cadquery', cadquery.__version__, 'OK')" \
 && kicad-cli version

CMD ["python", "run_dashboard.py", "--no-browser"]
