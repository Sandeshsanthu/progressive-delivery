FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir gunicorn

# Copy app code
COPY . .

# Ensure DB folder writable (SQLite file will be created here)
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/car_market.db

EXPOSE 8000

# Run behind gunicorn; binds to 0.0.0.0 so it's reachable
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]

