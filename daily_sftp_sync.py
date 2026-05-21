import os
import sys
import datetime
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
SFTP_REMOTE_DIR = os.environ.get("SFTP_REMOTE_DIR", ".")

if not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP secrets are missing.")
    sys.exit(1)

# === DATE FORMAT REQUIRED BY SYNC ===
attendance_date = datetime.datetime.now().strftime("%Y-%m-%dT00:00:00Z")
date_for_id = datetime.datetime.now().strftime("%Y%m%d")

print(f"Loading master file: {MASTER_FILE}")

try:
    df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
except Exception as e:
    print(f"CRITICAL ERROR: Could not read template file: {e}")
    sys.exit(1)

print(f"Processing {len(df)} rows")

# === NORMALIZE HEADERS ===
df.columns = [col.strip() for col in df.columns]

column_aliases = {
    "Attendance": "Attendance_",
    "attendance": "Attendance_",
    "attendance_status": "Attendance_",
    "Attendance_status": "Attendance_",
}

df = df.rename(columns={old: new for old, new in column_aliases.items() if old in df.columns})

required_columns = [
    "Student_id",
    "School_id",
    "Section_id",
    "Attendance_date",
    "Attendance_type",
    "Attendance_",
    "Excuse_code",
    "Attendance_id",
]

missing = [col for col in required_columns if col not in df.columns]
if missing:
    print(f"CRITICAL ERROR: Missing required columns: {missing}")
    print(f"Found columns: {list(df.columns)}")
    sys.exit(1)

# === NORMALIZE VALUES ===
df["Attendance_date"] = attendance_date

df["Attendance_type"] = (
    df["Attendance_type"]
    .replace("", "daily")
    .str.lower()
    .str.strip()
)

df["Attendance_"] = (
    df["Attendance_"]
    .replace("", "present")
    .str.lower()
    .str.strip()
)

# Normalize common values just in case
attendance_value_map = {
    "Present": "present",
    "Absent": "absent",
    "Tardy": "tardy",
    "present": "present",
    "absent": "absent",
    "tardy": "tardy",
}

df["Attendance_"] = df["Attendance_"].replace(attendance_value_map)

# Only keep excuse codes for non-present records
df.loc[df["Attendance_"] == "present", "Excuse_code"] = ""

df["Excuse_code"] = (
    df["Excuse_code"]
    .astype(str)
    .str.strip()
    .str.replace(" ", "_", regex=False)
)

# === SAFER ATTENDANCE IDS ===
df["Attendance_id"] = [
    f"sisid{date_for_id}{str(i + 1).zfill(5)}"
    for i in range(len(df))
]

# === FINAL CLEANUP ===
for col in required_columns:
    df[col] = df[col].astype(str).str.strip()

df = df[required_columns]

df.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8",
    lineterminator="\n"
)

print(f"Saved processed file: {OUTPUT_FILE}")

# === UPLOAD TO SFTP ===
try:
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)

    sftp = paramiko.SFTPClient.from_transport(transport)

    remote_path = f"{SFTP_REMOTE_DIR.rstrip('/')}/{REMOTE_FILE}"
    print(f"Uploading to SFTP: {remote_path}")

    sftp.put(OUTPUT_FILE, remote_path)

    sftp.close()
    transport.close()

    print("Upload complete.")

except Exception as e:
    print(f"CRITICAL ERROR: SFTP upload failed: {e}")
    sys.exit(1)
