#!/usr/bin/env python3
"""
Generate daily + section attendance and sync it to the Clever SFTP box.

ATTENDANCE MODEL
----------------
Section attendance is generated first and the daily status is rolled up from
it, per the district rule:

    absent in MORE THAN HALF of a student's periods  -> daily = absent
    otherwise, any tardy period                      -> daily = tardy
    otherwise                                        -> daily = present

Each student is assigned a stable "archetype" that determines how often they
miss school. Archetypes are assigned deterministically from the student ID, so
a perfect attender is perfect every single day, across every run, forever.
Quotas are applied per school so each school gets a realistic spread:

    perfect   100.0% attendance   never absent
    strong     98.5%              funding target zone
    typical    96.5%              funding target zone
    at_risk    92.0%              90-94% warning zone
    chronic    87.0%              below 90%, chronically absent

Attendance rate counts tardy as present, matching the district formula:

    rate = (present + tardy) / (present + tardy + absent)

Default quotas give every school of 4+ students exactly one perfect attender,
one at-risk student, and one chronic student, with everyone else healthy --
which lands school ADA just above the 95% funding threshold.

HISTORY
-------
A single day's file can't demonstrate a 95% rate. Use BACKFILL_DAYS to emit
many school days into one attendance.csv (the spec carries Attendance_date per
row, and Clever retains 6 months):

    BACKFILL_DAYS=120 python3 generate_attendance.py

Attendance_ids are deterministic hashes of (student, date, type, section), so
re-running a backfill produces byte-identical ids and re-uploading is
idempotent rather than duplicating records.

Clever CSV spec reference:
https://support.clever.com/hc/s/articles/000001704?language=en_US
"""

import os
import sys
import csv
import math
import random
import hashlib
import datetime
from collections import defaultdict

import pandas as pd

try:
    import paramiko
except ImportError:  # only needed for the upload step
    paramiko = None

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

ADDITIONAL_FILES = [
    "students.csv",
    "staff.csv",
    "enrollments.csv",
    "teachers.csv",
    "sections.csv",
    "schools.csv",
]

OUTPUT_COLUMNS = [
    "Student_id",
    "School_id",
    "Section_id",
    "Attendance_date",
    "Attendance_type",
    "Attendance_status",
    "Excuse_code",
    "Attendance_id",
]

EXCUSE_CODE_MAP = {
    "present": "",
    "absent": "excusecodeAbsent",
    "tardy": "excusecodeTardy",
}


def _flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


DRY_RUN = _flag("DRY_RUN")
INCLUDE_PRESENT = _flag("INCLUDE_PRESENT")
EMIT_DAILY = _flag("EMIT_DAILY", "true")
EMIT_SECTION = _flag("EMIT_SECTION", "true")
FORCE_RUN = _flag("FORCE_RUN")
STABLE_ATTENDANCE_IDS = _flag("STABLE_ATTENDANCE_IDS", "true")
CLEAR_ON_NON_SCHOOL_DAYS = _flag("CLEAR_ON_NON_SCHOOL_DAYS")

if os.environ.get("SKIP_WEEKENDS") is not None:
    SCHOOL_DAYS_ONLY = _flag("SKIP_WEEKENDS")
    print("NOTE: SKIP_WEEKENDS is deprecated; use SCHOOL_DAYS_ONLY instead.")
else:
    SCHOOL_DAYS_ONLY = _flag("SCHOOL_DAYS_ONLY", "true")

