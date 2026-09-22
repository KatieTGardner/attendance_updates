#!/usr/bin/env python3
"""
Generate fresh daily + section attendance and sync it to the Clever SFTP box.

Attendance is generated on school days only -- weekdays, minus any dates you
list as closures. On a non-school day the script skips attendance generation
but still syncs the roster/reference files, so Clever stays current 7 days a
week while attendance reflects a real instructional calendar.

CONFIGURING NON-SCHOOL DAYS
---------------------------
Weekends are skipped automatically. For holidays and breaks, either:

  * set NO_SCHOOL_DATES to a comma-separated list, e.g.
        NO_SCHOOL_DATES="2026-11-26,2026-11-27,2026-12-21..2027-01-02"
  * or drop a `no_school_dates.txt` file in LOCAL_DATA_DIR, one entry per
    line, `#` for comments, and `START..END` for an inclusive range:
        # Thanksgiving break
        2026-11-26
        2026-11-27
        # Winter break
        2026-12-21..2027-01-02

Both sources are merged. Set SCHOOL_DAYS_ONLY=false to generate every day
regardless, or FORCE_RUN=true for a one-off run on a closed day.

Clever CSV spec reference:
https://support.clever.com/hc/s/articles/000001704?language=en_US
"""

import os
import sys
import csv
import random
import datetime
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
NO_SCHOOL_FILE = os.path.join(LOCAL_DATA_DIR, "no_school_dates.txt")

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

# === Additional static files to keep in sync alongside attendance ===
ADDITIONAL_FILES = [
    "students.csv",
    "staff.csv",
    "enrollments.csv",
    "teachers.csv",
    "sections.csv",
    "schools.csv",
]


# === BEHAVIOR FLAGS ===
def _flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


DRY_RUN = _flag("DRY_RUN")                       # generate the CSV, skip the upload
INCLUDE_PRESENT = _flag("INCLUDE_PRESENT")       # keep out-of-spec "present" rows
EMIT_DAILY = _flag("EMIT_DAILY", "true")
EMIT_SECTION = _flag("EMIT_SECTION", "true")
FORCE_RUN = _flag("FORCE_RUN")                   # ignore the school-day check once

# Attendance is generated on school days only by default.
# SKIP_WEEKENDS is the old name for this flag and still works.
if os.environ.get("SKIP_WEEKENDS") is not None:
    SCHOOL_DAYS_ONLY = _flag("SKIP_WEEKENDS")
    print("NOTE: SKIP_WEEKENDS is deprecated; use SCHOOL_DAYS_ONLY instead.")
else:
    SCHOOL_DAYS_ONLY = _flag("SCHOOL_DAYS_ONLY", "true")

# On a non-school day, overwrite the remote attendance.csv with a header-only
# file instead of leaving the previous school day's records in place. Off by
# default: re-ingesting an unchanged file is harmless because the
# Attendance_ids are identical, so Clever treats it as the same records.
CLEAR_ON_NON_SCHOOL_DAYS = _flag("CLEAR_ON_NON_SCHOOL_DAYS")

# Share of a student's sections that get their own record on a normal
# (non-absent) day. 1.0 = every enrolled section reports attendance.
SECTION_COVERAGE = float(os.environ.get("SECTION_COVERAGE", "1.0"))

# === RANDOMNESS ===
# Seed from ATTENDANCE_SEED + the date, so a given day is reproducible but
# consecutive days still differ. Unset = non-deterministic.
SEED = os.environ.get("ATTENDANCE_SEED")

# === DATE / RUN INFO ===
# ATTENDANCE_TZ_OFFSET is hours from UTC (e.g. -7 for PDT, -4 for EDT). Without
# it, a run after ~5-8pm Pacific stamps tomorrow's date -- and, now that the
# calendar matters, can push a Friday evening run onto Saturday.
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


# === SCHOOL CALENDAR ===
def _parse_date_entry(entry, source):
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD..YYYY-MM-DD' into a set of dates."""
    entry = entry.split("#", 1)[0].strip()
    if not entry:
        return set()
    try:
        if ".." in entry:
            raw_start, raw_end = (p.strip() for p in entry.split("..", 1))
            start = datetime.date.fromisoformat(raw_start)
            end = datetime.date.fromisoformat(raw_end)
            if end < start:
                start, end = end, start
            return {
                start + datetime.timedelta(days=i)
                for i in range((end - start).days + 1)
            }
        return {datetime.date.fromisoformat(entry)}
    except ValueError:
        print(f"WARNING: ignoring unparseable date '{entry}' in {source}. "
              f"Expected YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD.")
        return set()


