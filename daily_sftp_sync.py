import os
import datetime
import sys
import pandas as pd
import paramiko

# === 1. CONFIGURATION ===
MASTER_FILE = "attendance.csv"

# Fetch secure environmental credentials from GitHub Secrets
SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = 22
SFTP_USER = os.environ.get("SFTP_USER")
SFTP_PASS = os.environ.get("SFTP_PASS")
REMOTE_DIRECTORY = os.environ.get("REMOTE_DIRECTORY", "/")

# Defensive safeguard: Halt early if secrets are unconfigured
if not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP Secrets are missing from repository settings!")
    sys.exit(1)

# === 2. DYNAMIC DATA PROCESSING ===
today_str = datetime.datetime.now().strftime("%Y-%m-%d")

print(f"Parsing data sheet template: {MASTER_FILE}")
df = pd.read_csv(MASTER_FILE)

# Dynamically stamp today's execution date onto your tracking columns
for col in df.columns:
    if col.lower() in ['date', 'attendance_date', 'timestamp']:
        df[col] = today_str
        print(f"✅ Auto-updated date field values in column: {col}")

# Programmatically overwrite data row variables
if 'Attendance_status' in df.columns:
    df['Attendance_status'] = 'Present'
    print("✅ Stabilized Attendance_status data rows.")

if 'Excuse_code' in df.columns:
    df['Excuse_code'] = 'N/A'
    print("✅ Defaulted Excuse_code parameters.")

if 'Attendance_id' in df.columns:
    df['Attendance_id'] = [f"ATT-{today_str}-{i+1:04d}" for i in range(len(df))]
    print("✅ Assigned unique, chronological tracking IDs.")

# FIXED: Kept the name static so it cleanly overwrites the file on their end daily
daily_filename = "attendance.csv"

# Save the freshly manipulated dataframe locally inside the runner instance container
df.to_csv(daily_filename, index=False)
print(f"Generated staging export file: {daily_filename}")

# === 3. AUTOMATED SFTP TRANSFER (S3 GATEWAY COMPATIBLE) ===
print(f"Opening secure transport handshake to {SFTP_HOST}...")
transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
try:
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    # Safely handle the path resolution without utilizing sftp.chdir()
    if REMOTE_DIRECTORY and REMOTE_DIRECTORY != "/":
        remote_folder = REMOTE_DIRECTORY.strip("/")
        remote_target_path = f"/{remote_folder}/{daily_filename}"
    else:
        remote_target_path = f"/{daily_filename}"
        
    print(f"Direct streaming target file write destination pathway set to: {remote_target_path}")
    
    # Directly streaming the payload to bypass AWS Transfer Family ListBucket validation blocks
    sftp.put(daily_filename, remote_target_path)
    print("🚀 Cloud Export Completed Successfully!")
    
    sftp.close()
finally:
    transport.close()
