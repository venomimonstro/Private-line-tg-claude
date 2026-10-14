FROM ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2 AS xray

FROM python:3.11-slim

COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl openssh-client sshpass && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client && rm -rf /var/lib/apt/lists/*

COPY . .

ARG PRIVATE_LINE_REVISION=unknown
LABEL org.opencontainers.image.revision="$PRIVATE_LINE_REVISION"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "admin.main:app", "--host", "0.0.0.0", "--port", "8000"]
