FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
  cups cups-client cups-filters cups-ppdc printer-driver-gutenprint ghostscript poppler-utils \
  libreoffice-writer libreoffice-calc fonts-dejavu-core ca-certificates curl \
  cmake g++ make libcups2-dev \
  iproute2 netcat-openbsd snmp tini \
  && mkdir -p --mode=0755 /usr/share/keyrings \
  && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
  && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list \
  && apt-get update && apt-get install -y --no-install-recommends tailscale \
  && curl -fsSL -o /tmp/brlaser.tar.gz https://github.com/Owl-Maintain/brlaser/archive/refs/tags/v6.2.8.tar.gz \
  && tar -xzf /tmp/brlaser.tar.gz -C /tmp \
  && cmake -S /tmp/brlaser-6.2.8 -B /tmp/brlaser-build -DCMAKE_BUILD_TYPE=Release \
  && cmake --build /tmp/brlaser-build \
  && cmake --install /tmp/brlaser-build \
  && grep -q 'HL-L2380DW' /usr/share/cups/drv/brlaser.drv \
  && grep -q 'brlaserDensityAdjust' /usr/share/cups/drv/brlaser.drv \
  && rm -rf /tmp/brlaser-6.2.8 /tmp/brlaser-build /tmp/brlaser.tar.gz \
  && apt-get purge -y cmake g++ make libcups2-dev \
  && apt-get autoremove -y \
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
