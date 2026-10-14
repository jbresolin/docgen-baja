# BAJA SAE Document Personalization Worker

Python worker that creates personalized, digitally signed PDFs for judge evaluations.

---

## Features

- **Automatic file personalization**: Downloads base files, applies watermarks, digitally signs PDFs
- **Google Drive integration**: Uploads personalized files to shared drive
- **Error reporting**: Reports failures back to GAS for visibility in Arquivos sheet
- **Job deduplication**: Prevents reprocessing jobs within 1 hour window
- **Health monitoring**: Writes status to `data/worker_status.json` for monitoring
- **Secret redaction**: API keys are scrubbed from logs, status files and alert emails

---

## How It Works

1. **Polls GAS API** every 60 seconds for pending jobs
2. **Downloads base file** (PDF or Google Doc → exported as PDF)
3. **Applies watermark**: Tiled opacity watermark + red footer + unique ID
4. **Digitally signs**: PKCS#12 certificate with auto-generation
5. **Uploads to Drive**: Personalized folder in shared drive
6. **Reports success/failure**: Calls GAS API to update Arquivos sheet
7. **GAS grants permissions**: Automatically gives judges/recipients access

---

## Files Created

All persisted under `data/` (mounted as a Docker volume so it survives
container recreation):

- `data/worker_status.json` - Health check status (updated every poll)
- `data/processed_jobs.json` - Recently processed job IDs (deduplication)
- `data/baja_personalizer.pfx` - Digital signing certificate (auto-generated once)
- `data/error_log.json` - Error log used for the daily digest email

`tmp_jobs/` holds temporary per-job files, cleaned up after each job.

---

## Health Check Status

`data/worker_status.json` format:
```json
{
  "status": "idle",
  "last_poll": "2024-12-24T19:30:15.123456",
  "jobs_processed_total": 42,
  "last_error": null,
  "timestamp": 1703450415.123
}
```

**Status values**:
- `"idle"` - Worker running, no jobs currently processing
- `"running"` - Worker processing jobs
- `"error"` - Last API poll failed
- `"stopped"` - Worker shut down cleanly

**Monitoring**: Check if `timestamp` is recent (< 2 minutes old) to verify worker is alive.

---

## Error Handling

### Job Processing Failures

When a job fails (download error, signing error, upload error):
1. Error logged to console with full stack trace
2. Error message (truncated to 200 chars) sent to GAS via `submitFailure` API
3. GAS writes error to Arquivos **Status** column: `"Erro Python: [error message]"`
4. Admin can see error in Arquivos sheet and take action

### Common Errors

**Drive download failed**:
- Cause: Base file deleted, permission denied, invalid file ID
- Status: `Erro Python: Download failed`
- Solution: Check base file URL in Arquivos sheet

**Upload failed**:
- Cause: No access to personalized folder, quota exceeded
- Status: `Erro Python: Upload failed`
- Solution: Check service account permissions on folder

