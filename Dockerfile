FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY lib/ /app/lib/
COPY server/ /app/server/

# Set Python path so server can import from lib/
ENV PYTHONPATH=/app/lib:/app/server
ENV PORT=8080

WORKDIR /app/server

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
