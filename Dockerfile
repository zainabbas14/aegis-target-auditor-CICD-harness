# syntax=docker/dockerfile:1

# ---- build: resolve pinned wheels once -------------------------------------
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt .

# ---- runtime: no compilers, no source tree, no root ------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDITOR_LOG_FORMAT=json
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
COPY --from=build /wheels /wheels
# --no-index: only the pinned wheels built above can be installed.
RUN pip install --no-index --find-links=/wheels multi-agent-finance-auditor && rm -rf /wheels
USER app
WORKDIR /home/app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "auditor.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
