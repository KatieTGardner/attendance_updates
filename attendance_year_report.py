#!/usr/bin/env python3
"""
Simulate a school year of attendance and report the ADA story.

Uses the exact same model as generate_attendance.py -- same archetypes, same
deterministic schedule, same section-to-daily rollup -- so what this prints is
what the generated files will actually contain.

Reports, per student and per school:
    rate = (present + tardy) / (present + tardy + absent)   on DAILY records

against the district's funding bands:
    >= 95%      funding target zone
    90% - 94%   at-risk warning zone
    <  90%      chronic absenteeism / crisis zone

Usage:
    LOCAL_DATA_DIR=/path/to/data python3 attendance_year_report.py
    REPORT_DAYS=180 python3 attendance_year_report.py
    REPORT_CSV=ada_report.csv python3 attendance_year_report.py
"""

import os
import csv
import random
import datetime
from collections import defaultdict

import generate_attendance as gen

REPORT_DAYS = int(os.environ.get("REPORT_DAYS", "180"))
REPORT_END = os.environ.get("REPORT_END")
REPORT_CSV = os.environ.get("REPORT_CSV", "")

FUNDING_TARGET = 0.95
AT_RISK_FLOOR = 0.90


def band(rate):
    if rate >= FUNDING_TARGET:
        return "funding target"
    if rate >= AT_RISK_FLOOR:
        return "at-risk"
    return "chronic"


BAND_LABEL = {
    "funding target": ">=95%  funding target zone",
    "at-risk":        "90-94% at-risk warning zone",
    "chronic":        "<90%   chronic / crisis zone",
}


def main():
    end_day = (datetime.date.fromisoformat(REPORT_END) if REPORT_END
               else datetime.date.today())
    days = gen.school_days_in_range(end_day, REPORT_DAYS)
    if not days:
        print("No school days in range. Check SCHOOL_YEAR_START and the "
              "closure calendar.")
        return

    student_school, section_school, enrollments = gen.load_roster()
    archetypes = gen.assign_archetypes(student_school)

    # Simulate. Track daily status and verify the rollup rule on every day.
    daily_counts = defaultdict(lambda: defaultdict(int))
    rollup_checked = rollup_mismatch = material_mismatch = 0
    section_counts = defaultdict(lambda: defaultdict(int))

    for day in days:
        day_index = gen.school_day_index(day)
        for student_id in student_school:
            archetype = archetypes[student_id]
            intent = gen.intended_daily_status(student_id, archetype, day_index)
            sections = enrollments.get(student_id, [])
            rng = random.Random(
                gen.stable_hash(gen.SEED or "", student_id, day.isoformat()))
            statuses = gen.build_section_statuses(
                intent, len(sections), rng,
                allow_variance=(archetype != "perfect"))
            derived = gen.rollup_daily(statuses) or intent

            if statuses:
                rollup_checked += 1
                if derived != intent:
                    rollup_mismatch += 1
                # Material = did the rollup flip absent-ness? That is what
                # moves ADA; a present day rolling up to tardy does not.
                if (derived == "absent") != (intent == "absent"):
                    material_mismatch += 1
            daily_counts[student_id][derived] += 1
            for status in statuses:
                section_counts[student_id][status] += 1

    # === per-student table ===
    print()
    print(f"School-year simulation: {len(days)} school days "
          f"({days[0]} -> {days[-1]})")
    print("Rate = (present + tardy) / (present + tardy + absent) on daily records")
    print()
    header = (f"{'School':<9} {'Student':<12} {'Archetype':<9} "
              f"{'Pres':>5} {'Tard':>5} {'Abs':>4} {'Rate':>7}  Band")
    print(header)
    print("-" * len(header))

    rows = []
    by_school = defaultdict(list)
    for student_id, school_id in sorted(student_school.items(),
                                        key=lambda kv: (kv[1], kv[0])):
        counts = daily_counts[student_id]
        present, tardy, absent = (counts["present"], counts["tardy"],
                                  counts["absent"])
        total = present + tardy + absent
        rate = (present + tardy) / total if total else 0.0
        label = band(rate)
        by_school[school_id].append(rate)
        rows.append({
            "School_id": school_id, "Student_id": student_id,
            "Archetype": archetypes[student_id],
            "Present_days": present, "Tardy_days": tardy,
            "Absent_days": absent, "School_days": total,
            "Attendance_rate": round(rate, 4), "Band": label,
        })
        star = " *" if rate >= 1.0 else ""
        print(f"{school_id:<9} {student_id:<12} {archetypes[student_id]:<9} "
              f"{present:>5} {tardy:>5} {absent:>4} {rate:>6.1%}  "
              f"{BAND_LABEL[label]}{star}")

    # === per-school ADA ===
    print()
    print("School-level ADA")
    print("-" * 58)
    for school_id in sorted(by_school):
        rates = by_school[school_id]
        ada = sum(rates) / len(rates)
        perfect = sum(1 for r in rates if r >= 1.0)
        counts = defaultdict(int)
        for r in rates:
            counts[band(r)] += 1
        verdict = "MEETS 95% target" if ada >= FUNDING_TARGET else \
                  ("AT RISK" if ada >= AT_RISK_FLOOR else "CRISIS")
        print(f"{school_id}: ADA {ada:.2%}  [{verdict}]")
        print(f"    {len(rates)} students | {perfect} perfect attender(s) | "
              f"{counts['funding target']} at 95%+, "
              f"{counts['at-risk']} at-risk, {counts['chronic']} chronic")

    all_rates = [r for rates in by_school.values() for r in rates]
    district = sum(all_rates) / len(all_rates)
    print()
    print(f"District ADA: {district:.2%}  "
          f"[{'MEETS 95% target' if district >= FUNDING_TARGET else 'BELOW target'}]")

    # === rollup verification ===
    print()
    if material_mismatch:
        print(f"Rollup check: FAIL -- {material_mismatch:,} of "
              f"{rollup_checked:,} student-days flip absent-ness. ADA is wrong.")
    elif rollup_mismatch:
        print(f"Rollup check: PASS (material) -- no student-day flips "
              f"absent-ness across {rollup_checked:,} student-days, so ADA is "
              f"exact. {rollup_mismatch:,} present days roll up to tardy via "
              f"INCIDENTAL_TARDY_RATE, which does not affect the rate.")
    else:
        print(f"Rollup check: PASS (exact) -- section records roll up to the "
              f"intended daily status on all {rollup_checked:,} student-days "
              f"with sections.")

    if REPORT_CSV:
        with open(REPORT_CSV, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote per-student detail to {REPORT_CSV}")


if __name__ == "__main__":
    main()
