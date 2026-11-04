# rhg-reporter · scheduled reports of pulse

A headless Chromium that opens screens of pulse as the people who receive them and emails a picture (in the body)
and a PDF of each. Identity owns the reports and the schedule (rhg-identity/reports.py); this service only:

1. asks identity for the runs due now (`POST /internal/reports/claim`, service token);
2. opens each screen through the internal nginx with a short read-only session of a recipient, waits until the
   screen has read its data, opens the tab, takes the picture and the PDF;
3. sends them through the Purchasing mail server (`POST /internal/mail`);
4. tells identity how it went (`POST /internal/reports/done`), which ends the sessions.

Environment: `IDENTITY_URL`, `SITE_URL` (the internal nginx), `PURCHASING_URL`, `SERVICE_TOKEN`, `COOKIE_NAME`,
`REPORT_POLL_SECONDS` (30). No ports, no data, no accounts. See deploy/docker-compose.yml.