def load_no_school_dates():
    """Merge closure dates from NO_SCHOOL_DATES and no_school_dates.txt."""
    dates = set()
    env_value = os.environ.get("NO_SCHOOL_DATES", "")
    for entry in env_value.replace(";", ",").split(","):
        dates |= _parse_date_entry(entry, "NO_SCHOOL_DATES")
    if os.path.exists(NO_SCHOOL_FILE):
        with open(NO_SCHOOL_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                dates |= _parse_date_entry(line, NO_SCHOOL_FILE)
        print(f"Loaded closure calendar from {NO_SCHOOL_FILE}")
    return dates


NO_SCHOOL_DATES = load_no_school_dates()


def school_day_status(day):
    """Return (is_school_day, human-readable reason)."""
    if not SCHOOL_DAYS_ONLY:
        return True, "SCHOOL_DAYS_ONLY is off"
    if day.weekday() >= 5:
        return False, f"{day:%A} is a weekend"
    if day in NO_SCHOOL_DATES:
        return False, f"{day.isoformat()} is listed as a non-school day"
    return True, f"{day:%A} is a school day"


is_school_day, calendar_reason = school_day_status(target_day)
if not is_school_day and FORCE_RUN:
    print(f"FORCE_RUN is set -- generating attendance even though "
          f"{calendar_reason}.")
    is_school_day = True

if not DRY_RUN and not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
    print("CRITICAL ERROR: One or more secure SFTP secrets are missing.")
    sys.exit(1)

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


def build_attendance():
    """Generate the day's attendance records as a DataFrame."""
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

    missing = [c for c in ("Student_id", "School_id") if c not in df.columns]
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
        print(f"NOTE: collapsed {len(df)} master rows -> {len(roster)} unique "
              f"student/school pairs for daily attendance.")
    student_school = dict(zip(roster["Student_id"], roster["School_id"]))

    # --- sections (School_id + period ordering) ---
    section_school, section_period = {}, {}
    if EMIT_SECTION and os.path.exists(SECTIONS_FILE):
        sections_df = read_csv_flexible(
            SECTIONS_FILE,
            aliases={"Section_id": ["section_id"],
                     "School_id": ["school_id"],
                     "Period": ["period"]},
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
        print(f"NOTE: {SECTIONS_FILE} not found; section School_id will fall "
              f"back to the student's school and period order will be arbitrary.")

    # --- enrollments (Student_id -> [Section_id]) ---
    enrollments = defaultdict(list)
    if EMIT_SECTION:
        if not os.path.exists(ENROLLMENTS_FILE):
            print(f"CRITICAL ERROR: {ENROLLMENTS_FILE} not found. Section "
                  f"attendance needs it to map students to sections. Set "
                  f"LOCAL_DATA_DIR, or set EMIT_SECTION=false to generate "
                  f"daily records only.")
            sys.exit(1)
        enroll_df = read_csv_flexible(
            ENROLLMENTS_FILE,
            aliases={"Student_id": ["student_id", "sis_id"],
                     "Section_id": ["section_id"],
                     "School_id": ["school_id"]},
        )
        enroll_missing = [c for c in ("Student_id", "Section_id")
                          if c not in enroll_df.columns]
        if enroll_missing:
            print(f"CRITICAL ERROR: enrollments.csv is missing {enroll_missing}. "
                  f"Found columns: {list(enroll_df.columns)}")
            sys.exit(1)

        seen_pairs = set()
        for _, row in enroll_df.iterrows():
            stu, sec = row["Student_id"], row["Section_id"]
            if not stu or not sec or (stu, sec) in seen_pairs:
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
            print(f"NOTE: {len(uncovered)} student(s) have no enrollments and "
                  f"will get daily records only: {uncovered[:5]}"
                  f"{'...' if len(uncovered) > 5 else ''}")

    # --- attendance model ---
    # Daily outcome for a student, then section outcomes derived from it.
    daily_weights = [("present", 0.86), ("absent", 0.09), ("tardy", 0.05)]
    section_cut_rate = float(os.environ.get("SECTION_CUT_RATE", "0.03"))
    section_tardy_rate = float(os.environ.get("SECTION_TARDY_RATE", "0.05"))
    excuse_code_map = {"present": "",
                       "absent": "excusecodeAbsent",
                       "tardy": "excusecodeTardy"}

    def weighted_choice(weights):
        roll = random.random()
        cumulative = 0.0
        for value, weight in weights:
            cumulative += weight
            if roll < cumulative:
                return value
        return weights[-1][0]

    records = []
    counter = 0

    def add_record(student_id, school_id, section_id, att_type, status):
        """Append one attendance record. Present rows are filtered per spec."""
        nonlocal counter
        if status == "present" and not INCLUDE_PRESENT:
            return
        counter += 1
        records.append({
            "Student_id": student_id,
            "School_id": school_id,
            # Spec: Section_id is populated only for attendance_type="section".
            # Clever infers the type from this field when Attendance_type is
            # blank, so leaving it on a daily row would misclassify the record.
            "Section_id": section_id if att_type == "section" else "",
            "Attendance_date": attendance_date,
            "Attendance_type": att_type,
            "Attendance_status": status,
            "Excuse_code": excuse_code_map.get(status, ""),
            "Attendance_id": f"sisid{run_id}{str(counter).zfill(6)}",
        })

    for student_id, school_id in student_school.items():
        daily_status = weighted_choice(daily_weights)

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
                if roll < section_cut_rate:
                    section_status = "absent"
                elif roll < section_cut_rate + section_tardy_rate:
                    section_status = "tardy"
                else:
                    section_status = "present"

            add_record(student_id, section_school.get(section_id) or school_id,
                       section_id, "section", section_status)

    return pd.DataFrame(records, columns=output_columns)


def write_output(out_df):
    for col in output_columns:
        out_df[col] = out_df[col].astype(str).str.strip()
    out_df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8",
                  lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


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


def upload(files_to_sync):
    """Push (local_path, remote_filename) pairs to the Clever SFTP box."""
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
    except SystemExit:
        raise
    except Exception as e:
        print(f"CRITICAL ERROR: SFTP upload failed: {e}")
        sys.exit(1)
    finally:
        if transport is not None:
            transport.close()


# === MAIN ===
print(f"Target date: {target_day.isoformat()} ({calendar_reason})")

# Reference files sync every day, school day or not, so roster changes made
# over a weekend or break still reach Clever.
files_to_sync = [(os.path.join(LOCAL_DATA_DIR, name), name)
                 for name in ADDITIONAL_FILES]

if is_school_day:
    out_df = build_attendance()
    if out_df.empty:
        # A day where nobody was absent or tardy is a valid outcome, not a
        # failure. Write the header row anyway so Clever sees an explicit
        # "no negative attendance today" rather than a stale file.
        print("NOTE: no negative attendance today. Writing a header-only file.")
    write_output(out_df)

    type_counts = out_df["Attendance_type"].value_counts().to_dict()
    status_counts = out_df["Attendance_status"].value_counts().to_dict()
    print(f"Saved fresh attendance file: {OUTPUT_FILE}")
    print(f"  Attendance date: {attendance_date}")
    print(f"  Unique run ID:   {run_id}")
    print(f"  Records:         {len(out_df)}  by type: {type_counts}")
    print(f"  Status mix:      {status_counts}")
    if not INCLUDE_PRESENT:
        print("  (present records omitted -- Clever ingests negative "
              "attendance only)")

    files_to_sync.insert(0, (OUTPUT_FILE, REMOTE_FILE))
else:
    print(f"No attendance generated: {calendar_reason}.")
    if CLEAR_ON_NON_SCHOOL_DAYS:
        write_output(pd.DataFrame([], columns=output_columns))
        print(f"CLEAR_ON_NON_SCHOOL_DAYS is set -- uploading an empty "
              f"{REMOTE_FILE}.")
        files_to_sync.insert(0, (OUTPUT_FILE, REMOTE_FILE))
    else:
        print(f"Leaving the existing remote {REMOTE_FILE} untouched.")
    print(f"Still syncing {len(ADDITIONAL_FILES)} reference files.")

if DRY_RUN:
    print("DRY_RUN is set -- skipping SFTP upload. Would have synced: "
          f"{[remote for _, remote in files_to_sync]}")
    sys.exit(0)

upload(files_to_sync)
