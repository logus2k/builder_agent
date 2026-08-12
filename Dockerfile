# Slim Python runtime. The Builder core is stdlib-only (subprocess -> opencode, urllib ->
# reqoach commit); only the FastAPI service layer needs packages (requirements.txt). The local
# AI coder (opencode + Gemma) is NOT bundled: the host's opencode binary and its model config
# are bind-mounted at runtime (see compose), and the model backend is llama.cpp on :8500 reached
# via host networking — nothing model-related ships in this image.
FROM python:3.12-slim
WORKDIR /app
# Headless Chromium for the in-pipeline FRONTEND GATE: generated pages are rendered and their JS
# console is checked, so a page that throws (undefined function, bad URL, blocked resource) is
# rejected and regenerated instead of shipped. Without this the gate is skipped (structural check only).
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/*
ENV CHROMIUM_FLAGS="--headless --no-sandbox"
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY scripts/ ./scripts/
ENV PYTHONPATH=/app/src BUILDER_DATA_DIR=/app/data
# Run as a non-root user whose UID matches the host owner of data/ and the project repos, so
# files opencode writes into the repos' code/ area are not root-owned. Override at build time:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "$APP_GID" app 2>/dev/null || true \
 && useradd -u "$APP_UID" -g "$APP_GID" -m -d /home/app -s /usr/sbin/nologin app 2>/dev/null || true \
 && mkdir -p /app/data /home/app/.local /home/app/.cache /home/app/.config \
 && chown -R "$APP_UID:$APP_GID" /app /home/app
USER $APP_UID:$APP_GID
# opencode resolves its binary at ~/.opencode/bin/opencode and its model config at
# ~/.config/opencode/opencode.json — both bind-mounted under this HOME (see compose).
ENV HOME=/home/app
VOLUME ["/app/data"]
# HTTP service (mirrors the Planner): FACTORY triggers `builder:run` and polls `/jobs/{id}`.
# The batch CLI still works: docker compose run --rm builder-agent \
#   python3 scripts/build_plan.py <plan.json> --repo
CMD ["python3", "-m", "uvicorn", "builder.api:api", "--host", "0.0.0.0", "--port", "7806"]