SECTION_COVERAGE = float(os.environ.get("SECTION_COVERAGE", "1.0"))
# Odds a student present for the day still misses one individual period.
# Never enough to flip the daily rollup to absent.
CUT_CLASS_RATE = float(os.environ.get("CUT_CLASS_RATE", "0.10"))
# Odds a present day picks up an unrelated late period. Default 0 so daily
# tardies track the archetype exactly; raise it for extra noise.
INCIDENTAL_TARDY_RATE = float(os.environ.get("INCIDENTAL_TARDY_RATE", "0.0"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0"))
SCHOOL_YEAR_START = datetime.date.fromisoformat(
    os.environ.get("SCHOOL_YEAR_START", "2026-08-01")
)
SEED = os.environ.get("ATTENDANCE_SEED")

TZ_OFFSET = float(os.environ.get("ATTENDANCE_TZ_OFFSET", "0"))
_now_utc = datetime.datetime.now(datetime.timezone.utc)
RUN_ID = _now_utc.strftime("%Y%m%d%H%M%S")


# =====================================================================
# ARCHETYPES
# =====================================================================
# absence = share of school days fully missed (drives the attendance rate)
# tardy   = share of remaining days the student arrives late
ARCHETYPES = {
    "perfect": {"absence": 0.000, "tardy": 0.000},
    "strong":  {"absence": 0.015, "tardy": 0.010},
    "typical": {"absence": 0.035, "tardy": 0.020},
    "at_risk": {"absence": 0.080, "tardy": 0.040},
    "chronic": {"absence": 0.130, "tardy": 0.060},
}

# Per school: one of each of these, then everyone else drawn from FILL_CYCLE.
# Applied only to schools with at least MIN_SIZE_FOR_TAIL students -- a
# two-student school with a chronic absentee would sink below 90% ADA.
QUOTA_SPEC = os.environ.get("ARCHETYPE_QUOTAS", "perfect:1,at_risk:1,chronic:1")
FILL_CYCLE = [a.strip() for a in
              os.environ.get("FILL_ARCHETYPES", "strong,typical").split(",")
              if a.strip()]
MIN_SIZE_FOR_TAIL = int(os.environ.get("MIN_SIZE_FOR_TAIL", "4"))


def parse_quotas(spec):
    quotas = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, count = chunk.partition(":")
        name = name.strip()
        if name not in ARCHETYPES:
            print(f"WARNING: unknown archetype '{name}' in ARCHETYPE_QUOTAS, ignoring.")
            continue
        try:
            quotas.append((name, int(count or 1)))
        except ValueError:
            print(f"WARNING: bad count in ARCHETYPE_QUOTAS entry '{chunk}', ignoring.")
    return quotas


QUOTAS = parse_quotas(QUOTA_SPEC)


def stable_hash(*parts):
    """Deterministic integer hash, stable across runs and Python versions."""
    joined = "|".join(str(p) for p in parts)
    return int(hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12], 16)


def assign_archetypes(student_school):
    """Map student_id -> archetype, with quotas applied within each school."""
    by_school = defaultdict(list)
    for student_id, school_id in student_school.items():
        by_school[school_id].append(student_id)

    assignments = {}
    for school_id, students in by_school.items():
        # Stable ordering: same roster always produces the same assignment.
        ordered = sorted(students, key=lambda s: stable_hash("archetype", s))
        if len(ordered) < MIN_SIZE_FOR_TAIL:
            # Too small for a meaningful tail; keep the school healthy.
            for i, student_id in enumerate(ordered):
                assignments[student_id] = "perfect" if i == 0 else "strong"
            continue

        index = 0
        for name, count in QUOTAS:
            for _ in range(count):
                if index >= len(ordered):
                    break
                assignments[ordered[index]] = name
                index += 1
        fill = FILL_CYCLE or ["typical"]
        for offset, student_id in enumerate(ordered[index:]):
            assignments[student_id] = fill[offset % len(fill)]
    return assignments


# =====================================================================
# SCHOOL CALENDAR
# =====================================================================
def _parse_date_entry(entry, source):
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
            return {start + datetime.timedelta(days=i)
                    for i in range((end - start).days + 1)}
        return {datetime.date.fromisoformat(entry)}
    except ValueError:
        print(f"WARNING: ignoring unparseable date '{entry}' in {source}. "
              f"Expected YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD.")
        return set()


def load_no_school_dates():
    dates = set()
    for entry in os.environ.get("NO_SCHOOL_DATES", "").replace(";", ",").split(","):
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


def school_day_index(day):
    """How many school days have elapsed since SCHOOL_YEAR_START."""
    if day < SCHOOL_YEAR_START:
        return 0
    index, cursor = 0, SCHOOL_YEAR_START
    while cursor < day:
        if school_day_status(cursor)[0]:
            index += 1
        cursor += datetime.timedelta(days=1)
    return index


def school_days_in_range(end_day, count):
    """The `count` most recent school days ending at (and including) end_day."""
    days, cursor = [], end_day
    guard = 0
    while len(days) < count and guard < count * 10 + 400:
        if school_day_status(cursor)[0]:
            days.append(cursor)
        cursor -= datetime.timedelta(days=1)
        guard += 1
    return sorted(days)


# =====================================================================
# ATTENDANCE SCHEDULE
# =====================================================================
def _phase(salt, student_id):
    """Per-student offset so students don't all miss the same days."""
    return (stable_hash(salt, student_id) % 10_000) / 10_000.0


def _scheduled(student_id, day_index, rate, salt):
    """
    Deterministic evenly-spread schedule: returns True on exactly ~rate of
    school days. Uses a Bresenham-style accumulator with a per-student phase
    offset.

    Stateless -- depends only on the student, the day index, and the rate --
    so any day can be regenerated or backfilled and gets the same answer.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    phase = _phase(salt, student_id)
    return math.floor((day_index + 1 + phase) * rate) > \
        math.floor((day_index + phase) * rate)


def attended_day_index(student_id, day_index, absence_rate):
    """
    Position of this day among the days the student actually attends.

    The tardy schedule is indexed on attended days rather than calendar days.
    Indexing both on calendar days lets an absence schedule mask the tardy
    schedule whenever the two rates are harmonically related -- e.g. the
    at_risk archetype's 0.08 absence vs 0.04 tardy put every scheduled tardy
    on a day the student was already absent, producing zero tardies.
    """
    if absence_rate <= 0:
        return day_index
    phase = _phase("absence", student_id)
    absences_so_far = math.floor((day_index + phase) * absence_rate)
    return day_index - absences_so_far


def intended_daily_status(student_id, archetype, day_index):
    """The daily status this student is *supposed* to have, per archetype."""
    rates = ARCHETYPES[archetype]
    if _scheduled(student_id, day_index, rates["absence"], "absence"):
        return "absent"
    attended = attended_day_index(student_id, day_index, rates["absence"])
    if _scheduled(student_id, attended, rates["tardy"], "tardy"):
        return "tardy"
    return "present"


def rollup_daily(section_statuses):
    """
    District rule: absent in more than half of periods -> daily absent.
    Otherwise a tardy period makes the day tardy, else present.
    """
    total = len(section_statuses)
    if not total:
        return None
    absences = sum(1 for s in section_statuses if s == "absent")
    if absences * 2 > total:
        return "absent"
    if any(s == "tardy" for s in section_statuses):
        return "tardy"
    return "present"


def build_section_statuses(intent, section_count, rng, allow_variance=True):
    """
    Produce per-section statuses that roll up to `intent` under rollup_daily.

    Some variance is allowed -- a "present" day can still include a cut class,
    and an "absent" day can include a period or two marked present (arrived
    late / left early) -- but never enough to flip the rollup.

    allow_variance=False produces clean days with no incidental cuts or
    tardies. Used for the "perfect" archetype so a perfect attender really is
    perfect in every period, not just in the daily rollup.
    """
    if section_count == 0:
        return []

    if intent == "absent":
        # Needs strictly more than half absent. Usually the whole day.
        minimum = section_count // 2 + 1
        if allow_variance and rng.random() >= 0.75:
            absent_count = rng.randint(minimum, section_count)
        else:
            absent_count = section_count
        statuses = ["absent"] * absent_count + \
                   ["present"] * (section_count - absent_count)
        rng.shuffle(statuses)
        return statuses

    # Present or tardy: absences must stay at or below half, so they can
    # never flip the rollup to absent.
    max_absent = section_count // 2
    absent_count = 0
    if allow_variance and max_absent >= 1 and rng.random() < CUT_CLASS_RATE:
        absent_count = 1 if (max_absent == 1 or rng.random() < 0.8) else 2
    statuses = ["present"] * section_count
    for position in rng.sample(range(section_count), absent_count):
        statuses[position] = "absent"

    if intent == "tardy":
        # Late arrival lands on the first period the student actually attended.
        for position, status in enumerate(statuses):
            if status == "present":
                statuses[position] = "tardy"
                break
        else:
            statuses[0] = "tardy"
    elif allow_variance and INCIDENTAL_TARDY_RATE > 0:
        # A present day may still include an unrelated late period. Off by
        # default: it rolls the day up to "tardy", which pushes the daily
        # tardy count above the archetype's own tardy rate. Harmless for ADA
        # (tardy counts as present) but it blurs the per-archetype story.
        candidates = [i for i, s in enumerate(statuses) if s == "present"]
        if candidates and rng.random() < INCIDENTAL_TARDY_RATE:
            statuses[rng.choice(candidates)] = "tardy"

    return statuses


def make_attendance_id(student_id, date_str, att_type, section_id, counter):
    if STABLE_ATTENDANCE_IDS:
        digest = hashlib.sha1(
            f"{student_id}|{date_str}|{att_type}|{section_id}".encode("utf-8")
        ).hexdigest()[:16]
        return f"sisid{digest}"
    return f"sisid{RUN_ID}{str(counter).zfill(6)}"


# =====================================================================
# DATA LOADING
# =====================================================================
def read_csv_flexible(path, aliases=None):
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


def load_roster():
    """Return student_school, section_school, enrollments."""
    print(f"Loading master file: {MASTER_FILE}")
    try:
        df = read_csv_flexible(MASTER_FILE, aliases={
            "Student_id": ["student_id", "sis_id", "Student_ID"],
            "School_id": ["school_id", "School_ID"],
            "Section_id": ["section_id", "Section_ID"],
        })
    except Exception as e:
        print(f"CRITICAL ERROR: Could not read master file: {e}")
        sys.exit(1)
    print(f"Loaded {len(df)} rows")

    missing = [c for c in ("Student_id", "School_id") if c not in df.columns]
    if missing:
        print(f"CRITICAL ERROR: Missing required source columns: {missing}")
        print(f"Found columns: {list(df.columns)}")
        sys.exit(1)

    roster = (df[["Student_id", "School_id"]]
              .loc[df["Student_id"] != ""]
              .drop_duplicates()
              .reset_index(drop=True))
    if len(roster) != len(df):
        print(f"NOTE: collapsed {len(df)} master rows -> {len(roster)} unique "
              f"student/school pairs.")
    student_school = dict(zip(roster["Student_id"], roster["School_id"]))

    section_school, section_period = {}, {}
    if EMIT_SECTION and os.path.exists(SECTIONS_FILE):
        sections_df = read_csv_flexible(SECTIONS_FILE, aliases={
            "Section_id": ["section_id"], "School_id": ["school_id"],
            "Period": ["period"]})
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
        print(f"NOTE: {SECTIONS_FILE} not found; section School_id falls back "
              f"to the student's school and period order will be arbitrary.")

    enrollments = defaultdict(list)
    if EMIT_SECTION:
        if not os.path.exists(ENROLLMENTS_FILE):
            print(f"CRITICAL ERROR: {ENROLLMENTS_FILE} not found. Section "
                  f"attendance needs it to map students to sections. Set "
                  f"LOCAL_DATA_DIR, or EMIT_SECTION=false for daily only.")
            sys.exit(1)
        enroll_df = read_csv_flexible(ENROLLMENTS_FILE, aliases={
            "Student_id": ["student_id", "sis_id"],
            "Section_id": ["section_id"], "School_id": ["school_id"]})
        enroll_missing = [c for c in ("Student_id", "Section_id")
                          if c not in enroll_df.columns]
        if enroll_missing:
            print(f"CRITICAL ERROR: enrollments.csv is missing {enroll_missing}. "
                  f"Found columns: {list(enroll_df.columns)}")
            sys.exit(1)
        seen = set()
        for _, row in enroll_df.iterrows():
            stu, sec = row["Student_id"], row["Section_id"]
            if not stu or not sec or (stu, sec) in seen:
                continue
            seen.add((stu, sec))
            enrollments[stu].append(sec)
        for stu in enrollments:
            enrollments[stu].sort(key=lambda s: (section_period.get(s, 0), s))
        covered = sum(1 for s in student_school if enrollments.get(s))
        print(f"Loaded {len(seen)} enrollments covering {covered}/"
              f"{len(student_school)} students")
        uncovered = [s for s in student_school if not enrollments.get(s)]
        if uncovered:
            print(f"NOTE: {len(uncovered)} student(s) have no enrollments and "
                  f"will get daily records only: {uncovered[:5]}"
                  f"{'...' if len(uncovered) > 5 else ''}")

    return student_school, section_school, enrollments


# =====================================================================
# GENERATION
# =====================================================================
def build_day(day, student_school, section_school, enrollments, archetypes,
              counter_start=0):
    """Generate one school day's records. Returns (rows, next_counter)."""
    date_str = day.strftime("%Y-%m-%dT00:00:00Z")
    day_index = school_day_index(day)
    rows = []
    counter = counter_start

    for student_id, school_id in student_school.items():
        archetype = archetypes[student_id]
        intent = intended_daily_status(student_id, archetype, day_index)

        sections = enrollments.get(student_id, []) if EMIT_SECTION else []
        if SECTION_COVERAGE < 1.0 and sections:
            keep = max(1, round(len(sections) * SECTION_COVERAGE))
            sections = sections[:keep]

        # Per-student-day RNG so variance is reproducible and backfillable.
        rng = random.Random(stable_hash(SEED or "", student_id, day.isoformat()))
        section_statuses = build_section_statuses(
            intent, len(sections), rng,
            allow_variance=(archetype != "perfect"))

        # Daily status is DERIVED from the sections, per the district rule.
        daily_status = rollup_daily(section_statuses) or intent

        if EMIT_DAILY:
            counter += 1
            if daily_status != "present" or INCLUDE_PRESENT:
                rows.append({
                    "Student_id": student_id,
                    "School_id": school_id,
                    "Section_id": "",
                    "Attendance_date": date_str,
                    "Attendance_type": "daily",
                    "Attendance_status": daily_status,
                    "Excuse_code": EXCUSE_CODE_MAP[daily_status],
                    "Attendance_id": make_attendance_id(
                        student_id, date_str, "daily", "", counter),
                })

        for section_id, status in zip(sections, section_statuses):
            counter += 1
            if status == "present" and not INCLUDE_PRESENT:
                continue
            rows.append({
                "Student_id": student_id,
                "School_id": section_school.get(section_id) or school_id,
                "Section_id": section_id,
                "Attendance_date": date_str,
                "Attendance_type": "section",
                "Attendance_status": status,
                "Excuse_code": EXCUSE_CODE_MAP[status],
                "Attendance_id": make_attendance_id(
                    student_id, date_str, "section", section_id, counter),
            })

    return rows, counter


def build_attendance(days, student_school, section_school, enrollments,
                     archetypes):
    rows, counter = [], 0
    for day in days:
        day_rows, counter = build_day(day, student_school, section_school,
                                      enrollments, archetypes, counter)
        rows.extend(day_rows)
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def write_output(out_df, path=OUTPUT_FILE):
    for col in OUTPUT_COLUMNS:
        out_df[col] = out_df[col].astype(str).str.strip()
    out_df.to_csv(path, index=False, encoding="utf-8",
                  lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


# =====================================================================
# UPLOAD
# =====================================================================
def ensure_remote_dir(sftp, remote_dir):
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
    if paramiko is None:
        print("CRITICAL ERROR: paramiko is not installed; cannot upload.")
        sys.exit(1)
    transport = None
    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        print(f"Logged in as '{SFTP_USER}', default directory is: "
              f"{sftp.normalize('.')}")

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


# =====================================================================
# MAIN
# =====================================================================
def resolve_target_day():
    local_now = _now_utc + datetime.timedelta(hours=TZ_OFFSET)
    if os.environ.get("ATTENDANCE_DATE"):
        return datetime.datetime.strptime(
            os.environ["ATTENDANCE_DATE"], "%Y-%m-%d").date()
    return local_now.date()


def main():
    target_day = resolve_target_day()
    is_school_day, reason = school_day_status(target_day)
    if not is_school_day and FORCE_RUN:
        print(f"FORCE_RUN is set -- generating attendance even though {reason}.")
        is_school_day = True

    if not DRY_RUN and not all([SFTP_HOST, SFTP_USER, SFTP_PASS]):
        print("CRITICAL ERROR: One or more secure SFTP secrets are missing.")
        sys.exit(1)

    print(f"Target date: {target_day.isoformat()} ({reason})")

    files_to_sync = [(os.path.join(LOCAL_DATA_DIR, name), name)
                     for name in ADDITIONAL_FILES]

    if BACKFILL_DAYS > 0:
        days = school_days_in_range(target_day, BACKFILL_DAYS)
        if not is_school_day and days and days[-1] == target_day:
            days = days[:-1]
        print(f"Backfill mode: {len(days)} school days "
              f"({days[0].isoformat()} -> {days[-1].isoformat()})"
              if days else "Backfill mode: no school days in range")
    elif is_school_day:
        days = [target_day]
    else:
        days = []

    if days:
        student_school, section_school, enrollments = load_roster()
        archetypes = assign_archetypes(student_school)

        mix = defaultdict(int)
        for archetype in archetypes.values():
            mix[archetype] += 1
        print(f"Archetype mix: {dict(sorted(mix.items()))}")
        projected = {}
        for school_id in sorted(set(student_school.values())):
            members = [s for s, sc in student_school.items() if sc == school_id]
            ada = sum(1 - ARCHETYPES[archetypes[s]]["absence"]
                      for s in members) / len(members)
            projected[school_id] = ada
            print(f"  {school_id}: projected ADA {ada:.1%} over {len(members)} students")

        out_df = build_attendance(days, student_school, section_school,
                                  enrollments, archetypes)
        if out_df.empty:
            print("NOTE: no negative attendance in range. Writing header only.")
        write_output(out_df)

        type_counts = out_df["Attendance_type"].value_counts().to_dict()
        status_counts = out_df["Attendance_status"].value_counts().to_dict()
        print(f"Saved attendance file: {OUTPUT_FILE}")
        print(f"  Days:       {len(days)}")
        print(f"  Records:    {len(out_df)}  by type: {type_counts}")
        print(f"  Status mix: {status_counts}")
        if not INCLUDE_PRESENT:
            print("  (present records omitted -- Clever ingests negative "
                  "attendance only; set INCLUDE_PRESENT=true if your rate "
                  "calculation needs present rows in the file)")
        files_to_sync.insert(0, (OUTPUT_FILE, REMOTE_FILE))
    else:
        print(f"No attendance generated: {reason}.")
        if CLEAR_ON_NON_SCHOOL_DAYS:
            write_output(pd.DataFrame([], columns=OUTPUT_COLUMNS))
            print(f"CLEAR_ON_NON_SCHOOL_DAYS is set -- uploading an empty "
                  f"{REMOTE_FILE}.")
            files_to_sync.insert(0, (OUTPUT_FILE, REMOTE_FILE))
        else:
            print(f"Leaving the existing remote {REMOTE_FILE} untouched.")
        print(f"Still syncing {len(ADDITIONAL_FILES)} reference files.")

    if DRY_RUN:
        print("DRY_RUN is set -- skipping SFTP upload. Would have synced: "
              f"{[remote for _, remote in files_to_sync]}")
        return
    upload(files_to_sync)


if __name__ == "__main__":
    main()
