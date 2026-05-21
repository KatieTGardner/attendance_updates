import os
import datetime
import sys
import random
import pandas as pd
import paramiko

# === 1. CONFIGURATION ===
MASTER_FILE = "attendance.csv"

SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = 22
SFTP_USER = os.environ.get("SFTP_USER")
SFTP_PASS = os.environ.get("SFTP_PASS")

if not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP Secrets are missing from repository settings!")
    sys.exit(1)

# === 2. LOAD TEMPLATE FILE ===
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
print(f"Loading master file template: {MASTER_FILE}")

try:
    df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
except Exception as e:
    print(f"CRITICAL ERROR: Could not read template file: {e}")
    sys.exit(1)

print(f"Processing {len(df)} total rows. Overwriting attendance fields...")

# === 3. VALIDATE / NORMALIZE COLUMNS ===
required_columns = [
    "Student_id",
    "School_id",
    "Section_id",
    "Attendance_date",
    "Attendance_type",
    "Attendance_status",
    "Excuse_code",
    "Attendance_id",
]

for col in required_columns:
    if col not in df.columns:
        df[col] = ""

# Keep only one row per student/school/section combination if the template has duplicates.
# Remove this block if you intentionally want duplicate attendance records per student.
df = df.drop_duplicates(
    subset=["Student_id", "School_id", "Section_id"],
    keep="first"
).reset_index(drop=True)

# === 4. GENERATE DAILY ATTENDANCE VALUES ===
status_options = ["Present", "Tardy", "Absent"]
status_weights = [0.85, 0.08, 0.07]

absent_excuse_codes = ["Illness", "Family_Emergency", "Unexcused"]
tardy_excuse_codes = ["Late_Bus", "Traffic", "Unexcused"]

for idx in range(len(df)):
    assigned_status = random.choices(
        status_options,
        weights=status_weights,
        k=1
    )[0]

    df.at[idx, "Attendance_date"] = today_str
    df.at[idx, "Attendance_type"] = "daily"
    df.at[idx, "Attendance_status"] = assigned_status

    if assigned_status == "Absent":
        df.at[idx, "Excuse_code"] = random.choice(absent_excuse_codes)
    elif assigned_status == "Tardy":
        df.at[idx, "Excuse_code"] = random.choice(tardy_excuse_codes)
    else:
        df.at[idx, "Excuse_code"] = ""

    df.at[idx, "Attendance_id"] = f"sisid-{today_str}-{idx + 1:04d}"

# Optional: enforce clean column order
df = df[required_columns]

# === 5. SAVE DAILY FILE LOCALLY ===
df.to_csv(MASTER_FILE, index=False)
print(f"✅ Finished updating fields. Prepared {len(df)} rows for export.")
print(df[["Student_id", "School_id", "Attendance_date", "Attendance_status", "Excuse_code", "Attendance_id"]].head(10))

# === 6. AUTOMATED SFTP TRANSFER ===
print(f"Opening secure transport handshake to {SFTP_HOST}...")

transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))

try:
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)

    remote_target_path = "/home/decorous-school-4198/attendance.csv"
    print(f"Streaming target write file directly to destination path: {remote_target_path}")

    sftp.put(MASTER_FILE, remote_target_path)
    print("🚀 Cloud Export Completed Successfully!")

    sftp.close()

finally:
    transport.close()
