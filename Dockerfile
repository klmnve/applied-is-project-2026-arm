FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/workspace/data/audit.db \
    UPLOAD_DIR=/workspace/data/uploads

WORKDIR /workspace
COPY requirements.backend.txt ./requirements.backend.txt
RUN python -m pip install --no-cache-dir -r requirements.backend.txt \
    && groupadd --gid 10001 arm \
    && useradd --uid 10001 --gid arm --create-home arm \
    && mkdir -p /workspace/data/uploads \
    && chown -R arm:arm /workspace/data

COPY app ./app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
