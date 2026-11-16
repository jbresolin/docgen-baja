#!/usr/bin/env python3
"""
BAJA SAE BRASIL – Generic Personalized Document Generator
---------------------------------------------------------

This worker:
- Polls the Apps Script API for pending personalized-file jobs
- Downloads base documents (PDF or Google Docs -> exported as PDF)
- Applies tiled short-ID watermarks + red footer
- Digitally signs with auto-generated PKCS#12 certificate (.pfx)
- Uploads signed PDFs to Google Drive (Shared Drive)
- Calls submitResult() to finalize the job
- Runs forever (default poll interval: 60s)
"""

import os
import io
import sys
import time
import json
import base64
import signal
import logging
import hashlib
import tempfile
import argparse
from urllib.parse import quote, quote_plus
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Global shutdown flag for graceful termination
SHUTDOWN_REQUESTED = False

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    global SHUTDOWN_REQUESTED
    if SHUTDOWN_REQUESTED:
        # Second Ctrl+C - force exit
        print("\nForce shutdown...")
        sys.exit(1)
    SHUTDOWN_REQUESTED = True
    print("\nShutdown requested. Waiting for current jobs to finish (Ctrl+C again to force)...")

import requests
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import Color, red
from pypdf import PdfReader, PdfWriter

# Google API
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# Certificate generation
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# PDF signing
from pyhanko.sign import signers
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env_number(name, default, cast, minimum):
    """Read a numeric env var; blank means default, invalid values stop startup."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        kind = "a whole number" if cast is int else "a number"
        raise RuntimeError(f"{name} must be {kind}, got {raw!r}")
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}, got {raw!r}")
    return value


API_BASE_URL = os.environ.get("API_BASE_URL", "")
API_KEY = os.environ.get("API_KEY", "")

EVENT_CODE = os.environ.get("EVENT_CODE", "26BR")
POLL_INTERVAL = env_number("POLL_INTERVAL", 60, int, minimum=1)  # Seconds between polls
NUM_WORKERS = env_number("NUM_WORKERS", 4, int, minimum=1)  # Parallel job threads

# Google service account credentials: either the whole key file in the
# GOOGLE_SERVICE_ACCOUNT_JSON env var (raw JSON or base64), or a file on disk.
SERVICE_ACCOUNT_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
SERVICE_ACCOUNT_JSON = "./service-account.json"

TEMP_DIR = "./tmp_jobs"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs("./data", exist_ok=True)

# Certificate
PFX_PATH = "./data/baja_personalizer.pfx"
PFX_PASSWORD = os.environ.get("PFX_PASSWORD", "")  # Required - checked in main()

# Health check and job tracking
STATUS_FILE = "./data/worker_status.json"
PROCESSED_JOBS_FILE = "./data/processed_jobs.json"
# Don't reprocess jobs completed in the last N hours (0 disables dedup)
JOB_DEDUP_HOURS = env_number("JOB_DEDUP_HOURS", 1.0, float, minimum=0)

# Email notifications via Resend
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")  # Set via environment variable
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "")  # Recipient email (no emails if empty)
NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "DocGen Worker <onboarding@resend.dev>")
DIGEST_HOUR = 6  # Send daily digest at 6 AM
ERROR_LOG_FILE = "./data/error_log.json"

# Verbose mode (set via command line --verbose or -v)
VERBOSE = False

# Secrets that must never reach logs, status files or emails. The API key
# travels as a URL query parameter, so `requests` errors include it verbatim.
_SECRETS = {
    s for s in (
        API_KEY, quote(API_KEY, safe=""), quote_plus(API_KEY),
        RESEND_API_KEY,
    ) if s
}


def redact(text):
    """Replace any configured secret in text with ***. Redact BEFORE truncating."""
    text = str(text)
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


class RedactingFormatter(logging.Formatter):
    """Scrubs secrets from the final log line, including tracebacks."""
    def format(self, record):
        return redact(super().format(record))


# Logging
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(
    RedactingFormatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("docgen_worker")


def log_verbose(msg):
    """Log message only if verbose mode is enabled."""
    if VERBOSE:
        logger.info(f"[VERBOSE] {msg}")

PAGE_WIDTH, PAGE_HEIGHT = A4


# ---------------------------------------------------------------------------
# AUTO-GENERATE PKCS#12 DIGITAL SIGNING CERTIFICATE
# ---------------------------------------------------------------------------

def create_pfx_if_missing(pfx_path: str, password: str):
    if os.path.exists(pfx_path):
        return

    print("🔐 No .pfx found – generating self-signed certificate...")

    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "BAJA SAE Brasil DocGen"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BAJA SAE Brasil"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=3650))
        .sign(key, hashes.SHA512())
    )

    pfx_bytes = serialization.pkcs12.serialize_key_and_certificates(
        name=b"BAJA SAE DocGen",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode())
    )

    with open(pfx_path, "wb") as f:
        f.write(pfx_bytes)

    print(f"✅ Certificate generated: {pfx_path}")


def load_signer_from_pfx(pfx_path: str, password: str):
    return signers.SimpleSigner.load_pkcs12(
        pfx_path,
        passphrase=password.encode("utf-8")
    )

# ---------------------------------------------------------------------------
# GOOGLE DRIVE SERVICE
# ---------------------------------------------------------------------------

def load_service_account_credentials():
    scopes = ["https://www.googleapis.com/auth/drive"]

    raw = os.environ.get(SERVICE_ACCOUNT_ENV, "").strip()
    if raw:
        # Accept raw JSON or base64 (base64 survives .env / Portainer quoting)
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        return service_account.Credentials.from_service_account_info(
            json.loads(raw),
            scopes=scopes
        )

    if not os.path.isfile(SERVICE_ACCOUNT_JSON):
        raise RuntimeError(
            f"No Google credentials: set {SERVICE_ACCOUNT_ENV} or provide {SERVICE_ACCOUNT_JSON}"
        )
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON,
        scopes=scopes
    )


def get_drive_service():
    return build("drive", "v3", credentials=load_service_account_credentials())

def extract_drive_id(url: str) -> str:
    # Handle /file/d/ID format (Drive files)
    if "/file/d/" in url:
        return url.split("/file/d/")[1].split("/")[0]
    # Handle /spreadsheets/d/ID format (Google Sheets)
    if "/spreadsheets/d/" in url:
        return url.split("/spreadsheets/d/")[1].split("/")[0]
    # Handle /document/d/ID format (Google Docs)
    if "/document/d/" in url:
        return url.split("/document/d/")[1].split("/")[0]
    # Handle /presentation/d/ID format (Google Slides)
    if "/presentation/d/" in url:
        return url.split("/presentation/d/")[1].split("/")[0]
    # Handle id= query parameter
    if "id=" in url:
        return url.split("id=")[1].split("&")[0]
    # Handle /folders/ID format
    if "folders/" in url:
        return url.split("folders/")[1].split("?")[0]
    # Assume it's already an ID
    return url


def drive_get_metadata(DRIVE, file_id):
    return DRIVE.files().get(
        fileId=file_id,
        fields="id,name,mimeType",
        supportsAllDrives=True
    ).execute()


def drive_download(DRIVE, file_id, dest_path):
    meta = drive_get_metadata(DRIVE, file_id)
    mime = meta["mimeType"]

    if mime.startswith("application/vnd.google-apps."):
        request = DRIVE.files().export_media(
            fileId=file_id,
            mimeType="application/pdf"
        )
    else:
        request = DRIVE.files().get_media(
            fileId=file_id,
            supportsAllDrives=True
        )

    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    if not os.path.exists(dest_path):
        raise RuntimeError("Download failed")


def drive_upload(DRIVE, local_path, filename, folder_id):
    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/pdf")

    file = DRIVE.files().create(
        body=metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()

    return file["id"]


# ---------------------------------------------------------------------------
# WATERMARK (short tiled watermark)
# ---------------------------------------------------------------------------

def make_watermark(hex_id: str, judge_name: str = ""):
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=A4)

    # red footer with judge name
    if judge_name:
        footer = f"BAJA SAE BRASIL - CONFIDENCIAL - Para avaliação por {judge_name}"
    else:
        footer = "BAJA SAE BRASIL - CONFIDENCIAL - Documento individualizado"
    c.setFont("Helvetica", 10)
    c.setFillColor(red)
    c.drawString(15, 15, footer)

    # watermark text: short hex ID only
    stamp = hex_id
    font_size = 18
    opacity = 0.06
    spacing_x = 180
    spacing_y = 150

    c.saveState()
    c.setFont("Helvetica-Bold", font_size)
    c.setFillColor(Color(0.1, 0.1, 0.1, alpha=opacity))
    c.rotate(45)

    for x in range(-800, 1800, spacing_x):
        for y in range(-800, 1800, spacing_y):
            c.drawString(x, y, stamp)

    c.restoreState()

    # corner IDs
    c.setFont("Helvetica", 7)
    c.setFillColor(Color(0.1, 0.1, 0.1, alpha=0.65))
    c.drawRightString(PAGE_WIDTH - 20, 20, hex_id)
    c.drawRightString(PAGE_WIDTH - 20, PAGE_HEIGHT - 20, hex_id)
    c.drawString(20, PAGE_HEIGHT - 20, hex_id)

    c.save()
    packet.seek(0)
    return PdfReader(packet)


def personalize_pdf(input_pdf, output_pdf, hex_id, judge_name=""):
    reader = PdfReader(input_pdf)
    writer = PdfWriter()
    watermark = make_watermark(hex_id, judge_name)

    for page in reader.pages:
        # pypdf 7+: page must belong to the writer before merging into it
        out_page = writer.add_page(page)
        out_page.merge_page(watermark.pages[0])

    with open(output_pdf, "wb") as f:
        writer.write(f)


# ---------------------------------------------------------------------------
# SIGNING
# ---------------------------------------------------------------------------

def sign_pdf(input_pdf, output_pdf, signer):
    with open(input_pdf, "rb") as inf:
        writer = IncrementalPdfFileWriter(inf)
        meta = signers.PdfSignatureMetadata(field_name="BAJA_SIG")
        pdf_signer = signers.PdfSigner(meta, signer=signer)
        result = pdf_signer.sign_pdf(writer)

    with open(output_pdf, "wb") as outf:
        outf.write(result.getbuffer())


# ---------------------------------------------------------------------------
# APPS SCRIPT API
# ---------------------------------------------------------------------------

def api_get_jobs():
    log_verbose(f"Calling API: GET {API_BASE_URL}?action=getJobs")
    # Add cache-busting parameter to bypass Google Apps Script edge caching
    params = {"action": "getJobs", "key": API_KEY, "_t": int(time.time())}
    resp = requests.get(API_BASE_URL, params=params, timeout=120)
    log_verbose(f"API response status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    log_verbose(f"API response data: {json.dumps(data, indent=2)[:500]}")
    jobs = data["jobs"] if "jobs" in data else []
    log_verbose(f"Found {len(jobs)} jobs")
    return jobs


def api_submit_result(job_id, doc_code, parent_config_code, team_number, judge_name, file_id):
    params = {"action": "submitResult", "key": API_KEY}
    payload = {
        "jobId": job_id,
        "docCode": doc_code,
        "parentConfigCode": parent_config_code,
        "teamNumber": str(team_number),
        "judgeName": judge_name,
        "personalFileId": file_id
    }
    log_verbose(f"api_submit_result: POST payload = {json.dumps(payload, indent=2)}")
    resp = requests.post(API_BASE_URL, params=params, json=payload, timeout=30)
    log_verbose(f"api_submit_result: Response status={resp.status_code}, body={resp.text[:500]}")
    resp.raise_for_status()


def api_submit_failure(job_id, doc_code, team_number, judge_name, error_message):
    params = {"action": "submitFailure", "key": API_KEY}
    payload = {
        "jobId": job_id,
        "docCode": doc_code,
        "teamNumber": str(team_number),
        "judgeName": judge_name,
        "error": redact(error_message)
    }
    try:
        requests.post(API_BASE_URL, params=params, json=payload, timeout=30).raise_for_status()
    except Exception as e:
        logger.error(f"Failed to report job failure to GAS: {e}")


# ---------------------------------------------------------------------------
# HEALTH CHECK & JOB TRACKING
# ---------------------------------------------------------------------------

def update_status_file(status, jobs_processed=0, last_error=None):
    """Update health check status file."""
    try:
        status_data = {
            "status": status,
            "last_poll": datetime.now().isoformat(),
            "jobs_processed_total": jobs_processed,
            "last_error": last_error,
            "timestamp": time.time()
        }
        with open(STATUS_FILE, "w") as f:
            json.dump(status_data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to update status file: {e}")


def load_processed_jobs():
    """Load list of recently processed job IDs."""
    if not os.path.exists(PROCESSED_JOBS_FILE):
        return {}
    try:
        with open(PROCESSED_JOBS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_processed_job(job_id):
    """Mark job as processed with timestamp."""
    processed = load_processed_jobs()
    processed[job_id] = time.time()

    # Clean up old entries (older than JOB_DEDUP_HOURS)
    cutoff = time.time() - (JOB_DEDUP_HOURS * 3600)
    processed = {jid: ts for jid, ts in processed.items() if ts > cutoff}

    try:
        with open(PROCESSED_JOBS_FILE, "w") as f:
            json.dump(processed, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save processed jobs: {e}")


def is_job_recently_processed(job_id):
    """Check if job was processed in the last JOB_DEDUP_HOURS."""
    processed = load_processed_jobs()
    if job_id not in processed:
        return False

    cutoff = time.time() - (JOB_DEDUP_HOURS * 3600)
    return processed[job_id] > cutoff


# ---------------------------------------------------------------------------
# ERROR NOTIFICATIONS (Resend)
# ---------------------------------------------------------------------------

def load_error_log():
    """Load accumulated errors from file."""
    if not os.path.exists(ERROR_LOG_FILE):
        return {"errors": [], "last_digest_sent": None}
    try:
        with open(ERROR_LOG_FILE, "r") as f:
            return json.load(f)
    except:
        return {"errors": [], "last_digest_sent": None}


def save_error_log(error_log):
    """Save error log to file."""
    try:
        with open(ERROR_LOG_FILE, "w") as f:
            json.dump(error_log, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save error log: {e}")


def add_error_to_log(error_type, message, details=None, is_critical=False):
    """Add an error to the accumulated log."""
    error_log = load_error_log()
    error_entry = {
        "timestamp": datetime.now().isoformat(),
        "type": error_type,
        "message": redact(message)[:500],
        "details": redact(details) if details else details,
        "is_critical": is_critical
    }
    error_log["errors"].append(error_entry)
    save_error_log(error_log)

    # Send immediate notification for critical errors
    if is_critical and RESEND_API_KEY and NOTIFY_EMAIL_TO:
        send_critical_alert(error_entry)

    return error_entry


def send_email_via_resend(subject, html_body):
    """Send an email using Resend API."""
    if not RESEND_API_KEY or not NOTIFY_EMAIL_TO:
        log_verbose("Resend not configured, skipping email")
        return False

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": NOTIFY_EMAIL_FROM,
                "to": [NOTIFY_EMAIL_TO],
                "subject": subject,
                "html": html_body
            },
            timeout=30
        )
        resp.raise_for_status()
        logger.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email via Resend: {e}")
        return False


def send_critical_alert(error_entry):
    """Send immediate alert for critical errors."""
    subject = f"[CRITICAL] DocGen Worker Error: {error_entry['type']}"
    html_body = f"""
    <h2 style="color: #dc2626;">Critical Error in DocGen Worker</h2>
    <p><strong>Time:</strong> {error_entry['timestamp']}</p>
    <p><strong>Type:</strong> {error_entry['type']}</p>
    <p><strong>Message:</strong></p>
    <pre style="background: #f3f4f6; padding: 12px; border-radius: 4px;">{error_entry['message']}</pre>
    {f"<p><strong>Details:</strong> {error_entry['details']}</p>" if error_entry.get('details') else ""}
    <hr>
    <p style="color: #6b7280; font-size: 12px;">This is an immediate alert. A full digest will be sent at {DIGEST_HOUR}:00 AM.</p>
    """
    send_email_via_resend(subject, html_body)


def send_daily_digest():
    """Send daily digest of accumulated errors."""
    error_log = load_error_log()
    errors = error_log.get("errors", [])

    if not errors:
        log_verbose("No errors to report in daily digest")
        return

    # Count by type
    error_counts = {}
    for err in errors:
        err_type = err.get("type", "Unknown")
        error_counts[err_type] = error_counts.get(err_type, 0) + 1

    # Build summary
    summary_rows = "".join([
        f"<tr><td style='padding: 8px; border: 1px solid #e5e7eb;'>{t}</td>"
        f"<td style='padding: 8px; border: 1px solid #e5e7eb; text-align: center;'>{c}</td></tr>"
        for t, c in sorted(error_counts.items(), key=lambda x: -x[1])
    ])

    # Build detail list (last 20 errors)
    recent_errors = errors[-20:]
    error_details = "".join([
        f"""<div style="margin-bottom: 12px; padding: 12px; background: #fef2f2; border-radius: 4px;">
            <p style="margin: 0; font-size: 12px; color: #6b7280;">{err['timestamp']}</p>
            <p style="margin: 4px 0; font-weight: bold;">{err['type']}</p>
            <p style="margin: 0;">{err['message'][:200]}</p>
        </div>"""
        for err in reversed(recent_errors)
    ])

    subject = f"[DocGen] Daily Error Digest - {len(errors)} error(s)"
    html_body = f"""
    <h2>DocGen Worker - Daily Error Digest</h2>
    <p>Period: Last 24 hours | Total errors: <strong>{len(errors)}</strong></p>

    <h3>Summary by Type</h3>
    <table style="border-collapse: collapse; width: 100%; max-width: 400px;">
        <tr style="background: #f3f4f6;">
            <th style="padding: 8px; border: 1px solid #e5e7eb; text-align: left;">Error Type</th>
            <th style="padding: 8px; border: 1px solid #e5e7eb; text-align: center;">Count</th>
        </tr>
        {summary_rows}
    </table>

    <h3>Recent Errors (last 20)</h3>
    {error_details}

    <hr>
    <p style="color: #6b7280; font-size: 12px;">
        DocGen Worker running on TrueNAS<br>
        Next digest at {DIGEST_HOUR}:00 AM
    </p>
    """

    if send_email_via_resend(subject, html_body):
        # Clear errors after successful send
        error_log["errors"] = []
        error_log["last_digest_sent"] = datetime.now().isoformat()
        save_error_log(error_log)


def check_and_send_digest():
    """Check if it's time to send the daily digest."""
    now = datetime.now()
    error_log = load_error_log()

    # Check if we should send digest (at DIGEST_HOUR, once per day)
    if now.hour == DIGEST_HOUR:
        last_sent = error_log.get("last_digest_sent")
        if last_sent:
            last_sent_dt = datetime.fromisoformat(last_sent)
            # Don't send if already sent today
            if last_sent_dt.date() == now.date():
                return

        # Send digest if there are errors
        if error_log.get("errors"):
            logger.info("Sending daily error digest...")
            send_daily_digest()


