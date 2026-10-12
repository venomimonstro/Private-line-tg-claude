FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client && rm -rf /var/lib/apt/lists/*

COPY . .

ARG PRIVATE_LINE_REVISION=unknown
LABEL org.opencontainers.image.revision="$PRIVATE_LINE_REVISION"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "admin.main:app", "--host", "0.0.0.0", "--port", "8000"]
