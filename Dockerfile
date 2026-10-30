FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
  cups cups-client cups-filters printer-driver-brlaser ghostscript poppler-utils \
  libreoffice-writer libreoffice-calc fonts-dejavu-core ca-certificates \
  iproute2 netcat-openbsd snmp tini \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/printharbor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY start.sh ./start.sh
RUN chmod +x start.sh && mkdir -p /data/files /data/tmp /run/cups && chmod 700 /data
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/printharbor/start.sh"]
