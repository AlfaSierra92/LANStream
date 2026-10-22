FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
        7zip \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        libvpl2 \
        vainfo \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-linux.txt /app/requirements-linux.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /app/requirements-linux.txt

COPY app.py launcher.py media_runtime.py media_cache.py nzbdav.py subtitle_windows.py tone_mapping.py gunicorn.conf.py /app/
COPY templates /app/templates
COPY static /app/static

RUN mkdir -p /cache

EXPOSE 8080

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
