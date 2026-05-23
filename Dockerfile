FROM python:3.11-slim

WORKDIR /app

# Build-time deps needed to compile Python packages (layer cached separately)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=120 -r requirements.txt

# Runtime tools in a separate layer so the pip layer above stays cached
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client && rm -rf /var/lib/apt/lists/*

COPY . .

RUN adduser --disabled-password --gecos '' appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/health_check.py

CMD ["python", "-m", "uvicorn", "admin.main:app", "--host", "0.0.0.0", "--port", "8000"]
