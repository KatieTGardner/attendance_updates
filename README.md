# attendance_updates

Generates a fresh set of synthetic daily attendance records and pushes them, along with a static set of SIS reference files, to a Clever SFTP sandbox directory. Runs automatically every day via GitHub Actions.

Use this when you need a sandbox account to keep receiving plausible-looking attendance data day over day — for demos, support reproductions, or testing attendance-dependent behavior downstream.

## How it works

Each run of [`daily_sftp_sync.py`](daily_sftp_sync.py):

1. Reads `attendance.csv` (the master roster of `Student_id` / `School_id` / `Section_id` combinations) as strings.
2. Normalizes headers and rewrites every attendance field — date, status, excuse code, and a unique `Attendance_id` — so the output is dated today and freshly randomized.
3. Writes the result to `attendance_processed.csv` locally.
4. Uploads it to the SFTP as `attendance.csv`, then uploads the six reference files unchanged.
5. Exits non-zero if any file fails to upload, so a partial sync shows up as a red Actions run rather than a silent gap.

The master `attendance.csv` is only a template for *which* rows to generate. Its status and date values are never sent as-is.

### Files in this repo

| File | Role |
| --- | --- |
| `daily_sftp_sync.py` | The whole job: generate, write, upload. |
| `.github/workflows/sftp_sync.yml` | Scheduled + manual trigger. |
| `attendance.csv` | Master template. Defines the student/school/section rows to generate attendance for. |
| `students.csv`, `staff.csv`, `teachers.csv`, `enrollments.csv`, `sections.csv`, `schools.csv` | Static reference data, uploaded verbatim each run. These follow [Clever's SFTP CSV format](https://support.clever.com/s/articles/203114867?language=en_US) — edit them if you need different rosters, but keep the column names intact. |

The sandbox currently covers three schools: `02M800` (City High School), `13K123` (Pineapple Elementary), and `27Q321` (Rockaway Beach Middle).

### Generated attendance.csv columns

| Column | Source | Notes |
| --- | --- | --- |
| `Student_id` | Master file | Required. Job aborts if missing. |
| `School_id` | Master file | Required. Job aborts if missing. |
| `Section_id` | Master file | Optional. Blank for `daily` rows, populated for `section` rows. Added as an empty column if absent. |
| `Attendance_date` | Generated | Current UTC date as `YYYY-MM-DDT00:00:00Z`. |
| `Attendance_type` | Generated | Hardcoded to `daily` for every row — see [Known issues](#known-issues--gotchas). |
| `Attendance_status` | Generated | Random pick from a weighted pool: 5× `present`, 1× `absent`, 1× `tardy` (≈71% present). |
| `Excuse_code` | Generated | Derived from status: `""` / `excusecodeAbsent` / `excusecodeTardy`. |
| `Attendance_id` | Generated | `sisid<YYYYMMDDHHMMSS><5-digit row number>`, e.g. `sisid2026081808000100042`. Unique per run. |

Header aliasing: if the master file's status column is named `Attendance`, `Attendance_`, `attendance`, or `attendance_status`, it's renamed to `Attendance_status` before processing. (Cosmetic — the column gets overwritten anyway.)

## Setup

### Required repository secrets

Set all four under **Settings → Secrets and variables → Actions**. The job fails fast if any are missing.

| Secret | Value |
| --- | --- |
| `SFTP_HOST` | Clever SFTP hostname. |
| `SFTP_USER` | Sandbox SFTP username. |
| `SFTP_PASS` | Sandbox SFTP password. |
| `SFTP_REMOTE_DIR` | District subdirectory, e.g. `home/decorous-school-4198`. **Must be set** — see below. |

Optional env var: `LOCAL_DATA_DIR` (defaults to `.`) if the reference CSVs ever move out of the repo root.

### Schedule

`cron: "0 8 * * *"` — 08:00 UTC daily. Also runnable on demand via **Actions → Upload Daily Attendance → Run workflow**.

Note that GitHub's scheduled runs are best-effort and can be delayed during peak load, and GitHub disables schedules on repos with no activity for 60 days. If the data goes stale, check whether the schedule was disabled before debugging the script.

### Running locally

```bash
pip install pandas paramiko

export SFTP_HOST="..."
export SFTP_USER="..."
export SFTP_PASS="..."
export SFTP_REMOTE_DIR="home/your-district-slug"

python daily_sftp_sync.py
```

This uploads to the real sandbox. To do a dry run of just the generation step, comment out the upload block at the bottom and inspect `attendance_processed.csv`.

## Known issues & gotchas

**`SFTP_REMOTE_DIR` must be set on every job that runs this script.** It defaults to `.`, and an unset or blank value used to mean files landed in the SFTP root instead of the district subdirectory — that's what produced the stray root-level `attendance.csv`. The script now aborts if the value is empty or `.`, so the failure is loud rather than silent. If you add another workflow or job that calls this script, remember to pass the secret through; the guard will catch you, but only after the run fails.

**The attendance data is randomly generated, not sequential or realistic.** Every run reassigns each student's status independently, so a student can be absent five days running or have contradictory daily and section records. Don't use this data to validate attendance-rate calculations or chronic-absence logic — it isn't internally consistent.

**`Attendance_type` is hardcoded to `daily`.** The master `attendance.csv` contains both `daily` rows (blank `Section_id`) and `section` rows (populated `Section_id`), but the script overwrites the type on every row. Section-level rows are uploaded as `daily` records with a `Section_id` attached. If you need genuine section-level attendance, derive the type from whether `Section_id` is populated.

**Excuse codes are attached to `present` rows in the master file but not in the output.** The committed `attendance.csv` has values like `excusecodeGms` on present rows; the generated file correctly leaves `Excuse_code` blank when status is `present`. Expect the uploaded file to differ from the committed one here.

**Auth is username/password over `paramiko.Transport`, with no host key verification.** Fine for a sandbox with throwaway credentials. Don't point this at anything real without adding key-based auth and host key checking.

**Uploads are not atomic.** `sftp.put` writes directly to `attendance.csv` at the destination. A consumer polling the directory can in principle read a partially written file. Upload to a temp name and rename if that ever becomes a problem.

**Missing local files are treated as failures, not skips.** If one of the reference CSVs is deleted, the run logs a warning, continues with the rest, and then exits 1 at the end. The other files still upload — so a red run doesn't necessarily mean nothing synced. Read the log.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `CRITICAL ERROR: One or more secure SFTP secrets are missing` | One of `SFTP_HOST` / `SFTP_USER` / `SFTP_PASS` isn't set on the job. |
| `CRITICAL ERROR: SFTP_REMOTE_DIR is not set to the district subdirectory` | Secret missing or passed as blank in the workflow's `env` block. |
| `CRITICAL ERROR: Missing required source columns` | `attendance.csv` was edited and lost `Student_id` or `School_id`. The log prints the columns it did find. |
| `WARNING: local file '...' not found` | A reference CSV was renamed or deleted. Run exits 1 after finishing the others. |
| Data stopped updating, no failed runs | GitHub likely disabled the schedule after 60 days of repo inactivity. Re-enable in the Actions tab. |
