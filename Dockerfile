FROM python:3.11-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install libpq for psycopg2 + poppler for pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source (no venv, no .env — excluded by .dockerignore)
COPY app ./app
COPY emails ./emails

# Non-root user for container security
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]
