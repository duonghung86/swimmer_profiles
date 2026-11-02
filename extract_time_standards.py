import re
from pathlib import Path

import pandas as pd
import pdfplumber


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
USA_LEVELS = ("B", "BB", "A", "AA", "AAA", "AAAA")
USA_COLUMNS = tuple(f"{level}" for level in USA_LEVELS)
TIME_PATTERN = re.compile(r"^(?:\d{1,2}:)?\d{1,2}\.\d{2}\*?$")
USA_EVENT_PATTERN = re.compile(
    r"\b(\d{1,4})\s+(MED-R|FR-R|MR-R|FR|BK|BR|FL|IM)"
    r"(?:\s+(SCY|SCM|LCM))?\b",
    re.IGNORECASE,
)
TEXAS_EVENT_PATTERN = re.compile(
    r"\b(\d{1,4})\s+(FR|BK|BR|FL|IM|MR)\b",
    re.IGNORECASE,
)
USA_AGE_PATTERN = re.compile(
    r"\b(\d{1,2}\s*(?:&\s*under|-\s*\d{1,2}))\s+(Girls|Boys)\b",
    re.IGNORECASE,
)
TEXAS_AGE_TOKEN_PATTERN = re.compile(r"^(?:10\s*&\s*U|\d{1,2}-\d{1,2})$", re.I)


def _find_pdf(pattern, description):
    matches = sorted(INPUT_DIR.glob(pattern))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected exactly one {description} PDF matching {pattern!r} "
            f"in {INPUT_DIR}; found {len(matches)}"
        )
    return matches[0]


def _normalize_gender(gender):
    value = str(gender).strip().casefold()
    if value in {"girl", "girls", "female", "f"}:
        return "Girls"
    if value in {"boy", "boys", "male", "m"}:
        return "Boys"
    raise ValueError("gender must be Girls/Boys or Female/Male")


def _age_band(age):
    if isinstance(age, bool):
        raise ValueError("age must be an integer from 1 to 18")
    try:
        value = int(age)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("age must be an integer from 1 to 18") from error
    if value < 1 or value > 18 or str(value) != str(age).strip():
        raise ValueError("age must be an integer from 1 to 18")
    if value <= 10:
        return "10&U"
    if value <= 12:
        return "11-12"
    if value <= 14:
        return "13-14"
    if value <= 16:
        return "15-16"
    return "17-18"


def _page_lines(page):
    words = sorted(page.extract_words(), key=lambda word: (word["top"], word["x0"]))
    lines = []
    for word in words:
        if not lines or word["top"] - lines[-1]["top"] > 2.5:
            lines.append({"top": word["top"], "words": [word]})
        else:
            lines[-1]["words"].append(word)
    for line in lines:
        line["words"].sort(key=lambda word: word["x0"])
        line["text"] = " ".join(word["text"] for word in line["words"])
    return lines, words


def _time_words(words):
    return [
        word
        for word in words
        if TIME_PATTERN.fullmatch(word["text"].strip())
    ]


def _clean_time(value):
    return value.rstrip("*")


def _normalize_usa_age(value):
    normalized = re.sub(r"\s+", "", value).upper()
    return "10&U" if "UNDER" in normalized else normalized


def _usa_level_centers(words):
    labels = [word["text"].upper() for word in words]
    expected = [
        "B",
        "BB",
        "A",
        "AA",
        "AAA",
        "AAAA",
        "AAAA",
        "AAA",
        "AA",
        "A",
        "BB",
        "B",
    ]
    for start in range(len(labels) - len(expected) + 1):
        if labels[start : start + len(expected)] != expected:
            continue
        header_words = words[start : start + len(expected)]
        left = [
            (word["x0"] + word["x1"]) / 2
            for word in header_words[:6]
        ]
        right = [
            (word["x0"] + word["x1"]) / 2
            for word in header_words[6:]
        ]
        return {
            "Girls": dict(zip(USA_LEVELS, left)),
            "Boys": dict(zip(USA_LEVELS, reversed(right))),
        }
    return None


