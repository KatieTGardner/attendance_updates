#!/usr/bin/env python3
"""
Generate fresh daily + section attendance and sync it to the Clever SFTP box.

WHAT CHANGED vs. the previous version
-------------------------------------
1. Emits BOTH attendance_type="daily" and attendance_type="section" records.
2. Section records carry a real Section_id, sourced by joining enrollments.csv
   (Student_id -> Section_id). Clever's spec: Section_id "corresponds to the
   'sis_id' field on the section object in Clever."
3. Drops "present" rows. Clever only ingests negative attendance -- the spec
   lists Absent and Tardy as the only Attendance_status values. Present rows
   were ~71% of the old file and were being silently discarded on ingest.
   Set INCLUDE_PRESENT=true to go back to writing them.
4. De-duplicates daily records. The master file had 423 rows for only 14
   students, which produced 30-35 duplicate "daily" records per student per
   day. Daily is now exactly one record per student per day.
5. Statuses are correlated, not independently random. A student absent for the
   day is absent in every section that day; a tardy student is tardy in their
   first period only. Previously every row rolled the dice separately, so a
   student could be absent daily and present in all sections simultaneously.
6. Randomness is seedable (ATTENDANCE_SEED) so a run can be reproduced.
7. Optional weekend skip, timezone-correct dates, and a DRY_RUN mode.

Clever CSV spec reference:
https://support.clever.com/hc/s/articles/000001704?language=en_US
"""

import os
import sys
import csv
import random
import datetime
import hashlib
from collections import defaultdict

import pandas as pd
import paramiko

# === CONFIGURATION ===
MASTER_FILE = os.environ.get("MASTER_FILE", "attendance.csv")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "attendance_processed.csv")
REMOTE_FILE = "attendance.csv"

LOCAL_DATA_DIR = os.environ.get("LOCAL_DATA_DIR", ".")
ENROLLMENTS_FILE = os.path.join(LOCAL_DATA_DIR, "enrollments.csv")
SECTIONS_FILE = os.path.join(LOCAL_DATA_DIR, "sections.csv")

SFTP_HOST = os.environ.get("SFTP_HOST")
SFTP_PORT = int(os.environ.get("SFTP_PORT", "22"))
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

# === BEHAVIOR FLAGS ===
def _flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")

DRY_RUN = _flag("DRY_RUN")                       # generate the CSV, skip the upload
INCLUDE_PRESENT = _flag("INCLUDE_PRESENT")       # keep out-of-spec "present" rows
SKIP_WEEKENDS = _flag("SKIP_WEEKENDS")           # exit quietly on Sat/Sun
EMIT_DAILY = _flag("EMIT_DAILY", "true")
EMIT_SECTION = _flag("EMIT_SECTION", "true")

# Share of a student's sections that get their own record on a normal
# (non-absent) day. 1.0 = every enrolled section reports attendance.
SECTION_COVERAGE = float(os.environ.get("SECTION_COVERAGE", "1.0"))

# === RANDOMNESS ===
# Seed from ATTENDANCE_SEED + the date, so a given day is reproducible but
# consecutive days still differ. Unset = non-deterministic, as before.
SEED = os.environ.get("ATTENDANCE_SEED")

# === DATE / RUN INFO ===
# Previously datetime.utcnow(); a run after ~5-8pm Pacific stamped tomorrow's
# date. ATTENDANCE_TZ_OFFSET is hours from UTC (e.g. -7 for PDT, -4 for EDT).
TZ_OFFSET = float(os.environ.get("ATTENDANCE_TZ_OFFSET", "0"))
now_utc = datetime.datetime.now(datetime.timezone.utc)
local_now = now_utc + datetime.timedelta(hours=TZ_OFFSET)

# ATTENDANCE_DATE lets you backfill a specific day (YYYY-MM-DD).
if os.environ.get("ATTENDANCE_DATE"):
    target_day = datetime.datetime.strptime(
        os.environ["ATTENDANCE_DATE"], "%Y-%m-%d"
    ).date()
else:
    target_day = local_now.date()

attendance_date = target_day.strftime("%Y-%m-%dT00:00:00Z")
run_id = now_utc.strftime("%Y%m%d%H%M%S")

if SEED:
    random.seed(f"{SEED}:{target_day.isoformat()}")
    print(f"Seeded RNG with '{SEED}:{target_day.isoformat()}' (reproducible run)")

if SKIP_WEEKENDS and target_day.weekday() >= 5:
    print(f"{target_day} is a weekend and SKIP_WEEKENDS is on. Nothing to do.")
    sys.exit(0)

if not DRY_RUN and not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP secrets are missing.")
    sys.exit(1)

# === Additional static files to keep in sync alongside attendance ===
ADDITIONAL_FILES = [
    "students.csv",
    "staff.csv",
    "enrollments.csv",
    "teachers.csv",
    "sections.csv",
    "schools.csv",
]


