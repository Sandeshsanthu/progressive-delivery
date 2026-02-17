FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Install deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && pip install --no-cache-dir gunicorn

# Copy app code
COPY . .

# Ensure DB folder writable (SQLite file will be created here)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*
COPY . /app
EXPOSE 8000

# Run behind gunicorn; binds to 0.0.0.0 so it's reachable
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]

