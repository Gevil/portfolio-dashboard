FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

EXPOSE 8601

# --no-access-log: the ntfy approval buttons carry a ?token= query secret that the access log would persist.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8601", "--no-access-log"]