def _values_near_centers(words, centers):
    result = {}
    ordered_centers = sorted(centers.values())
    margin = (ordered_centers[-1] - ordered_centers[0]) / 10
    for word in _time_words(words):
        center = (word["x0"] + word["x1"]) / 2
        if (
            center < ordered_centers[0] - margin
            or center > ordered_centers[-1] + margin
        ):
            continue
        level = min(centers, key=lambda key: abs(centers[key] - center))
        result[level] = _clean_time(word["text"])
    return result


def _usa_course(event_match, line, words, page_width):
    if event_match.group(3):
        return event_match.group(3).upper()

    nearby_courses = [
        word
        for word in words
        if word["text"].upper() in {"SCY", "SCM", "LCM"}
        and abs(word["top"] - line["top"]) <= 12
    ]
    if not nearby_courses:
        return None
    course_word = min(
        nearby_courses,
        key=lambda word: (
            abs(word["top"] - line["top"]),
            abs((word["x0"] + word["x1"]) / 2 - page_width / 2),
        ),
    )
    return course_word["text"].upper()


def _read_usa_standards(pdf_path, gender, age_band):
    records = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines, words = _page_lines(page)
            current_age = None
            level_centers = None
            for line in lines:
                age_match = USA_AGE_PATTERN.search(line["text"])
                if age_match:
                    current_age = _normalize_usa_age(age_match.group(1))

                level_centers = _usa_level_centers(line["words"]) or level_centers
                if current_age != age_band or not level_centers:
                    continue

                event_match = USA_EVENT_PATTERN.search(line["text"])
                if not event_match:
                    continue

                event = f"{event_match.group(1)} {event_match.group(2).upper()}"
                course = _usa_course(event_match, line, words, page.width)
                if not course:
                    continue
                values = _values_near_centers(
                    line["words"], level_centers[gender]
                )
                if values:
                    records[(event, course)] = {
                        f"{level}": values.get(level, pd.NA)
                        for level in USA_LEVELS
                    }
    return records


def _normalize_texas_age(value):
    normalized = re.sub(r"\s+", "", value).upper()
    return "10&U" if normalized.replace("&", "").startswith("10U") else normalized


def _texas_age_centers(words):
    age_words = [
        word
        for word in words
        if TEXAS_AGE_TOKEN_PATTERN.fullmatch(word["text"])
    ]
    if len(age_words) < 6:
        return None
    age_words = sorted(age_words, key=lambda word: word["x0"])[:6]
    return [
        (_normalize_texas_age(word["text"]), (word["x0"] + word["x1"]) / 2)
        for word in age_words
    ]


def _read_texas_standards(pdf_path, gender, age_band):
    if age_band not in {"10&U", "11-12", "13-14"}:
        return {}

    records = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines, words = _page_lines(page)
            page_title = " ".join(line["text"] for line in lines[:3]).upper()
            if "TAGS TIME STANDARDS" not in page_title or "BONUS" in page_title:
                continue
            course = "LCM" if "LONG COURSE" in page_title else "SCY"

            age_centers = None
            in_bonus_section = False
            in_relay_section = False
            for line in lines:
                if "BONUS" in line["text"].upper():
                    in_bonus_section = True
                    continue
                if in_bonus_section:
                    continue

                age_centers = _texas_age_centers(line["words"]) or age_centers
                if not age_centers:
                    continue

                event_match = TEXAS_EVENT_PATTERN.search(line["text"])
                if not event_match:
                    continue

                age_columns = [
                    center
                    for label, center in age_centers
                    if label == age_band
                ]
                if not age_columns:
                    continue
                age_column_index = 0 if gender == "Girls" else 1
                target_center = age_columns[age_column_index]
                nearby_words = [
                    word
                    for word in words
                    if abs(word["top"] - line["top"]) <= 8
                ]
                times = _time_words(nearby_words)
                column_spacing = min(
                    right - left
                    for left, right in zip(
                        sorted(center for _, center in age_centers),
                        sorted(center for _, center in age_centers)[1:],
                    )
                )
                tolerance = column_spacing / 2
                matching_times = [
                    word
                    for word in times
                    if abs(
                        (word["x0"] + word["x1"]) / 2 - target_center
                    )
                    <= tolerance
                ]
                selected_time = (
                    min(
                        matching_times,
                        key=lambda word: abs(
                            (word["x0"] + word["x1"]) / 2 - target_center
                        ),
                    )
                    if matching_times
                    else None
                )

                distance = event_match.group(1)
                stroke = event_match.group(2).upper()
                if f"{distance} {stroke}" == "400 IM":
                    in_relay_section = True
                if stroke == "MR":
                    event = f"{distance} MED-R"
                elif stroke == "FR" and in_relay_section:
                    event = f"{distance} FR-R"
                else:
                    event = f"{distance} {stroke}"
                if selected_time:
                    records[(event, course)] = _clean_time(
                        selected_time["text"]
                    )
    return records


