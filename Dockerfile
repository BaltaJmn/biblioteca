FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && python -c "import libtorrent"
COPY app ./app

RUN useradd --system --uid 1000 --home-dir /data biblioteca \
    && mkdir -p /data && chown biblioteca /data
USER biblioteca
VOLUME /data

EXPOSE 8080 6881/tcp 6881/udp
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

# Un único proceso: la sesión BitTorrent no se puede repartir entre varios workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