**API submission failed**:
- Cause: Network error, GAS endpoint down, authentication failed
- Status: Logged locally, NOT reported to GAS (can't reach GAS)
- Solution: Check worker logs, verify API_BASE_URL and API_KEY

---

## Job Deduplication

Jobs are tracked in `data/processed_jobs.json` with timestamps:
```json
{
  "Rel|97|Judge Name": 1703450415.123,
  "Rel|98|Another Judge": 1703450420.456
}
```

**Behavior**:
- Jobs processed within last 1 hour are skipped
- Prevents duplicate processing if worker restarts
- Old entries automatically cleaned up

**Override**: Delete `data/processed_jobs.json` to force reprocessing all jobs.

---

## Configuration

**Environment variables** (copy `.env.example` to `.env`, or set these as stack
variables in Portainer — they are never hardcoded in source):

| Variable | Purpose |
|---|---|
| `EVENT_CODE` | Event label (e.g. `27BR`), shown in the startup log only — jobs are scoped by `API_BASE_URL`, not by this value |
| `API_BASE_URL` | GAS web app URL for this event's deployment |
| `API_KEY` | API key authenticating the worker to GAS |
| `PFX_PASSWORD` | **Required.** Password protecting the local signing certificate at rest. The worker refuses to start if it is unset or blank |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | (optional) the whole service account key file, base64-encoded or raw JSON. If unset, the worker reads `./service-account.json` |
| `SERVICE_ACCOUNT_FILE` | (optional, docker-compose only) host path of the key file to mount; defaults to `./service-account.json` |
| `RESEND_API_KEY` | (optional) Resend API key for failure-email alerts |
| `NOTIFY_EMAIL_TO` | (optional) alert recipient address — no emails are sent if empty |
| `NOTIFY_EMAIL_FROM` | (optional) alert sender address |
| `POLL_INTERVAL` | (optional) seconds between polls, default `60` |
| `NUM_WORKERS` | (optional) parallel job threads, default `4` |
| `JOB_DEDUP_HOURS` | (optional) skip jobs completed in the last N hours, default `1`; `0` disables dedup |
| `SHARED_DRIVE_ID` | Only for `test_drive_access.py`: the Shared Drive to test against |

Blank tuning values fall back to the defaults; a non-numeric or out-of-range
value stops the worker at startup. The `--poll-interval` and `--dedup-hours`
command-line flags, when given, override the env vars.

---

## Running the Worker

### Development
```bash
export PFX_PASSWORD=... API_BASE_URL=... API_KEY=...
python3 docgen_worker.py
```

### Production

**Option 1: Docker Compose / Portainer stack**
```bash
cp .env.example .env   # fill in the values
docker compose up -d --build
```

The Google key is never baked into the image. Set `GOOGLE_SERVICE_ACCOUNT_JSON`
(the default route, and the one to use for a Portainer git stack), or
uncomment the mount line in `docker-compose.yml` to mount the key file instead
(default: `./service-account.json`, override with `SERVICE_ACCOUNT_FILE`).

**Option 2: systemd (Linux)**
```ini
[Unit]
Description=BAJA DocGen Worker
After=network.target

[Service]
Type=simple
User=baja
WorkingDirectory=/path/to/docgen
ExecStart=/usr/bin/python3 docgen_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Option 3: supervisor**
```ini
[program:baja-docgen]
command=/usr/bin/python3 docgen_worker.py
directory=/path/to/docgen
autostart=true
autorestart=true
stderr_logfile=/var/log/baja-docgen.err.log
stdout_logfile=/var/log/baja-docgen.out.log
```

---

## Monitoring

### Check Worker Status
```bash
cat data/worker_status.json | python3 -m json.tool
```

### Check Logs
```bash
# If using Docker
docker logs -f docgen-worker

# If using systemd
journalctl -u baja-docgen -f

# If using supervisor
tail -f /var/log/baja-docgen.out.log
```

### Alert on Staleness
```bash
# Check if worker updated status in last 2 minutes
python3 -c "import json, time; s=json.load(open('data/worker_status.json')); exit(0 if time.time()-s['timestamp']<120 else 1)"
```

---

## Dependencies

Install with:
```bash
pip3 install -r requirements.txt
```

Required packages:
- `google-api-python-client` - Google Drive API
- `google-auth` - Service account authentication
- `reportlab` - PDF generation (watermarks)
- `pypdf` - PDF manipulation
- `pyhanko` - Digital signing
- `cryptography` - Certificate generation
- `requests` - HTTP API calls

---

## Security Notes

This repo is safe to make public as long as the files below stay out of it.
`.gitignore` already excludes all of them — don't force-add any of them.

- `service-account.json` - Google service account credentials. Never commit.
  Each deployment supplies it at runtime, either as a file next to
  `docgen_worker.py` (mounted read-only in Docker) or through the
  `GOOGLE_SERVICE_ACCOUNT_JSON` env var. It is never copied into the image.
- `*.pfx` - the digital signing certificate's private key. Never commit.
  Auto-generated on first run; back up `data/baja_personalizer.pfx` if you
  want signatures to stay stable across redeploys, but keep the backup private.
- `.env` - holds `API_KEY`, `PFX_PASSWORD`, `RESEND_API_KEY`, etc. Never commit.
  Only `.env.example` (placeholders) belongs in git.
- `data/`, `tmp_jobs/`, `worker_status.json`, `processed_jobs.json`,
  `error_log.json` - runtime state and temp files, not secrets, but don't
  belong in version control either.

All per-event settings and secrets (`EVENT_CODE`, `API_BASE_URL`, `API_KEY`,
`PFX_PASSWORD`, `RESEND_API_KEY`) are read from environment variables — see
[Configuration](#configuration) — and are never hardcoded in source.

`API_KEY` is sent to GAS as a URL query parameter, so HTTP error messages
contain it. The worker replaces it with `***` before anything is written to
logs, `data/` files, alert emails or the failure reports sent back to GAS.

---

**Last Updated**: 2026-10-12