def read_csv_flexible(path, aliases=None):
    """Read a CSV as strings, strip headers, and apply column aliases."""
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]
    if aliases:
        lowered = {c.lower(): c for c in df.columns}
        renames = {}
        for canonical, options in aliases.items():
            if canonical in df.columns:
                continue
            for option in options:
                if option.lower() in lowered:
                    renames[lowered[option.lower()]] = canonical
                    break
        if renames:
            df = df.rename(columns=renames)
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    return df


# === LOAD MASTER ROSTER ===
print(f"Loading master file: {MASTER_FILE}")
try:
    df = read_csv_flexible(
        MASTER_FILE,
        aliases={
            "Student_id": ["student_id", "sis_id", "Student_ID"],
            "School_id": ["school_id", "School_ID"],
            "Section_id": ["section_id", "Section_ID"],
        },
    )
except Exception as e:
    print(f"CRITICAL ERROR: Could not read master file: {e}")
    sys.exit(1)
print(f"Loaded {len(df)} rows")

required_source_columns = ["Student_id", "School_id"]
missing = [c for c in required_source_columns if c not in df.columns]
if missing:
    print(f"CRITICAL ERROR: Missing required source columns: {missing}")
    print(f"Found columns: {list(df.columns)}")
    sys.exit(1)

# One daily record per student per school, not one per master-file row.
roster = (
    df[["Student_id", "School_id"]]
    .loc[df["Student_id"] != ""]
    .drop_duplicates()
    .reset_index(drop=True)
)
if len(roster) != len(df):
    print(
        f"NOTE: collapsed {len(df)} master rows -> {len(roster)} unique "
        f"student/school pairs for daily attendance."
    )

student_school = dict(zip(roster["Student_id"], roster["School_id"]))

# === LOAD SECTIONS (for School_id + period ordering) ===
section_school = {}
section_period = {}
if EMIT_SECTION and os.path.exists(SECTIONS_FILE):
    sections_df = read_csv_flexible(
        SECTIONS_FILE,
        aliases={
            "Section_id": ["section_id"],
            "School_id": ["school_id"],
            "Period": ["period"],
        },
    )
    for _, row in sections_df.iterrows():
        sid = row.get("Section_id", "")
        if not sid:
            continue
        section_school[sid] = row.get("School_id", "")
        try:
            section_period[sid] = int(row.get("Period") or 0)
        except ValueError:
            section_period[sid] = 0
    print(f"Loaded {len(section_school)} sections from {SECTIONS_FILE}")
elif EMIT_SECTION:
    print(f"NOTE: {SECTIONS_FILE} not found; section School_id will fall back "
          f"to the student's school and period order will be arbitrary.")

# === LOAD ENROLLMENTS (Student_id -> [Section_id]) ===
enrollments = defaultdict(list)
if EMIT_SECTION:
    if not os.path.exists(ENROLLMENTS_FILE):
        print(f"CRITICAL ERROR: {ENROLLMENTS_FILE} not found. Section attendance "
              f"needs it to map students to sections. Set LOCAL_DATA_DIR, or set "
              f"EMIT_SECTION=false to generate daily records only.")
        sys.exit(1)
    enroll_df = read_csv_flexible(
        ENROLLMENTS_FILE,
        aliases={
            "Student_id": ["student_id", "sis_id"],
            "Section_id": ["section_id"],
            "School_id": ["school_id"],
        },
    )
    enroll_missing = [c for c in ("Student_id", "Section_id") if c not in enroll_df.columns]
    if enroll_missing:
        print(f"CRITICAL ERROR: enrollments.csv is missing {enroll_missing}. "
              f"Found columns: {list(enroll_df.columns)}")
        sys.exit(1)

    seen_pairs = set()
    for _, row in enroll_df.iterrows():
        stu, sec = row["Student_id"], row["Section_id"]
        if not stu or not sec:
            continue
        if (stu, sec) in seen_pairs:
            continue
        seen_pairs.add((stu, sec))
        enrollments[stu].append(sec)

    for stu in enrollments:
        enrollments[stu].sort(key=lambda s: (section_period.get(s, 0), s))

    covered = sum(1 for s in student_school if enrollments.get(s))
    print(f"Loaded {len(seen_pairs)} enrollments covering {covered}/"
          f"{len(student_school)} students on the roster")
    uncovered = [s for s in student_school if not enrollments.get(s)]
    if uncovered:
        print(f"NOTE: {len(uncovered)} student(s) have no enrollments and will "
              f"get daily records only: {uncovered[:5]}"
              f"{'...' if len(uncovered) > 5 else ''}")

