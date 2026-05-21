import os
import datetime
import sys
import random
import pandas as pd
import paramiko

# === 1. CONFIGURATION ===
# Paste your list of active student/school records here
# Format: (Student_id, School_id)
STUDENT_DATA_RECORDS = [
    ("574095232", "13K123"),
    ("958709227", "13K123"),
    ("243615677", "13K123"),
    ("381370052", "13K123"),
    ("992399521", "13K123"),
    ("159541752", "13K123"),
    ("142858690", "27Q321"),
    ("526702789", "27Q321"),
    ("406243122", "27Q321"),
    ("281062504", "27Q321"),
    ("189563051", "27Q321"),
    ("260768059", "27Q321"),
    ("153274070", "02M800")
    # 💡 Pro-tip: You can paste additional rows following this exact format above!
]

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

# === 2. DYNAMIC ATTENDANCE GENERATION ===
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
print(f"Generating live data rows for date: {today_str}")

# Setup status options and weighting (e.g., 80% Present, 10% Tardy, 10% Absent)
status_options = ["Present", "Tardy", "Absent"]
status_weights = [0.80, 0.10, 0.10]

# List to hold processed row structures
compiled_rows = []

for i, (student_id, school_id) in enumerate(STUDENT_DATA_RECORDS):
    # Randomly assign a status based on our weights
    assigned_status = random.choices(status_options, weights=status_weights, k=1)
    
    # Generate excuse codes dynamically based on the attendance status outcome
    if assigned_status == "Absent":
        assigned_excuse = random.choice(["Illness", "Family_Emergency", "Unexcused"])
    elif assigned_status == "Tardy":
        assigned_excuse = random.choice(["Late_Bus", "Traffic", "Unexcused"])
    else:
        assigned_excuse = "N/A"
        
    # Build a clean row matching Clever's exact CSV column specifications
    compiled_rows.append({
        "Attendance_id": f"ATT-{today_str}-{i+1:04d}",
        "Attendance_date": today_str,
        "Attendance_status": assigned_status,
        "Excuse_code": assigned_excuse,
        "Student_id": str(student_id),
        "Section_id": "",     # Left blank per specifications note for standard profiles
        "School_id": str(school_id),
        "Attendance_type": "daily"
    })

# Convert compile list cleanly to a DataFrame frame
df = pd.DataFrame(compiled_rows)

# Enforce static delivery name layout so it overwrites downstream target correctly
daily_filename = "attendance.csv"
df.to_csv(daily_filename, index=False)
print(f"Successfully generated dynamic sync payload file containing {len(df)} rows.")

# === 3. AUTOMATED SFTP TRANSFER (S3 GATEWAY COMPATIBLE) ===
print(f"Opening secure transport handshake to {SFTP_HOST}...")
transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
try:
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    # Safely handle path resolution without utilizing sftp.chdir()
    if REMOTE_DIRECTORY and REMOTE_DIRECTORY != "/":
        remote_folder = REMOTE_DIRECTORY.strip("/")
        remote_target_path = f"/{remote_folder}/{daily_filename}"
    else:
        remote_target_path = f"/{daily_filename}"
        
    print(f"Streaming target write file directly to pathway destination: {remote_target_path}")
    
    # Direct streaming put execution
    sftp.put(daily_filename, remote_target_path)
    print("🚀 Cloud Export Completed Successfully!")
    
    sftp.close()
finally:
    transport.close()
