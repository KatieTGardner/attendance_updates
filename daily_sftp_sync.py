import os
import datetime
import sys
import random
import pandas as pd
import paramiko

# === 1. CONFIGURATION ===
MASTER_FILE = "attendance.csv"

# Fetch secure environmental credentials from GitHub Secrets
SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = 22
SFTP_USER = os.environ.get("SFTP_USER")
SFTP_PASS = os.environ.get("SFTP_PASS")

# Defensive safeguard: Halt early if secrets are unconfigured
if not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP Secrets are missing from repository settings!")
    sys.exit(1)

# === 2. SMART IN-PLACE OVERWRITE PROCESSING ===
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
print(f"Loading master file template: {MASTER_FILE}")

try:
    # Read the full file directly out of your repository
    df = pd.read_csv(MASTER_FILE)
except Exception as e:
    print(f"CRITICAL ERROR: Could not read template file: {e}")
    sys.exit(1)

print(f"Processing {len(df)} total rows. Overwriting attendance fields...")

# Clean Capitalized Options matching Clever's exact required Enum structures
status_options = ["Present", "Tardy", "Absent"]
status_weights = [0.85, 0.08, 0.07]

# Loop through every row in the file to selectively modify only attendance parameters
for idx in range(len(df)):
    # 1. Update the date column to today's date
    df.at[idx, 'Attendance_date'] = today_str
    
    # 2. Extract status as a clean string asset (Fixed with trailing)
    assigned_status = random.choices(status_options, weights=status_weights, k=1)
    df.at[idx, 'Attendance_status'] = assigned_status
    
    # 3. Dynamic excuse code generator matching the selected status string
    if assigned_status == "Absent":
        df.at[idx, 'Excuse_code'] = random.choice(["Illness", "Family_Emergency", "Unexcused"])
    elif assigned_status == "Tardy":
        df.at[idx, 'Excuse_code'] = random.choice(["Late_Bus", "Traffic", "Unexcused"])
    else:
        df.at[idx, 'Excuse_code'] = "N/A"
        
    # 4. Generate a clean unique tracking code for this day's run
    df.at[idx, 'Attendance_id'] = f"sisid{today_str}-{idx+1:04d}"

# Ensure Attendance_type matches daily tracking structure
if 'Attendance_type' in df.columns:
    df['Attendance_type'] = "daily"

# Save the freshly updated values back to the staging path
df.to_csv(MASTER_FILE, index=False)
print(f"✅ Finished updating fields. Prepared {len(df)} rows for export.")

# === 3. AUTOMATED SFTP TRANSFER (EXPLICIT SUBFOLDER ROUTING) ===
print(f"Opening secure transport handshake to {SFTP_HOST}...")
transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
try:
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    # FORCED TARGET PATHWAY: Overwrites the target subfolder file explicitly
    remote_target_path = "/home/decorous-school-4198/attendance.csv"
    print(f"Streaming target write file directly to destination path: {remote_target_path}")
    
    # Send the processed file over to your partner's environment
    sftp.put(MASTER_FILE, remote_target_path)
    print("🚀 Cloud Export Completed Successfully!")
    
    sftp.close()
finally:
    transport.close()