# === ATTENDANCE MODEL ===
# Daily outcome for a student, then section outcomes derived from it.
DAILY_WEIGHTS = [("present", 0.86), ("absent", 0.09), ("tardy", 0.05)]
# Odds a student who showed up still misses/arrives late to an individual class.
SECTION_CUT_RATE = float(os.environ.get("SECTION_CUT_RATE", "0.03"))
SECTION_TARDY_RATE = float(os.environ.get("SECTION_TARDY_RATE", "0.05"))

EXCUSE_CODE_MAP = {
    "present": "",
    "absent": "excusecodeAbsent",
    "tardy": "excusecodeTardy",
}


def weighted_choice(weights):
    roll = random.random()
    cumulative = 0.0
    for value, weight in weights:
        cumulative += weight
        if roll < cumulative:
            return value
    return weights[-1][0]


records = []
_counter = 0


def add_record(student_id, school_id, section_id, att_type, status):
    """Append one attendance record. Present rows are filtered per spec."""
    global _counter
    if status == "present" and not INCLUDE_PRESENT:
        return
    _counter += 1
    records.append({
        "Student_id": student_id,
        "School_id": school_id,
        # Spec: Section_id is populated only for attendance_type="section".
        # Clever infers the type from this field when Attendance_type is blank.
        "Section_id": section_id if att_type == "section" else "",
        "Attendance_date": attendance_date,
        "Attendance_type": att_type,
        "Attendance_status": status,
        "Excuse_code": EXCUSE_CODE_MAP.get(status, ""),
        "Attendance_id": f"sisid{run_id}{str(_counter).zfill(6)}",
    })


for student_id, school_id in student_school.items():
    daily_status = weighted_choice(DAILY_WEIGHTS)

    if EMIT_DAILY:
        add_record(student_id, school_id, "", "daily", daily_status)

    if not EMIT_SECTION:
        continue

    student_sections = enrollments.get(student_id, [])
    if SECTION_COVERAGE < 1.0 and student_sections:
        keep = max(1, round(len(student_sections) * SECTION_COVERAGE))
        student_sections = student_sections[:keep]

    for index, section_id in enumerate(student_sections):
        if daily_status == "absent":
            # Out for the day -> absent from every class.
            section_status = "absent"
        elif daily_status == "tardy" and index == 0:
            # Late arrival hits the first period only.
            section_status = "tardy"
        else:
            roll = random.random()
            if roll < SECTION_CUT_RATE:
                section_status = "absent"
            elif roll < SECTION_CUT_RATE + SECTION_TARDY_RATE:
                section_status = "tardy"
            else:
                section_status = "present"

        add_record(
            student_id,
            section_school.get(section_id) or school_id,
            section_id,
            "section",
            section_status,
        )

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

out_df = pd.DataFrame(records, columns=output_columns)
if out_df.empty:
    # A day where nobody was absent or tardy is a valid outcome, not a failure.
    # Write the header row anyway so Clever sees an explicit "no negative
    # attendance today" rather than a stale file from the previous run.
    print("NOTE: no negative attendance today. Writing a header-only file.")

for col in output_columns:
    out_df[col] = out_df[col].astype(str).str.strip()

out_df.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8",
    lineterminator="\n",
    quoting=csv.QUOTE_MINIMAL,
)

type_counts = out_df["Attendance_type"].value_counts().to_dict()
status_counts = out_df["Attendance_status"].value_counts().to_dict()
print(f"Saved fresh attendance file: {OUTPUT_FILE}")
print(f"  Attendance date: {attendance_date}")
print(f"  Unique run ID:   {run_id}")
print(f"  Records:         {len(out_df)}  by type: {type_counts}")
print(f"  Status mix:      {status_counts}")
if not INCLUDE_PRESENT:
    print("  (present records omitted -- Clever ingests negative attendance only)")


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
if DRY_RUN:
    print("DRY_RUN is set -- skipping SFTP upload.")
    sys.exit(0)

transport = None
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

    files_to_sync = [(OUTPUT_FILE, REMOTE_FILE)]
    for filename in ADDITIONAL_FILES:
        files_to_sync.append((os.path.join(LOCAL_DATA_DIR, filename), filename))

    failures = []
    for local_path, remote_filename in files_to_sync:
        remote_path = f"{remote_dir}/{remote_filename}"
        if not os.path.exists(local_path):
            print(f"WARNING: local file '{local_path}' not found, skipping "
                  f"upload of {remote_filename}.")
            failures.append(remote_filename)
            continue
        try:
            print(f"Uploading {local_path} -> {remote_path}")
            sftp.put(local_path, remote_path)
        except Exception as e:
            print(f"ERROR: failed to upload {remote_filename}: {e}")
            failures.append(remote_filename)

    sftp.close()

    if failures:
        print(f"CRITICAL ERROR: The following files failed to sync: {failures}")
        sys.exit(1)

    print("Upload complete. All files synced.")
except Exception as e:
    print(f"CRITICAL ERROR: SFTP upload failed: {e}")
    sys.exit(1)
finally:
    if transport is not None:
        transport.close()
