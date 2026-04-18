# ARIA-OS dashboard — Railway Dockerfile
# Used in preference to nixpacks because nixpacks's multi-stage build
# strips libGL.so.1 from the runtime image even when it's installed
# at build time, breaking cadquery / OCP imports.
#
# Two-stage build: pull kicad-cli from the official KiCad image
# (bookworm-based, ABI-compatible with our runtime) so the backend can
# export Gerbers headlessly. Pinned to 9.0 for reproducible builds.

FROM kicad/kicad:9.0 AS kicad-src

FROM python:3.11-slim-bookworm AS runtime

# System libraries cadquery / OCP / VTK / matplotlib need at runtime.
# Without these, "import cadquery" fails with libGL.so.1 ENOENT and
# /api/pipeline/health reports the kernel as unavailable.
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
        libwxgtk3.2-1 \
        libwxgtk-gl3.2-1 \
        libngspice0 \
        libpython3.11 \
        libsecret-1-0 \
        libnss3 \
    && rm -rf /var/lib/apt/lists/*

# Pull kicad-cli from the official KiCad image so the backend can export
# Gerbers headlessly. Adds ~250MB but avoids 1.2GB full apt install.
# 3dmodels deleted post-copy (~200MB) since Gerber export doesn't need them.
#
# IMPORTANT: copy `libki*` broadly (catches both libkicad_* AND libkicommon*).
# The narrower libkicad_* pattern missed libkicommon.so.9.0.8 on the first
# attempt and kicad-cli refused to start: "error while loading shared
# libraries: libkicommon.so.9.0.8". Same goes for OCCT libs — KiCad ships
# its own libTKernel/libTKMath/etc. (not just libocct_*); copy libTK* too.
COPY --from=kicad-src /usr/bin/kicad-cli /usr/bin/kicad-cli
COPY --from=kicad-src /usr/lib/x86_64-linux-gnu/libki*   /usr/lib/x86_64-linux-gnu/
COPY --from=kicad-src /usr/lib/x86_64-linux-gnu/libocct* /usr/lib/x86_64-linux-gnu/
COPY --from=kicad-src /usr/lib/x86_64-linux-gnu/libTK*   /usr/lib/x86_64-linux-gnu/
COPY --from=kicad-src /usr/share/kicad /usr/share/kicad
RUN rm -rf /usr/share/kicad/3dmodels && ldconfig

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
