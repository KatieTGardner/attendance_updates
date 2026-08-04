import os
import sys
import datetime
import random
import pandas as pd
import paramiko

# === CONFIGURATION ===
MASTER_FILE = "attendance.csv"
OUTPUT_FILE = "attendance_processed.csv"
REMOTE_FILE = "attendance.csv"

SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = 22
SFTP_USER = os.environ.get("SFTP_USER")
SFTP_PASS = os.environ.get("SFTP_PASS")

# NOTE on this account's SFTP setup: logging in as the district user (e.g.
# "decorous-school-4198") already drops you into that district's home
# directory on Clever's shared SFTP server -- you do NOT need to additionally
# cd into "home/<district>" after connecting. SFTP_REMOTE_DIR should normally
# just be "." (upload directly into the login's default directory). Setting
# it to "home/<district>" on top of that login will create a duplicate
# nested subdirectory.
SFTP_REMOTE_DIR = os.environ.get("SFTP_REMOTE_DIR", ".")

if not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP secrets are missing.")
    sys.exit(1)

# === Additional static files to keep in sync alongside attendance ===
# Map of local filename -> remote filename. Adjust LOCAL_DATA_DIR if these
# files don't live next to this script.
LOCAL_DATA_DIR = os.environ.get("LOCAL_DATA_DIR", ".")
ADDITIONAL_FILES = [
    "students.csv",
    "staff.csv",
    "enrollments.csv",
    "teachers.csv",
    "sections.csv",
    "schools.csv",
]

# === DATE / RUN INFO ===
now = datetime.datetime.utcnow()
attendance_date = now.strftime("%Y-%m-%dT00:00:00Z")
run_id = now.strftime("%Y%m%d%H%M%S")

print(f"Loading master file: {MASTER_FILE}")
try:
    df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
except Exception as e:
    print(f"CRITICAL ERROR: Could not read master file: {e}")
    sys.exit(1)
print(f"Loaded {len(df)} rows")

# === NORMALIZE HEADERS ===
df.columns = [col.strip() for col in df.columns]
column_aliases = {
    "Attendance": "Attendance_status",
    "Attendance_": "Attendance_status",
    "attendance": "Attendance_status",
    "attendance_status": "Attendance_status",
}
df = df.rename(columns={old: new for old, new in column_aliases.items() if old in df.columns})

# === REQUIRED STABLE COLUMNS ===
required_source_columns = [
    "Student_id",
    "School_id",
]
missing = [col for col in required_source_columns if col not in df.columns]
if missing:
    print(f"CRITICAL ERROR: Missing required source columns: {missing}")
    print(f"Found columns: {list(df.columns)}")
    sys.exit(1)

# Section_id is optional, but expected in output
if "Section_id" not in df.columns:
    df["Section_id"] = ""

# === GENERATE FRESH ATTENDANCE EACH RUN ===
attendance_status_options = [
    "present",
    "present",
    "present",
    "present",
    "present",
    "absent",
    "tardy",
]
excuse_code_map = {
    "present": "",
    "absent": "excusecodeAbsent",
    "tardy": "excusecodeTardy",
}
df["Attendance_date"] = attendance_date
df["Attendance_type"] = "daily"
df["Attendance_status"] = [
    random.choice(attendance_status_options)
    for _ in range(len(df))
]
df["Excuse_code"] = df["Attendance_status"].map(excuse_code_map)
df["Attendance_id"] = [
    f"sisid{run_id}{str(i + 1).zfill(5)}"
    for i in range(len(df))
]

# === FINAL OUTPUT COLUMNS ===
output_columns = [
    "Student_id",
    "School_id",
    "Section_id",
    "Attendance_date",
    "Attendance_type",
    "Attendance_status",
    "Excuse_code",
    "Attendance_id",
]
for col in output_columns:
    df[col] = df[col].astype(str).str.strip()
df = df[output_columns]

# === WRITE FRESH CSV ===
df.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8",
    lineterminator="\n"
)
print(f"Saved fresh attendance file: {OUTPUT_FILE}")
print(f"Attendance date: {attendance_date}")
print(f"Unique run ID: {run_id}")


def ensure_remote_dir(sftp, remote_dir):
    """Create the remote directory (and parents) if it doesn't exist yet."""
    parts = remote_dir.strip("/").split("/")
    path = ""
    for part in parts:
        path = f"{path}/{part}" if path else part
        try:
            sftp.stat(path)
        except FileNotFoundError:
            print(f"Remote directory '{path}' not found, creating it.")
            sftp.mkdir(path)


# === UPLOAD TO SFTP ===
try:
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)

    login_cwd = sftp.normalize(".")
    print(f"Logged in as '{SFTP_USER}', default directory is: {login_cwd}")

    remote_dir = SFTP_REMOTE_DIR.rstrip("/")
    if remote_dir not in ("", "."):
        ensure_remote_dir(sftp, remote_dir)
    else:
        remote_dir = "."

    # Build the full list of (local_path, remote_filename) pairs to sync:
    # the freshly generated attendance file plus the static reference files.
    files_to_sync = [(OUTPUT_FILE, REMOTE_FILE)]
    for filename in ADDITIONAL_FILES:
        local_path = os.path.join(LOCAL_DATA_DIR, filename)
        files_to_sync.append((local_path, filename))

    failures = []
    for local_path, remote_filename in files_to_sync:
        remote_path = f"{remote_dir}/{remote_filename}"
        if not os.path.exists(local_path):
            print(f"WARNING: local file '{local_path}' not found, skipping upload of {remote_filename}.")
            failures.append(remote_filename)
            continue
        try:
            print(f"Uploading {local_path} -> {remote_path}")
            sftp.put(local_path, remote_path)
        except Exception as e:
            print(f"ERROR: failed to upload {remote_filename}: {e}")
            failures.append(remote_filename)

    sftp.close()
    transport.close()

    if failures:
        print(f"CRITICAL ERROR: The following files failed to sync: {failures}")
        sys.exit(1)

    print("Upload complete. All files synced.")
except Exception as e:
    print(f"CRITICAL ERROR: SFTP upload failed: {e}")
    sys.exit(1)
