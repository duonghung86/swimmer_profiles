import asyncio
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
SWIMMER_FILE = ROOT / "swimer_ids.txt"
PROFILE_DIR = ROOT / "edge-profile"
OUTPUT_DIR = ROOT / "output"
ATHLETE_URL = "https://dhy-prod.usaswimming.org/search/athlete/{swimmer_id}/all-times?sortBy=Newest"
MAX_PAGES = 1000
BASE_COLUMNS = ("swimmer_id", "swimmer_name", "retrieved_at")
CARD_HEADERS = (
    "Event",
    "Time",
    "Age",
    "Points",
    "Standard",
    "Meet",
    "Team",
    "Date",
)
CARD_EVENT_PATTERN = re.compile(
    r"^\d{2,4}\s+(?:FR|BR|FL|BK|IM)\s+(?:SCY|SCM|LCM)$",
    re.IGNORECASE,
)
CARD_TIME_PATTERN = re.compile(r"^(?:\d{1,2}:)?\d{1,2}\.\d{2}$")
CARD_DATE_PATTERN = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def load_swimmers():
    if not SWIMMER_FILE.is_file():
        raise FileNotFoundError(f"Swimmer list not found: {SWIMMER_FILE}")

    swimmers = []
    seen = set()
    for line_number, raw_line in enumerate(
        SWIMMER_FILE.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        swimmer_id, separator, name = line.partition(":")
        swimmer_id = swimmer_id.strip()
        name = name.strip() if separator else ""
        if not re.fullmatch(r"[A-Za-z0-9-]+", swimmer_id):
            raise ValueError(
                f"Invalid swimmer ID on line {line_number}: {swimmer_id!r}"
            )
        if swimmer_id in seen:
            raise ValueError(f"Duplicate swimmer ID on line {line_number}: {swimmer_id}")
        seen.add(swimmer_id)
        swimmers.append((swimmer_id, name))

    if not swimmers:
        raise ValueError(f"No swimmer IDs found in {SWIMMER_FILE}")
    return swimmers


async def extract_table(page):
    return await page.evaluate(
        """() => {
            const table = document.querySelector("table, [role='table'], [role='grid']");
            if (!table) return null;

            const rows = Array.from(table.querySelectorAll("tr, [role='row']"));
            const cellText = (row) => Array.from(
                row.querySelectorAll(
                    "th, td, [role='columnheader'], [role='cell'], [role='rowheader']"
                )
            ).map((cell) => (cell.innerText || cell.textContent || "").trim());

            let headers = [];
            let dataRows = [];
            for (const row of rows) {
                const cells = cellText(row);
                if (!cells.length) continue;

                const isHeader = row.matches("thead tr, [role='row']")
                    ? row.querySelector(
                        "th, [role='columnheader'], [role='rowheader']"
                    ) !== null
                    : false;
                if (isHeader && !headers.length) {
                    headers = cells;
                } else {
                    dataRows.push(cells);
                }
            }

            if (!headers.length && dataRows.length) {
                const firstRow = rows.find((row) => cellText(row).length);
                if (firstRow && firstRow.querySelector("th, [role='columnheader']")) {
                    headers = dataRows.shift();
                }
            }
            return { headers, rows: dataRows };
        }"""
    )


def parse_card_text(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows = []
    index = 0
    while index < len(lines):
        if not CARD_EVENT_PATTERN.fullmatch(lines[index]):
            index += 1
            continue

        values = lines[index : index + len(CARD_HEADERS)]
        if (
            len(values) == len(CARD_HEADERS)
            and CARD_TIME_PATTERN.fullmatch(values[1])
            and CARD_DATE_PATTERN.fullmatch(values[7])
        ):
            rows.append(values)
            index += len(CARD_HEADERS)
        else:
            index += 1
    return rows


async def extract_results(page):
    table = await extract_table(page)
    if table and table["rows"]:
        return table

    text = await page.locator("body").inner_text()
    card_rows = parse_card_text(text)
    if card_rows:
        return {"headers": CARD_HEADERS, "rows": card_rows}
    return table


async def find_next_button(page):
    return await page.evaluate(
        """() => {
            const controls = Array.from(document.querySelectorAll("button, a"));
            const next = controls.find((element) => {
                const label = [
                    element.innerText,
                    element.getAttribute("aria-label"),
                    element.getAttribute("title")
                ].filter(Boolean).join(" ").trim().toLowerCase();
                const isNext = /\\b(next|load more|show more)\\b/.test(label);
                const disabled = element.disabled
                    || element.getAttribute("aria-disabled") === "true";
                return isNext && !disabled && element.getClientRects().length > 0;
            });
            if (!next) return null;
            next.setAttribute("data-collector-next", "true");
            return next.tagName.toLowerCase();
        }"""
    )


async def wait_for_results(page, seen_rows=None):
    results = None
    for _ in range(20):
        current = await extract_results(page)
        if current:
            results = current
        if results and results["rows"] and (
            seen_rows is None
            or any(
                tuple(cell.strip() for cell in row) not in seen_rows
                for row in results["rows"]
            )
        ):
            return results
        await page.wait_for_timeout(500)
    return results


def unique_headers(headers, width):
    names = []
    used = set(BASE_COLUMNS)
    for index in range(width):
        source = headers[index].strip() if index < len(headers) else ""
        name = source or f"column_{index + 1}"
        candidate = name
        suffix = 2
        while candidate in used:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used.add(candidate)
        names.append(candidate)
    return names


async def collect_rows(page, swimmer_id):
    await page.goto(
        ATHLETE_URL.format(swimmer_id=swimmer_id),
        wait_until="domcontentloaded",
    )
    await page.locator("body").wait_for()
    table = await wait_for_results(page)

    collected_headers = []
    collected_rows = []
    seen_rows = set()

    for _ in range(MAX_PAGES):
        if table:
            if table["headers"] and not collected_headers:
                collected_headers = table["headers"]
            for row in table["rows"]:
                normalized = tuple(cell.strip() for cell in row)
                if any(normalized) and normalized not in seen_rows:
                    seen_rows.add(normalized)
                    collected_rows.append(normalized)

        tag_name = await find_next_button(page)
        if not tag_name:
            break
        button = page.locator("[data-collector-next='true']").first
        before = len(seen_rows)
        await button.click()
        next_table = await wait_for_results(page, seen_rows)
        if not next_table or not next_table["rows"]:
            break
        for row in next_table["rows"]:
            normalized = tuple(cell.strip() for cell in row)
            if any(normalized) and normalized not in seen_rows:
                seen_rows.add(normalized)
                collected_rows.append(normalized)
        if len(seen_rows) == before:
            break
        table = next_table
    else:
        raise RuntimeError(f"Stopped after reaching the {MAX_PAGES}-page safety limit")

    if not collected_rows:
        title = await page.title()
        raise RuntimeError(
            "No recognizable All Times results were found. Confirm you are "
            "logged in and that the "
            f"All Times page loaded. Page title: {title!r}, URL: {page.url}"
        )

    return collected_headers, collected_rows


def write_csv(swimmer_id, swimmer_name, headers, rows):
    width = max((len(row) for row in rows), default=0)
    data_headers = unique_headers(headers, width)
    output_path = OUTPUT_DIR / f"{swimmer_id}.csv"
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fieldnames = [*BASE_COLUMNS, *data_headers]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    append = output_path.is_file() and output_path.stat().st_size > 0
    if append:
        with output_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            existing_fieldnames = next(csv.reader(csv_file), [])
        if existing_fieldnames != fieldnames:
            raise RuntimeError(
                f"CSV columns changed for {swimmer_id}. Move or rename "
                f"{output_path} before collecting with the new page layout."
            )

    mode = "a" if append else "w"
    encoding = "utf-8" if append else "utf-8-sig"
    with output_path.open(mode, newline="", encoding=encoding) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
            extrasaction="raise",
        )
        if not append:
            writer.writeheader()
        for row in rows:
            record = {
                "swimmer_id": swimmer_id,
                "swimmer_name": swimmer_name,
                "retrieved_at": retrieved_at,
            }
            record.update(
                {
                    data_headers[index]: row[index] if index < len(row) else ""
                    for index in range(width)
                }
            )
            writer.writerow(record)
    return output_path


async def main():
    swimmers = load_swimmers()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    failures = []

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://dhy-prod.usaswimming.org/",
                wait_until="domcontentloaded",
            )
            print(
                "Edge is open with the collector's dedicated profile. Sign in if "
                "needed, then return here."
            )
            await asyncio.to_thread(input, "Press Enter to collect swimmer times...")

            for swimmer_id, swimmer_name in swimmers:
                try:
                    headers, rows = await collect_rows(page, swimmer_id)
                    output_path = write_csv(swimmer_id, swimmer_name, headers, rows)
                    print(f"{swimmer_id}: saved {len(rows)} rows to {output_path}")
                except (PlaywrightError, RuntimeError, OSError) as error:
                    failures.append((swimmer_id, str(error)))
                    print(f"{swimmer_id}: FAILED: {error}", file=sys.stderr)
        finally:
            await context.close()

    if failures:
        print("\nSome swimmers could not be collected:", file=sys.stderr)
        for swimmer_id, error in failures:
            print(f"- {swimmer_id}: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
