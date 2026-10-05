FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY docs/legal ./docs/legal
ENV PG_DB=/app/out/twin.db PG_SANDBOXES=/app/out/sandboxes PYTHONUNBUFFERED=1
# the base twin is deterministic; seed it on first start (about 3s) unless a volume already holds one
CMD ["sh", "-c", "test -f \"$PG_DB\" || python -m src.seed \"$PG_DB\" > /dev/null; exec python -m uvicorn src.twin_api:app --host 0.0.0.0 --port ${PORT:-8765}"]
