# syntax=docker/dockerfile:1.6

# ===== Stage 1: builder =====
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ===== Stage 2: runtime =====
FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin laurel

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels /root/.cache

# config.py lee el .env de BASE_DIR (el WORKDIR) y run.py expone la app.
COPY config.py run.py ./
COPY app/ ./app/

ENV PYTHONDONTWRITEBYTECODE=1

USER laurel

EXPOSE 5002

CMD ["gunicorn", "-b", "0.0.0.0:5002", "-w", "2", "--timeout", "60", "run:app"]