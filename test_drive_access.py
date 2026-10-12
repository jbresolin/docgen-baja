import os
import io
import tempfile
from datetime import datetime

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload


# -------------------------------------------------------
# CONFIG
# -------------------------------------------------------

SERVICE_ACCOUNT_JSON = "./service-account.json"
MASTER_FOLDER_ID = os.environ.get("SHARED_DRIVE_ID", "")   # <== MUST SET THIS
if not MASTER_FOLDER_ID:
    raise SystemExit("Set the SHARED_DRIVE_ID environment variable to the Shared Drive ID to test.")


# -------------------------------------------------------
# AUTHENTICATE
# -------------------------------------------------------

def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON, scopes=scopes
    )
    service = build("drive", "v3", credentials=creds)
    return service

drive = get_drive_service()


# -------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------

def list_master_folder():
    print("\n--- STEP 1: Listing master folder contents ---")

    results = drive.files().list(
        q=f"'{MASTER_FOLDER_ID}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        corpora="drive",
        driveId=MASTER_FOLDER_ID  # KEY CHANGE
    ).execute()


    files = results.get("files", [])

    for f in files[:20]:
        print(f"[{f['mimeType']}] {f['name']} (ID: {f['id']})")

    print(f"Total items found: {len(files)}\n")
    return True


def create_dummy_pdf(path):
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(100, 750, "BAJA SAE Test PDF")
    c.drawString(100, 730, f"Generated: {datetime.now()}")
    c.drawString(100, 710, "Service Account Access Check")
    c.save()


def upload_dummy_pdf(local_path):
    print("--- STEP 2: Uploading dummy PDF ---")

    file_metadata = {
        'name': "TEST_UPLOAD.pdf",
        'parents': [MASTER_FOLDER_ID]
    }

    media = MediaFileUpload(local_path, mimetype="application/pdf")

    uploaded = drive.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()


    file_id = uploaded.get("id")
    print(f"Uploaded file ID: {file_id}\n")
    return file_id


def download_back(file_id, download_path):
    print("--- STEP 3: Downloading file back ---")

    request = drive.files().get_media(
        fileId=file_id,
        supportsAllDrives=True
    )

    fh = io.FileIO(download_path, "wb")

    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    exists = os.path.exists(download_path)
    print(f"Downloaded file exists? {exists}")
    print(f"Downloaded to: {download_path}\n")

    return exists


def delete_test_file(file_id):
    print("--- STEP 4: Deleting test file from Drive ---")

    drive.files().delete(
        fileId=file_id,
        supportsAllDrives=True
    ).execute()


    print("Deleted test file.\n")
    return True


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------

def main():
    print("=====================================================")
    print(" BAJA SAE GOOGLE DRIVE ACCESS TEST ")
    print("=====================================================\n")

    # 1) List
    try:
        list_ok = list_master_folder()
    except Exception as e:
        print("Error listing folder:", e)
        list_ok = False

    # 2) Create dummy PDF
    tmp_dir = tempfile.gettempdir()
    dummy_pdf_path = os.path.join(tmp_dir, "baja_test_upload.pdf")
    create_dummy_pdf(dummy_pdf_path)

    # 3) Upload
    try:
        uploaded_file_id = upload_dummy_pdf(dummy_pdf_path)
        upload_ok = True
    except Exception as e:
        print("Error uploading file:", e)
        upload_ok = False
        uploaded_file_id = None

    # 4) Download
    if uploaded_file_id:
        download_path = os.path.join(tmp_dir, "baja_test_download.pdf")
        try:
            download_ok = download_back(uploaded_file_id, download_path)
        except Exception as e:
            print("Error downloading file:", e)
            download_ok = False
    else:
        download_ok = False

    # 5) Delete
    if uploaded_file_id:
        try:
            delete_ok = delete_test_file(uploaded_file_id)
        except Exception as e:
            print("Error deleting file:", e)
            delete_ok = False
    else:
        delete_ok = False

    # SUMMARY
    print("\n=====================================================")
    print(" ACCESS TEST RESULT SUMMARY")
    print("=====================================================")

    print(f"Folder Listing OK?      {list_ok}")
    print(f"Upload OK?              {upload_ok}")
    print(f"Download OK?            {download_ok}")
    print(f"Delete OK?              {delete_ok}")

    if all([list_ok, upload_ok, download_ok, delete_ok]):
        print("\n🎉 ALL TESTS PASSED — Service account has full access!\n")
    else:
        print("\n❌ SOME TESTS FAILED — check sharing and folder ID.\n")


if __name__ == "__main__":
    main()
