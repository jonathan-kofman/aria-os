# ARIA-OS dashboard — Railway Dockerfile
# Used in preference to nixpacks because nixpacks's multi-stage build
# strips libGL.so.1 from the runtime image even when it's installed
# at build time, breaking cadquery / OCP imports.
#
# This single-stage Dockerfile keeps every system lib in the final
# image so cadquery_ocp can dlopen its native deps at runtime.

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
RUN python -c "import cadquery; print('cadquery', cadquery.__version__, 'OK')"

CMD ["python", "run_dashboard.py", "--no-browser"]
