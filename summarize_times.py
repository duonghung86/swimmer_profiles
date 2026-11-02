import csv
import re
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SUMMARY_PATH = OUTPUT_DIR / "best_times.csv"
REQUIRED_COLUMNS = ("swimmer_id", "swimmer_name", "Event", "Time")
TIME_PATTERN = re.compile(r"^(?:(\d+):)?(\d{1,2})\.(\d{2})$")


def parse_time(value, source, line_number):
    match = TIME_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"{source}:{line_number}: invalid swim time {value!r}")

    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    hundredths = int(match.group(3))
    if match.group(1) and seconds >= 60:
        raise ValueError(f"{source}:{line_number}: invalid swim time {value!r}")

    return Decimal(minutes * 60 + seconds) + Decimal(hundredths) / 100


def collect_best_times():
    if not OUTPUT_DIR.is_dir():
        raise FileNotFoundError(f"Output folder not found: {OUTPUT_DIR}")

    swimmers = {}
    events = []
    seen_events = set()
    input_files = sorted(
        path
        for path in OUTPUT_DIR.glob("*.csv")
        if path.resolve() != SUMMARY_PATH.resolve()
    )
    if not input_files:
        raise FileNotFoundError(f"No swimmer CSV files found in {OUTPUT_DIR}")

    for path in input_files:
        with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            if not reader.fieldnames:
                raise ValueError(f"{path}: CSV is missing a header row")
            missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
            if missing:
                raise ValueError(f"{path}: missing required columns: {', '.join(missing)}")

            for line_number, row in enumerate(reader, start=2):
                swimmer_id = (row.get("swimmer_id") or "").strip()
                name = (row.get("swimmer_name") or "").strip()
                event = (row.get("Event") or "").strip()
                time = (row.get("Time") or "").strip()
                if not all((swimmer_id, name, event, time)):
                    raise ValueError(
                        f"{path}:{line_number}: swimmer_id, swimmer_name, "
                        "Event, and Time must not be empty"
                    )

                swimmer = swimmers.setdefault(
                    swimmer_id,
                    {"name": name, "best": {}},
                )
                if swimmer["name"] != name:
                    raise ValueError(
                        f"{path}:{line_number}: swimmer {swimmer_id} has "
                        f"conflicting names {swimmer['name']!r} and {name!r}"
                    )

                seconds = parse_time(time, path, line_number)
                previous = swimmer["best"].get(event)
                if event not in seen_events:
                    seen_events.add(event)
                    events.append(event)
                if previous is None or seconds < previous[0]:
                    swimmer["best"][event] = (seconds, time)

    return swimmers, events


def write_summary(swimmers, events):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with SUMMARY_PATH.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=["Swimmer name", *events])
        writer.writeheader()
        for swimmer_id in sorted(swimmers, key=lambda key: swimmers[key]["name"].casefold()):
            swimmer = swimmers[swimmer_id]
            record = {"Swimmer name": swimmer["name"]}
            record.update(
                {
                    event: swimmer["best"].get(event, ("", ""))[1]
                    for event in events
                }
            )
            writer.writerow(record)
    return SUMMARY_PATH


def main():
    try:
        swimmers, events = collect_best_times()
        if not swimmers:
            raise ValueError(f"No swimmer records found in {OUTPUT_DIR}")
        summary_path = write_summary(swimmers, events)
    except (OSError, ValueError, csv.Error) as error:
        print(f"Could not summarize swimmer times: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(
        f"Saved best times for {len(swimmers)} swimmers across "
        f"{len(events)} events to {summary_path}"
    )


if __name__ == "__main__":
    main()
