FROM python:3.12-slim

WORKDIR /app

# Create non-root user
RUN addgroup --system app && adduser --system --ingroup app app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create directory for SQLite DB
RUN mkdir -p /data && chown -R app:app /data

# Collect static files
RUN python manage.py collectstatic --noinput --settings=webhook.settings || true

# Ensure app files owned by non-root user
RUN chown -R app:app /app

EXPOSE 8000

# Entrypoint handles migrations + server startup
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh


# Switch to non-root user
USER app

ENTRYPOINT ["/entrypoint.sh"]