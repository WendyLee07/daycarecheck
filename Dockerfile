FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint requires Pango / Cairo / GDK-Pixbuf to render the formal report
# template into PDF. Without these the Python import succeeds but write_pdf()
# crashes at first call.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    shared-mime-info \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend.py mail_service.py cache_service.py index.html legal.html daycare_chains.json llms.txt google6510bf36c4229bca.html google6402099e2c87ae42.html ./
COPY .well-known ./.well-known

# Cloud Run injects PORT; default for local docker run.
ENV PORT=8080

# Secrets are mounted by Cloud Run via --set-secrets in separate dirs
# (Cloud Run requires one secret per mount directory).
ENV CLAWFORCE_JWT_PRIVATE_KEY_PATH=/secrets/jwt-private-key/value \
    CLAWFORCE_JWT_KID_PATH=/secrets/jwt-kid/value

EXPOSE 8080

CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT}"]