# ---------------------------------------------------------------------------
# JOB PROCESSOR
# ---------------------------------------------------------------------------

def process_job(job, signer):
    log_verbose(f"process_job called with job: {json.dumps(job, indent=2)}")

    DRIVE = get_drive_service()
    log_verbose("Drive service initialized")

    job_id = job["jobId"]
    doc_code = job["docCode"]
    parent_config_code = job.get("parentConfigCode", doc_code)  # Fallback to docCode if not present
    team = str(job["teamNumber"])
    judge = job["judgeName"]

    # Check if job was recently processed (deduplication)
    if is_job_recently_processed(job_id):
        logger.info(f"Skipping job {job_id} - already processed recently")
        return True

    # short unique ID for traceability
    unique = hashlib.sha1(job_id.encode()).hexdigest()[:16]
    log_verbose(f"Generated unique ID: {unique}")

    base_url = job["baseFileUrl"]
    folder_id = job["personalizedFolderId"]
    filename = job["suggestedFileName"]

    log_verbose(f"Job details: base_url={base_url}, folder_id={folder_id}, filename={filename}")

    logger.info(f"Processing job {job_id} ({doc_code}, team {team}, judge {judge})")

    base_file_id = extract_drive_id(base_url)
    log_verbose(f"Extracted base file ID: {base_file_id}")

    base_pdf_path = os.path.join(TEMP_DIR, f"{unique}_base.pdf")
    water_path   = os.path.join(TEMP_DIR, f"{unique}_water.pdf")
    signed_path  = os.path.join(TEMP_DIR, f"{unique}_signed.pdf")

    try:
        log_verbose(f"Downloading base file to {base_pdf_path}...")
        drive_download(DRIVE, base_file_id, base_pdf_path)
        log_verbose(f"Download complete. File size: {os.path.getsize(base_pdf_path)} bytes")

        log_verbose(f"Applying watermark...")
        personalize_pdf(base_pdf_path, water_path, unique, judge)
        log_verbose(f"Watermark complete. File size: {os.path.getsize(water_path)} bytes")

        log_verbose(f"Signing PDF...")
        sign_pdf(water_path, signed_path, signer)
        log_verbose(f"Signing complete. File size: {os.path.getsize(signed_path)} bytes")

        log_verbose(f"Uploading to folder {folder_id}...")
        file_id = drive_upload(DRIVE, signed_path, filename, folder_id)
        log_verbose(f"Upload complete. New file ID: {file_id}")

        log_verbose(f"Submitting result to GAS API...")
        api_submit_result(job_id, doc_code, parent_config_code, team, judge, file_id)
        log_verbose("Result submitted successfully")

        # Mark job as successfully processed
        save_processed_job(job_id)

        logger.info(f"Job {job_id} completed: {file_id}")
        return True

    except Exception as e:
        logger.exception(f"Error processing job {job_id}: {e}")

        # Report failure to GAS
        error_msg = redact(e)[:200]  # Truncate long errors
        api_submit_failure(job_id, doc_code, team, judge, error_msg)

        # Log error for daily digest
        add_error_to_log(
            error_type="Job Processing Error",
            message=str(e),
            details=f"Job: {job_id}, Doc: {doc_code}, Team: {team}",
            is_critical=False
        )

        return False

    finally:
        for p in (base_pdf_path, water_path, signed_path):
            if os.path.exists(p):
                try: os.remove(p)
                except: pass


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def main():
    global VERBOSE

    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Parse command line arguments
    global VERBOSE, JOB_DEDUP_HOURS
    parser = argparse.ArgumentParser(description="BAJA DocGen Worker - Personalized document generator")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL, help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    parser.add_argument("--dedup-hours", type=float, default=JOB_DEDUP_HOURS, help=f"Don't reprocess jobs completed in the last N hours (default: {JOB_DEDUP_HOURS})")
    args = parser.parse_args()

    VERBOSE = args.verbose
    poll_interval = args.poll_interval
    JOB_DEDUP_HOURS = args.dedup_hours

    logger.info("=" * 60)
    logger.info("Starting BAJA DocGen Worker")
    logger.info("=" * 60)
    logger.info(f"Verbose mode: {VERBOSE}")
    logger.info(f"Poll interval: {poll_interval}s")
    logger.info(f"Workers: {NUM_WORKERS}")
    logger.info(f"Dedup hours: {JOB_DEDUP_HOURS}h")
    logger.info(f"API URL: {API_BASE_URL[:50]}...")
    logger.info(f"Event code: {EVENT_CODE}")
    sa_source = f"env {SERVICE_ACCOUNT_ENV}" if os.environ.get(SERVICE_ACCOUNT_ENV, "").strip() else SERVICE_ACCOUNT_JSON
    logger.info(f"Service account: {sa_source}")
    logger.info(f"Temp directory: {TEMP_DIR}")
    logger.info("=" * 60)

    if not PFX_PASSWORD.strip():
        raise RuntimeError(
            "PFX_PASSWORD is not set or blank. Set it to a non-empty value "
            "before starting the worker (see .env.example)."
        )

    log_verbose("Checking for PFX certificate...")
    create_pfx_if_missing(PFX_PATH, PFX_PASSWORD)
    log_verbose("Loading signer from PFX...")
    signer = load_signer_from_pfx(PFX_PATH, PFX_PASSWORD)
    log_verbose("Signer loaded successfully")

    pool = ThreadPoolExecutor(max_workers=NUM_WORKERS)
    log_verbose(f"Thread pool created with {NUM_WORKERS} workers")

    jobs_processed_total = 0
    last_error = None
    poll_count = 0

    while not SHUTDOWN_REQUESTED:
        poll_count += 1
        log_verbose(f"--- Poll #{poll_count} starting ---")

        # Check if it's time to send daily digest
        check_and_send_digest()

        try:
            log_verbose("Fetching jobs from API...")
            jobs = api_get_jobs()
            update_status_file("running", jobs_processed_total, last_error)
            consecutive_api_failures = 0  # Reset on success
        except Exception as e:
            logger.error(f"API getJobs failed: {e}")
            last_error = redact(e)[:200]
            update_status_file("error", jobs_processed_total, last_error)

            # Track consecutive failures for critical alert
            consecutive_api_failures = getattr(main, '_api_failures', 0) + 1
            main._api_failures = consecutive_api_failures

            # After 3 consecutive failures, send critical alert
            is_critical = consecutive_api_failures >= 3
            add_error_to_log(
                error_type="API Connection Error",
                message=str(e),
                details=f"Consecutive failures: {consecutive_api_failures}",
                is_critical=is_critical
            )

            log_verbose(f"Sleeping {poll_interval}s before retry...")
            time.sleep(poll_interval)
            continue

        if jobs and not SHUTDOWN_REQUESTED:
            logger.info(f"Received {len(jobs)} jobs")
            futures = [pool.submit(process_job, job, signer) for job in jobs]

            # Count successful jobs (with timeout to allow checking shutdown flag)
            for future in as_completed(futures, timeout=None):
                if SHUTDOWN_REQUESTED:
                    logger.info("Shutdown requested, waiting for current jobs to complete...")
                try:
                    success = future.result(timeout=1)
                    if success:
                        jobs_processed_total += 1
                except TimeoutError:
                    # Job still running, will be picked up on next iteration
                    pass
                except Exception as e:
                    logger.error(f"Future exception: {e}")
                    last_error = redact(e)[:200]
        else:
            if not SHUTDOWN_REQUESTED:
                logger.info(f"Poll #{poll_count}: No jobs available")

        if SHUTDOWN_REQUESTED:
            break

        # Update status after processing
        update_status_file("idle", jobs_processed_total, last_error)
        log_verbose(f"Poll #{poll_count} complete. Total jobs processed: {jobs_processed_total}")
        log_verbose(f"Sleeping {poll_interval}s...")

        # Sleep in small increments to allow faster shutdown response
        for _ in range(poll_interval):
            if SHUTDOWN_REQUESTED:
                break
            time.sleep(1)

    # Clean shutdown
    logger.info("Shutting down thread pool...")
    pool.shutdown(wait=True)
    update_status_file("stopped", jobs_processed_total, last_error)
    logger.info(f"Worker stopped. Total jobs processed: {jobs_processed_total}")


if __name__ == "__main__":
    main()