def get_time_standards(gender, age):
    """Extract USA and Texas standards for a swimmer and save the combined CSV.

    Age is a swimmer's integer age from 1 through 18. The returned dataframe
    has one row per event/course, with USA B-AAAA and Texas TAGS time columns.
    """
    normalized_gender = _normalize_gender(gender)
    age_band = _age_band(age)
    age_value = int(age)
    usa_pdf = _find_pdf("*motivational*pdf", "USA motivational standards")
    texas_pdf = _find_pdf("*tags*time*pdf", "Texas TAGS standards")

    usa_records = _read_usa_standards(usa_pdf, normalized_gender, age_band)
    texas_records = _read_texas_standards(
        texas_pdf, normalized_gender, age_band
    )
    if not usa_records:
        raise ValueError(
            f"No USA motivational standards found for {normalized_gender}, "
            f"age {age_value}"
        )
    if age_band in {"10&U", "11-12", "13-14"} and not texas_records:
        raise ValueError(
            f"No Texas TAGS standards found for {normalized_gender}, "
            f"age {age_value}"
        )

    usa_frame = pd.DataFrame.from_dict(usa_records, orient="index")
    usa_frame.index = pd.MultiIndex.from_tuples(
        usa_frame.index, names=["Event", "Course"]
    )
    usa_frame = usa_frame.reindex(columns=USA_COLUMNS)

    texas_frame = pd.DataFrame.from_dict(
        texas_records, orient="index", columns=["Texas TAGS"]
    )
    texas_frame.index = pd.MultiIndex.from_tuples(
        texas_frame.index, names=["Event", "Course"]
    )

    standards = usa_frame.join(texas_frame, how="outer").reset_index()
    course_order = {"SCY": 0, "SCM": 1, "LCM": 2}
    standards["_distance"] = (
        standards["Event"].str.extract(r"^(\d+)").astype(int)
    )
    standards["_course_order"] = standards["Course"].map(course_order)
    standards = standards.sort_values(
        ["_distance", "Event", "_course_order"], kind="stable"
    ).drop(columns=["_distance", "_course_order"])
    standards.insert(0, "Age", age_value)
    standards.insert(0, "Gender", normalized_gender)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / (
        f"time_standards_{normalized_gender.lower()}_{age_value}.csv"
    )
    standards.to_csv(output_path, index=False, encoding="utf-8-sig")
    return standards


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract USA Swimming and Texas TAGS time standards."
    )
    parser.add_argument("gender", help="Girls or Boys", default="Girls")
    parser.add_argument("age", type=int, help="Swimmer age from 1 to 18", default=10)
    arguments = parser.parse_args()
    try:
        result = get_time_standards(arguments.gender, arguments.age)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Saved {len(result)} standards to {OUTPUT_DIR}")
