FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# nodejs/npm: Debian's package is >= 18, which the Claude Code CLI needs (claude_client.py shells out to it).
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential nodejs npm && \
    node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Pinned: the flags claude_client.py builds were verified against this exact version.
RUN npm install -g @anthropic-ai/claude-code@2.1.306 && \
    npm cache clean --force && \
    claude --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
# Seed fallback of the paper universe (the pod has no ops/ mount).
COPY ops/paper_universe_seed.json ./ops/

EXPOSE 8601

# --no-access-log: the ntfy approval buttons carry a ?token= query secret that the access log would persist.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8601", "--no-access-log"]
