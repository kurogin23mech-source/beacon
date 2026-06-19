FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional DynamoDB backend deps (e-1987). 本番 (Firestore 経路) はこの ARG を
# 設定しないので boto3 は入らず、イメージサイズに影響しない。ローカル検証スタック
# (= docker-compose / BEACON_STORE_BACKEND=dynamodb) だけが INSTALL_DYNAMODB=1 を
# 渡して boto3 を入れる。
ARG INSTALL_DYNAMODB=
COPY server/requirements-dynamodb.txt .
RUN if [ -n "$INSTALL_DYNAMODB" ]; then pip install --no-cache-dir -r requirements-dynamodb.txt; fi

# Copy application code
# ARG CACHE_BUST is used to force Docker layer cache invalidation when lib/ changes
ARG CACHE_BUST=1
COPY lib/ /app/lib/
COPY server/ /app/server/

# Set Python path so server can import from lib/
ENV PYTHONPATH=/app/lib:/app/server
ENV PORT=8080

WORKDIR /app/server

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
