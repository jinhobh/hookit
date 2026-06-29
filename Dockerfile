FROM python:3.12-slim

WORKDIR /app

# Create a non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser

# Install production dependencies only
COPY pyproject.toml README.md ./
COPY app/ ./app/

RUN pip install --no-cache-dir .

# Transfer ownership and switch to non-root user
RUN chown -R appuser:appuser /app
USER appuser

# Start the Uvicorn server
# To run the delivery worker instead:
#   docker compose run app python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
