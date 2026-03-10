FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create directory for SQLite DB
RUN mkdir -p /data

# Collect static files
RUN python manage.py collectstatic --noinput --settings=webhook.settings || true

EXPOSE 8000

# Entrypoint handles migrations + server startup
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
