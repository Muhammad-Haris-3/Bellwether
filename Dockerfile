# Serving container. Pipeline code is not installed here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Serving requirements only — see the comment in requirements-serve.txt.
COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

# api/ needs bellwether/config.py for Settings, and nothing else from the
# package. Copying the whole package is simpler than splitting it, and the
# container has no credential that could write regardless.
COPY bellwether/ ./bellwether/
COPY api/ ./api/

# Render supplies $PORT and it varies between deploys, so it is read at
# runtime rather than baked in.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
