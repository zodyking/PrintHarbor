# Sources and design decision notes

Reviewed October 30, 2026.

1. SavaPage, AGPL Open Print Portal: https://www.savapage.org/ and mail print docs https://docs.savapage.org/docs/04-webapp-admin/04-11.html. Inspired web portal, inbound IMAP configuration and print queues. Its primary documented workflow includes collecting into a web queue for print selection. We did not incorporate AGPL source code.
2. PrintDash, https://github.com/isuruwa/printdash. Its README describes SNMP monitoring, CUPS queue management, uploads, logs and a dashboard. Inspired monitoring and UI modules. No code copied.
3. Email-to-print, MIT, https://github.com/jfoboston/email-to-print. Its README describes an IMAP poller, allowlist, CUPS dispatch and Office conversion. Inspired automatic print workflow, but PrintHarbor uses a different SQLite-based deduplication and subject-PIN requirement. No code copied.
4. Tailscale Docker parameters: https://tailscale.com/docs/features/containers/docker/docker-params. Uses TS_USERSPACE=false for kernel routing, TS_EXTRA_ARGS=--accept-routes, persistent TS_STATE_DIR.
5. OpenPrinting CUPS device admin: https://openprinting.github.io/cups/doc/man-lpadmin.html and network backends: https://openprinting.github.io/cups/doc/network.html.
6. Brother HL-L2380DW official specs (IPP, port 9100, BR-Script3, duplex): https://www.brother-usa.com/p/laser-printers/HLL2380DW.

This software is an original implementation that combines similar product capabilities, not a code-based derivative, fork, clone, or distribution of any of the three applications.
