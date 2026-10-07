FROM python:3.12-slim

# nfs-common / cifs-utils let the container mount Hammerspace shares itself.
RUN apt-get update \
 && apt-get install -y --no-install-recommends nfs-common cifs-utils tini tzdata \
 && rm -rf /var/lib/apt/lists/*

# hstk: the Hammerspace toolkit that provides the `hs` CLI.
# Pin with --build-arg HSTK_SPEC="hstk==4.6.6.1" or install from git:
#   --build-arg HSTK_SPEC="git+https://github.com/hammer-space/hstk.git@master"
ARG HSTK_SPEC=hstk
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "${HSTK_SPEC}" && hs --help >/dev/null

COPY app ./app

ENV HSR_DATA_DIR=/data \
    HSR_MOUNT_ROOT=/mnt/hs \
    HSR_LOCAL_ROOT=/mnt/external \
    TZ=UTC \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data /mnt/hs /mnt/external
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1

# Single worker on purpose: the scheduler and run queue live in-process.
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
