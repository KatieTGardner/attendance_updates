import os
import datetime
import pandas as pd
import paramiko

# === 1. CONFIGURATION ===
# File Locations
LOCAL_DIRECTORY = r"C:\Users\YourName\Documents"  # Use your folder path here
MASTER_FILE = os.path.join(LOCAL_DIRECTORY, "export_data.csv")

# SFTP Server Details
SFTP_HOST = "sftp.yourdomain.com"
SFTP_PORT = 22
SFTP_USER = "your_sftp_username"
SFTP_PASS = "your_sftp_password"
REMOTE_DIRECTORY = "/remote_directory/"

# === 2. DYNAMIC DATA PROCESSING ===
# Calculate today's date in various formats
today_str = datetime.datetime.now().strftime("%Y-%m-%d")  # e.g., 2026-05-21

print(f"Opening master data template: {MASTER_FILE}")
# Read the master file into a data processing frame
df = pd.read_csv(MASTER_FILE)

# A. Update the primary date column to today's date
# (Replace 'Attendance_Date' with the exact header name used in your file)
if 'Attendance_Date' in df.columns:
    df['Attendance_Date'] = today_str
    print("✅ Successfully updated all rows to today's date.")

# B. OPTIONAL HIGHER-LEVEL UPDATES
# Update optional columns safely if they exist in your template file layout
if 'Attendance_status' in df.columns:
    df['Attendance_status'] = 'Present'  # Or whatever default value you need to enforce
    print("✅ Reset Attendance_status fields.")

if 'Excuse_code' in df.columns:
    df['Excuse_code'] = 'N/A'  # Clear or default the excuse code
    print("✅ Cleaned Excuse_code fields.")

if 'Attendance_id' in df.columns:
    # Example: Generates a unique sequential ID string combined with today's date
    df['Attendance_id'] = [f"ATT-{today_str}-{i+1:04d}" for i in range(len(df))]
    print("✅ Created new unique dynamic Attendance_id values.")

# Generate the unique daily timestamped file name
daily_filename = f"export_data_{today_str}.csv"
output_path = os.path.join(LOCAL_DIRECTORY, daily_filename)

# Save the updated data as a fresh, clean CSV file
df.to_csv(output_path, index=False)
print(f"Saved freshly updated file locally at: {output_path}")

# === 3. AUTOMATED SFTP TRANSFER ===
print(f"Initiating secure handshake to {SFTP_HOST}...")
transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
try:
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    # Change to the remote target directory folder layout
    sftp.chdir(REMOTE_DIRECTORY)
    
    remote_target_path = remote_directory + daily_filename
    print(f"Uploading {daily_filename} to server...")
    sftp.put(output_path, daily_filename)
    print("🚀 SFTP Export Completed Successfully!")
    
    sftp.close()
finally:
    transport.close()

# 4. CLEAN UP (Optional)
# Uncomment the line below if you want to delete the daily file copy locally after upload
# os.remove(output_path)
