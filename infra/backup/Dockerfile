FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir google-cloud-firestore-admin

COPY firestore_export.py .

CMD ["python", "firestore_export.py"]
