FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
  cups cups-client cups-filters printer-driver-brlaser ghostscript poppler-utils \
  libreoffice-writer libreoffice-calc fonts-dejavu-core ca-certificates curl \
  iproute2 netcat-openbsd snmp tini \
  && mkdir -p --mode=0755 /usr/share/keyrings \
  && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
  && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list \
  && apt-get update && apt-get install -y --no-install-recommends tailscale \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/printharbor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY start.sh ./start.sh
RUN chmod +x start.sh && mkdir -p /data/files /data/tmp /run/cups /usr/share/printharbor/cups-seed \
  && cp -a /etc/cups/. /usr/share/printharbor/cups-seed/ \
  && chmod 700 /data
EXPOSE 47831
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/printharbor/start.sh"]
