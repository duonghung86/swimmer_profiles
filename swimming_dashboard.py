import asyncio
import csv
import re
import sys
import threading
from pathlib import Path

import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from playwright.async_api import Error as PlaywrightError
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from collect_times import collect_swimmers
from extract_time_standards import get_time_standards


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SWIMMER_FILE = ROOT / "swimmer_ids.txt"
SWIMMER_COLUMNS = (
    "swimmer_id",
    "swimmer_name",
    "event",
    "course",
    "seconds",
    "time",
    "age",
    "standard",
    "aaaa_cut",
    "tags_cut",
)
EVENT_PATTERN = re.compile(
    r"^\s*(\d{1,4})\s+(MED-R|FR-R|MR-R|MR|FR|BK|BR|FL|IM)\s+(SCY|SCM|LCM)\s*$",
    re.IGNORECASE,
)
LEVELS = ("B", "BB", "A", "AA", "AAA", "AAAA")
LEVEL_SCORES = {level: index + 2 for index, level in enumerate(LEVELS)}
STROKE_NAMES = {
    "FR": "Freestyle",
    "BK": "Backstroke",
    "BR": "Breaststroke",
    "FL": "Butterfly",
    "IM": "IM",
}


def parse_time(value):
    """Return a swim time in seconds, or None when the value is not a time."""
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().rstrip("*")
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2})\.(\d{2})", text)
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    if match.group(1) and seconds >= 60:
        return None
    return minutes * 60 + seconds + int(match.group(3)) / 100


def format_time(seconds):
    if seconds is None or pd.isna(seconds):
        return "—"
    minutes, remainder = divmod(float(seconds), 60)
    if minutes:
        return f"{int(minutes)}:{remainder:05.2f}"
    return f"{remainder:.2f}"


def parse_event(value):
    match = EVENT_PATTERN.fullmatch(str(value or ""))
    if not match:
        return None
    distance, stroke, course = match.groups()
    if stroke.upper() in {"MR", "MR-R"}:
        stroke = "MED-R"
    return f"{distance} {stroke.upper()}", course.upper()


def load_roster():
    if not SWIMMER_FILE.is_file():
        raise FileNotFoundError(f"Swimmer list not found: {SWIMMER_FILE}")

    swimmers = {}
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
        if swimmer_id in swimmers:
            raise ValueError(
                f"Duplicate swimmer ID on line {line_number}: {swimmer_id}"
            )
        swimmers[swimmer_id] = name or swimmer_id
    if not swimmers:
        raise ValueError(f"No swimmer IDs found in {SWIMMER_FILE}")
    return swimmers


def load_saved_swimmers(roster):
    rows = []
    warnings = []
    for path in sorted(OUTPUT_DIR.glob("*.csv")):
        if path.name == "best_times.csv" or path.name.startswith("time_standards_"):
            continue
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                required = {"swimmer_id", "swimmer_name", "Event", "Time"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    continue
                for row_number, raw in enumerate(reader, start=2):
                    parsed_event = parse_event(raw.get("Event"))
                    seconds = parse_time(raw.get("Time"))
                    swimmer_id = (raw.get("swimmer_id") or "").strip()
                    if swimmer_id not in roster:
                        continue
                    if not parsed_event or seconds is None:
                        warnings.append(
                            f"{path.name}:{row_number}: skipped incomplete or invalid swim row"
                        )
                        continue
                    raw_age = (raw.get("Age") or "").strip()
                    try:
                        age = int(raw_age) if raw_age else None
                    except ValueError:
                        age = None
                    rows.append(
                        {
                            "swimmer_id": swimmer_id,
                            "swimmer_name": roster[swimmer_id],
                            "event": parsed_event[0],
                            "course": parsed_event[1],
                            "seconds": seconds,
                            "time": raw["Time"].strip(),
                            "age": age,
                            "standard": (raw.get("Standard") or "").strip(),
                            "aaaa_cut": None,
                            "tags_cut": None,
                        }
                    )
        except (OSError, UnicodeError, csv.Error) as error:
            warnings.append(f"Could not read {path.name}: {error}")
    return pd.DataFrame(rows, columns=SWIMMER_COLUMNS), warnings


def load_swimmer_data():
    roster = load_roster()
    saved, warnings = load_saved_swimmers(roster)
    for column in ("aaaa_cut", "tags_cut"):
        saved[column] = pd.to_numeric(saved[column], errors="coerce")
    return saved, warnings, roster


def standards_as_seconds(frame):
    result = {}
    for _, row in frame.iterrows():
        key = (str(row["Event"]), str(row["Course"]))
        cuts = {}
        for level in LEVELS:
            column = f"USA {level}" if f"USA {level}" in frame.columns else level
            cuts[f"USA {level}"] = parse_time(row[column])
        cuts["Texas TAGS"] = parse_time(row["Texas TAGS"])
        result[key] = cuts
    return result


def level_for_time(seconds, cuts):
    if seconds is None or not cuts:
        return ""
    for level in reversed(LEVELS):
        cut = cuts.get(f"USA {level}")
        if cut is not None and seconds <= cut:
            return level
    return ""


def level_progress_for_time(seconds, cuts):
    """Interpolate a swim time onto a 1-7 scale across USA standard cuts."""
    if seconds is None or pd.isna(seconds) or not cuts:
        return None

    available_cuts = []
    for level in LEVELS:
        cut = cuts.get(f"USA {level}")
        if cut is not None and not pd.isna(cut):
            available_cuts.append((LEVEL_SCORES[level], float(cut)))
    if not available_cuts:
        return None

    b_cut = cuts.get("USA B")
    if b_cut is not None and not pd.isna(b_cut) and seconds > b_cut:
        return 1.0

    for (lower_score, slower_cut), (higher_score, faster_cut) in zip(
        available_cuts, available_cuts[1:]
    ):
        if slower_cut >= seconds >= faster_cut and slower_cut > faster_cut:
            fraction = (slower_cut - seconds) / (slower_cut - faster_cut)
            return lower_score + fraction * (higher_score - lower_score)

    fastest_score, fastest_cut = available_cuts[-1]
    if seconds < fastest_cut:
        return float(fastest_score)
    if b_cut is not None and not pd.isna(b_cut) and seconds >= b_cut:
        return float(LEVEL_SCORES["B"])
    return None


def next_cut_for_time(seconds, cuts):
    """Return the closest faster standard level and its time."""
    candidates = []
    for level in (*LEVELS, "TAGS"):
        column = "Texas TAGS" if level == "TAGS" else f"USA {level}"
        cut = cuts.get(column)
        if cut is not None and not pd.isna(cut) and cut < seconds:
            candidates.append((cut, level))
    return max(candidates, default=(None, None))


def short_name(name):
    name = str(name)
    if len(name) <= 14:
        return name
    return f"{name[:13].rstrip()}…"


def event_sort_key(event, course):
    parsed = re.match(r"(\d+)\s+(.*)", str(event))
    course_order = {"SCY": 0, "SCM": 1, "LCM": 2}
    return (
        int(parsed.group(1)) if parsed else 0,
        parsed.group(2) if parsed else str(event),
        course_order.get(str(course), 9),
    )


def add_roster_entry(swimmer_id, swimmer_name):
    swimmer_id = swimmer_id.strip()
    swimmer_name = swimmer_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", swimmer_id):
        raise ValueError("Swimmer ID may contain only letters, numbers, and hyphens.")
    if not swimmer_name or any(character in swimmer_name for character in "\r\n"):
        raise ValueError("A valid swimmer name is required.")
    roster = load_roster()
    if swimmer_id in roster:
        raise ValueError(f"Swimmer ID {swimmer_id} is already in {SWIMMER_FILE.name}.")

    existing_text = SWIMMER_FILE.read_text(encoding="utf-8-sig")
    with SWIMMER_FILE.open("a", encoding="utf-8", newline="") as file:
        if existing_text and not existing_text.endswith(("\n", "\r")):
            file.write("\n")
        file.write(f"{swimmer_id}: {swimmer_name}\n")


class UpdateSwimmersDialog(QDialog):
    def __init__(self, roster, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update swimmer times")
        self.resize(430, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select one or more swimmers to update:"))

        self.swimmer_list = QListWidget()
        self.swimmer_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        for swimmer_id, name in roster.items():
            item = QListWidgetItem(f"{swimmer_id} · {name}")
            item.setData(Qt.ItemDataRole.UserRole, swimmer_id)
            self.swimmer_list.addItem(item)
        layout.addWidget(self.swimmer_list, 1)

        selection_buttons = QHBoxLayout()
        select_all_button = QPushButton("Select all")
        clear_button = QPushButton("Clear")
        select_all_button.clicked.connect(self.swimmer_list.selectAll)
        clear_button.clicked.connect(self.swimmer_list.clearSelection)
        selection_buttons.addWidget(select_all_button)
        selection_buttons.addWidget(clear_button)
        selection_buttons.addStretch(1)
        layout.addLayout(selection_buttons)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        update_button = buttons.addButton(
            "Update selected",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        update_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_swimmers(self):
        return [
            (
                item.data(Qt.ItemDataRole.UserRole),
                item.text().split(" · ", 1)[1],
            )
            for item in self.swimmer_list.selectedItems()
        ]


class CollectionWorker(QThread):
    browser_ready = pyqtSignal()
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, swimmers):
        super().__init__()
        self.swimmers = swimmers
        self._continue_event = threading.Event()

    def continue_after_login(self):
        self._continue_event.set()

    async def _wait_for_login(self):
        self.browser_ready.emit()
        await asyncio.to_thread(self._continue_event.wait)

    def run(self):
        try:
            saved, failures = asyncio.run(
                collect_swimmers(
                    self.swimmers,
                    on_browser_ready=self._wait_for_login,
                )
            )
        except (PlaywrightError, OSError, RuntimeError, ValueError) as error:
            self.failed.emit(str(error))
        else:
            self.completed.emit(saved, failures)


class SwimmerPanel(QWidget):
    def __init__(self, title, color, swimmers, on_change):
        super().__init__()
        self.on_change = on_change
        self.color = color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("panelHeading")
        heading.setStyleSheet(f"color: {color};")
        layout.addWidget(heading)

        self.swimmer_combo = QComboBox()
        self.swimmer_combo.setObjectName("swimmerSelector")
        for swimmer_id, name in swimmers:
            self.swimmer_combo.addItem(name, swimmer_id)
        layout.addWidget(self.swimmer_combo)

        self.age_label = QLabel("Age: —")
        self.age_label.setObjectName("mutedLabel")
        layout.addWidget(self.age_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Event", "PB", "USA Level", "TAGS"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.table, 1)

        self.swimmer_combo.currentIndexChanged.connect(self._selection_changed)

    def _selection_changed(self):
        if self.on_change:
            self.on_change()

    def selected_id(self):
        return self.swimmer_combo.currentData()

    def set_age(self, age):
        self.age_label.setText(f"Age: {age}" if age is not None else "Age: unknown")

    def set_rows(self, rows, other_rows):
        self.table.setRowCount(len(rows))
        other_times = {
            (row["event"], row["course"]): row["seconds"]
            for row in other_rows
        }
        for row_index, row in enumerate(rows):
            key = (row["event"], row["course"])
            values = (
                f'{row["event"]} {row["course"]}',
                format_time(row["seconds"]),
                row["level"] or "—",
                "Yes" if row["tags"] else "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1 and key in other_times:
                    other_time = other_times[key]
                    if row["seconds"] < other_time:
                        item.setBackground(QColor("#dcfce7"))
                        item.setForeground(QColor("#166534"))
                    elif row["seconds"] > other_time:
                        item.setBackground(QColor("#fee2e2"))
                        item.setForeground(QColor("#991b1b"))
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
                self.table.setItem(row_index, column, item)
        self.table.resizeRowsToContents()


class SwimmingDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Swimming Analytics · Swimmer Comparison")
        self.resize(1500, 900)
        self.setMinimumSize(1080, 680)
        self.data, self.data_warnings, self.roster = load_swimmer_data()
        self.standard_cache = {}
        self.standard_errors = {}
        self.collection_worker = None
        self.collection_purpose = None
        self.pending_new_swimmer_id = None
        swimmer_choices = list(self.roster.items())

        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 8, 14, 8)
        root_layout.setSpacing(5)

        course_row = QHBoxLayout()
        course_row.addWidget(QLabel("Course"))
        self.course_combo = QComboBox()
        self.course_combo.addItems(["SCY", "LCM"])
        self.course_combo.setCurrentText("SCY")
        course_row.addWidget(self.course_combo)
        course_row.addStretch(1)
        self.add_swimmer_button = QPushButton("Add new swimmer")
        self.update_times_button = QPushButton("Update swimmer times")
        self.add_swimmer_button.clicked.connect(self.add_new_swimmer)
        self.update_times_button.clicked.connect(self.update_swimmer_times)
        course_row.addWidget(self.add_swimmer_button)
        course_row.addWidget(self.update_times_button)
        root_layout.addLayout(course_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.panel_a = SwimmerPanel(
            "SWIMMER A", "#2563eb", swimmer_choices, self.refresh
        )
        self.panel_b = SwimmerPanel(
            "SWIMMER B", "#e11d48", swimmer_choices, self.refresh
        )
        splitter.addWidget(self.panel_a)

        middle = QWidget()
        self.middle_layout = QVBoxLayout(middle)
        self.middle_layout.setContentsMargins(10, 12, 10, 12)
        self.radar_figure = Figure(figsize=(10, 5.3), facecolor="#ffffff")
        self.radar_canvas = FigureCanvasQTAgg(self.radar_figure)
        self.radar_canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.radar_canvas.setMinimumHeight(280)
        self.radar_axis = self.radar_figure.add_subplot(111, projection="polar")
        self.middle_layout.addWidget(self.radar_canvas, 3)

        progression_row = QHBoxLayout()
        progression_row.setSpacing(8)
        event_panel = QWidget()
        event_panel.setMaximumWidth(170)
        event_layout = QVBoxLayout(event_panel)
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_label = QLabel("Progression event")
        self.event_combo = QComboBox()
        self.event_combo.currentIndexChanged.connect(self.refresh)
        self.course_combo.currentIndexChanged.connect(self._course_changed)
        event_layout.addWidget(event_label)
        event_layout.addWidget(self.event_combo)
        event_layout.addStretch(1)
        progression_row.addWidget(event_panel)

        self.progress_figure = Figure(figsize=(6.4, 3.2), facecolor="#ffffff")
        self.progress_canvas = FigureCanvasQTAgg(self.progress_figure)
        self.progress_canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.progress_canvas.setMinimumHeight(190)
        self.progress_axis = self.progress_figure.add_subplot(111)
        progression_row.addWidget(self.progress_canvas, 1)
        self.middle_layout.addLayout(progression_row, 2)
        splitter.addWidget(middle)
        splitter.addWidget(self.panel_b)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([330, 700, 330])
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(
            f"Loaded {len(swimmer_choices)} swimmers from {SWIMMER_FILE.name}."
        )
        self._initialize_selectors()
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f7fb; color: #172033; }
            QSplitter::handle { background: #e2e8f0; width: 3px; }
            QLabel#panelHeading { font-size: 14px; font-weight: 750; letter-spacing: 1px; }
            QLabel#mutedLabel { color: #64748b; }
            QComboBox {
                background: white; border: 1px solid #d5dce7; border-radius: 7px;
                padding: 8px 10px; min-height: 20px;
            }
            QComboBox:focus { border-color: #3b82f6; }
            QPushButton {
                background: #2563eb; color: white; border: 0; border-radius: 7px;
                padding: 8px 12px; font-weight: 600;
            }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:disabled { background: #94a3b8; }
            QTableWidget {
                background: white; alternate-background-color: #f8fafc;
                border: 1px solid #e2e8f0; border-radius: 8px; gridline-color: #edf0f5;
            }
            QHeaderView::section {
                background: #eef2f7; border: 0; padding: 8px 5px;
                font-weight: 650; color: #475569;
            }
            QStatusBar { background: #f5f7fb; color: #64748b; }
            """
        )
        self.refresh()

    def add_new_swimmer(self):
        swimmer_id, accepted = QInputDialog.getText(
            self,
            "Add new swimmer",
            "Enter swimmer ID:",
        )
        if not accepted:
            return
        swimmer_id = swimmer_id.strip()
        try:
            if not re.fullmatch(r"[A-Za-z0-9-]+", swimmer_id):
                raise ValueError(
                    "Swimmer ID may contain only letters, numbers, and hyphens."
                )
            current_roster = load_roster()
            if swimmer_id in current_roster:
                QMessageBox.information(
                    self,
                    "Swimmer already exists",
                    f"{swimmer_id} is already listed in {SWIMMER_FILE.name}.",
                )
                return
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Invalid swimmer ID", str(error))
            return

        self._start_collection(
            [(swimmer_id, "")],
            purpose="add",
            new_swimmer_id=swimmer_id,
        )

    def update_swimmer_times(self):
        try:
            roster = load_roster()
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Could not read swimmer list", str(error))
            return
        dialog = UpdateSwimmersDialog(roster, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        swimmers = dialog.selected_swimmers()
        if not swimmers:
            QMessageBox.information(
                self,
                "No swimmers selected",
                "Select at least one swimmer to update.",
            )
            return
        self._start_collection(swimmers, purpose="update")

    def _start_collection(self, swimmers, purpose, new_swimmer_id=None):
        if self.collection_worker is not None and self.collection_worker.isRunning():
            return
        self.collection_purpose = purpose
        self.pending_new_swimmer_id = new_swimmer_id
        self.add_swimmer_button.setEnabled(False)
        self.update_times_button.setEnabled(False)
        self.statusBar().showMessage(
            f"Opening Chrome to update {len(swimmers)} swimmer(s)..."
        )
        worker = CollectionWorker(swimmers)
        worker.browser_ready.connect(self._confirm_browser_login)
        worker.completed.connect(self._collection_completed)
        worker.failed.connect(self._collection_failed)
        worker.finished.connect(self._collection_thread_finished)
        self.collection_worker = worker
        worker.start()

    def _confirm_browser_login(self):
        QMessageBox.information(
            self,
            "Chrome is ready",
            "Chrome has opened the USA Swimming site. Sign in if needed, "
            "then click OK to start collecting times.",
        )
        if self.collection_worker is not None:
            self.collection_worker.continue_after_login()

    def _collection_completed(self, saved, failures):
        saved_names = {
            swimmer_id: swimmer_name
            for swimmer_id, swimmer_name, _, _ in saved
        }
        if (
            self.collection_purpose == "add"
            and self.pending_new_swimmer_id in saved_names
        ):
            try:
                add_roster_entry(
                    self.pending_new_swimmer_id,
                    saved_names[self.pending_new_swimmer_id],
                )
            except (OSError, ValueError) as error:
                QMessageBox.critical(
                    self,
                    "Times collected but swimmer was not added",
                    f"Times were saved, but {SWIMMER_FILE.name} could not be "
                    f"updated: {error}",
                )
                self._reload_swimmers()
                return

        self._reload_swimmers()
        messages = [
            f"{swimmer_id} ({swimmer_name}): saved {row_count} rows to {path}"
            for swimmer_id, swimmer_name, row_count, path in saved
        ]
        messages.extend(
            f"{swimmer_id}: FAILED: {error}" for swimmer_id, error in failures
        )
        if messages:
            box = QMessageBox(self)
            box.setWindowTitle("Swimmer update results")
            box.setText(
                f"Updated {len(saved)} swimmer(s); "
                f"{len(failures)} failed."
            )
            box.setDetailedText("\n".join(messages))
            box.setIcon(
                QMessageBox.Icon.Warning
                if failures
                else QMessageBox.Icon.Information
            )
            box.exec()

    def _collection_failed(self, message):
        QMessageBox.critical(self, "Could not collect swimmer times", message)

    def _collection_thread_finished(self):
        self.add_swimmer_button.setEnabled(True)
        self.update_times_button.setEnabled(True)
        self.collection_worker = None
        self.collection_purpose = None
        self.pending_new_swimmer_id = None

    def _reload_swimmers(self):
        selected_ids = (
            self.panel_a.selected_id(),
            self.panel_b.selected_id(),
        )
        try:
            self.data, self.data_warnings, self.roster = load_swimmer_data()
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self,
                "Could not reload swimmer data",
                str(error),
            )
            return
        for panel, selected_id in zip(
            (self.panel_a, self.panel_b), selected_ids
        ):
            combo = panel.swimmer_combo
            combo.blockSignals(True)
            combo.clear()
            for swimmer_id, name in self.roster.items():
                combo.addItem(name, swimmer_id)
            index = combo.findData(selected_id)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self.standard_cache.clear()
        self.standard_errors.clear()
        self._update_event_choices()
        self.refresh()

    def _initialize_selectors(self):
        if self.panel_b.swimmer_combo.count() > 1:
            self.panel_b.swimmer_combo.setCurrentIndex(1)
        self._update_event_choices()

    def _course_changed(self, *_):
        self._update_event_choices()
        self.refresh()

    def _update_event_choices(self):
        course = self.course_combo.currentText()
        events = sorted(
            {
                f"{row.event} {row.course}"
                for row in self.data[self.data["course"] == course][
                    ["event", "course"]
                ].itertuples(index=False)
            },
            key=self._event_key,
        )
        previous = self.event_combo.currentText()
        self.event_combo.blockSignals(True)
        self.event_combo.clear()
        self.event_combo.addItems(events)
        if previous in events:
            self.event_combo.setCurrentText(previous)
        elif "50 FR SCY" in events:
            self.event_combo.setCurrentText("50 FR SCY")
        elif events:
            self.event_combo.setCurrentIndex(0)
        self.event_combo.blockSignals(False)

    @staticmethod
    def _event_key(value):
        parsed = parse_event(value)
        return event_sort_key(*parsed) if parsed else (0, str(value), 9)

    def profile(self, swimmer_id):
        rows = self.data[self.data["swimmer_id"] == swimmer_id]
        ages = rows["age"].dropna()
        return {
            "id": swimmer_id,
            "name": self.roster[swimmer_id],
            "age": int(ages.max()) if not ages.empty else None,
        }

    def standards_for(self, profile):
        if profile["age"] is None:
            return {}
        key = profile["age"]
        if key in self.standard_cache:
            return self.standard_cache[key]
        try:
            standards = standards_as_seconds(
                get_time_standards("Girls", profile["age"])
            )
        except (OSError, ValueError) as error:
            self.standard_errors[key] = str(error)
            self.standard_cache[key] = {}
            return {}
        self.standard_cache[key] = standards
        return standards

    def swimmer_summary(self, profile, standards):
        rows = self.data[
            (self.data["swimmer_id"] == profile["id"])
            & (self.data["course"] == self.course_combo.currentText())
        ]
        summaries = []
        for (event, course), group in rows.groupby(["event", "course"]):
            best_index = group["seconds"].idxmin()
            best = group.loc[best_index]
            key = (event, course)
            cuts = standards.get(key, {})
            level = level_for_time(best["seconds"], cuts)
            if not level:
                level = str(best["standard"] or "")
                if level.casefold().startswith("slower"):
                    level = ""
            tags_cut = cuts.get("Texas TAGS")
            tags = (
                tags_cut is not None
                and best["seconds"] <= tags_cut
            )
            summaries.append(
                {
                    "event": event,
                    "course": course,
                    "seconds": float(best["seconds"]),
                    "level": level,
                    "tags": tags,
                }
            )
        return sorted(
            summaries,
            key=lambda row: event_sort_key(row["event"], row["course"]),
        )

    def _fill_tables(self, summary_a, summary_b):
        self.panel_a.set_rows(summary_a, summary_b)
        self.panel_b.set_rows(summary_b, summary_a)

    def _draw_radar(self, profiles, standards):
        axis = self.radar_axis
        axis.clear()
        categories = list(STROKE_NAMES.values())
        angles = [
            index / len(categories) * 2 * 3.141592653589793
            for index in range(len(categories))
        ]
        closed_angles = [*angles, angles[0]]
        course = self.course_combo.currentText()
        colors = ("#2563eb", "#e11d48")

        for profile, cut_rows, color in zip(profiles, standards, colors):
            swimmer_rows = self.data[self.data["swimmer_id"] == profile["id"]]
            swimmer_rows = swimmer_rows[swimmer_rows["course"] == course]
            swimmer_rows = swimmer_rows.loc[
                swimmer_rows.groupby(["event", "course"])["seconds"].idxmin()
            ]
            values = []
            for stroke in STROKE_NAMES:
                stroke_rows = swimmer_rows[
                    swimmer_rows["event"].str.endswith(f" {stroke}")
                ]
                ratios = []
                for _, record in stroke_rows.iterrows():
                    cuts = cut_rows.get((record["event"], record["course"]), {})
                    if record["seconds"]:
                        level_progress = level_progress_for_time(
                            record["seconds"], cuts
                        )
                        if level_progress is not None:
                            ratios.append(level_progress)
                values.append(sum(ratios) / len(ratios) if ratios else 0)
            if any(values):
                closed_values = [*values, values[0]]
                axis.plot(
                    closed_angles,
                    closed_values,
                    color=color,
                    linewidth=2,
                    marker="o",
                    markersize=4,
                    label=short_name(profile["name"]),
                )
                axis.fill(closed_angles, closed_values, color=color, alpha=0.16)

        axis.set_xticks(angles, categories)
        axis.set_ylim(0, len(LEVELS) + 1)
        level_ticks = [LEVEL_SCORES[level] for level in LEVELS]
        axis.set_yticks(level_ticks)
        axis.set_yticklabels([])
        for index, (radius, level) in enumerate(zip(level_ticks, LEVELS)):
            label_angle = 162 if index % 2 == 0 else 198
            axis.text(
                label_angle * 3.141592653589793 / 180,
                radius,
                level,
                ha="center",
                va="center",
                fontsize=10,
                fontweight="bold",
                color="#334155",
                bbox={
                    "facecolor": "#ffffff",
                    "edgecolor": "none",
                    "alpha": 0.9,
                    "pad": 1.5,
                },
                zorder=10,
            )
        axis.grid(color="#dce3ed", linewidth=0.8)
        axis.set_facecolor("#ffffff")
        if axis.get_legend_handles_labels()[0]:
            axis.legend(
                loc="lower right",
                fontsize=8,
            )
        else:
            axis.text(
                0.5,
                0.5,
                f"No {course} swim times available for the radar chart",
                transform=axis.transAxes,
                ha="center",
                va="center",
                color="#64748b",
                fontsize=9,
            )

    def _draw_progression(self, profiles, standards):
        axis = self.progress_axis
        axis.clear()
        event_course = parse_event(self.event_combo.currentText())
        colors = ("#2563eb", "#e11d48")
        plotted = False
        benchmark_plotted = False
        if event_course:
            event, course = event_course
            for profile, color in zip(profiles, colors):
                rows = self.data[
                    (self.data["swimmer_id"] == profile["id"])
                    & (self.data["event"] == event)
                    & (self.data["course"] == course)
                    & self.data["age"].notna()
                ]
                if rows.empty:
                    continue
                progression = (
                    rows.groupby("age", as_index=False)["seconds"]
                    .min()
                    .sort_values("age")
                )
                axis.plot(
                    progression["age"],
                    progression["seconds"],
                    color=color,
                    marker="o",
                    linewidth=2,
                    label=short_name(profile["name"]),
                )
                plotted = True

            for profile, standard_rows, color in zip(
                profiles, standards, colors
            ):
                current_rows = self.data[
                    (self.data["swimmer_id"] == profile["id"])
                    & (self.data["event"] == event)
                    & (self.data["course"] == course)
                ]
                if current_rows.empty:
                    continue
                personal_best = float(current_rows["seconds"].min())
                cuts = standard_rows.get((event, course), {})
                next_cut, target_level = next_cut_for_time(personal_best, cuts)
                if next_cut is not None:
                    axis.axhline(
                        next_cut,
                        color=color,
                        linestyle="--",
                        linewidth=1.4,
                        label=(
                            f"{short_name(profile['name'])} · next {target_level} "
                            f"({format_time(next_cut)})"
                        ),
                    )
                    benchmark_plotted = True
            axis.set_title(
                f"Age Progression · {self.event_combo.currentText()}",
                fontsize=12,
                fontweight="bold",
                pad=10,
            )
        if plotted or benchmark_plotted:
            axis.invert_yaxis()
            axis.set_xlabel(
                "Age (years)" if plotted else "Age history unavailable"
            )
            axis.set_ylabel("Time (seconds)")
            axis.grid(color="#e2e8f0", linewidth=0.8)
            axis.legend(fontsize=8, loc="best")
        else:
            axis.text(
                0.5,
                0.5,
                "No age history for this event yet",
                transform=axis.transAxes,
                ha="center",
                va="center",
                color="#64748b",
            )
            axis.set_axis_off()

    def refresh(self, *_):
        if not hasattr(self, "panel_a") or not self.panel_a.selected_id():
            return
        profiles = [
            self.profile(self.panel_a.selected_id()),
            self.profile(self.panel_b.selected_id()),
        ]
        panels = (self.panel_a, self.panel_b)
        standards = []
        summaries = []
        for panel, profile in zip(panels, profiles):
            panel.set_age(profile["age"])
            cut_rows = self.standards_for(profile)
            standards.append(cut_rows)
            summaries.append(self.swimmer_summary(profile, cut_rows))

        self._fill_tables(summaries[0], summaries[1])
        self._draw_radar(profiles, standards)
        self._draw_progression(profiles, standards)
        self.radar_figure.tight_layout(pad=2.0)
        self.progress_figure.tight_layout(pad=1.2)
        self.radar_canvas.draw_idle()
        self.progress_canvas.draw_idle()

        errors = [
            self.standard_errors[key]
            for key in {profile["age"] for profile in profiles}
            if key in self.standard_errors
        ]
        if errors:
            self.statusBar().showMessage(
                f"Could not load time standards: {errors[0]}"
            )
        elif self.data_warnings:
            self.statusBar().showMessage(self.data_warnings[0])
        else:
            self.statusBar().showMessage(
                f"Showing {self.course_combo.currentText()} times; green PB cells are faster."
            )


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Swimming Analytics")
    app.setFont(QFont("Segoe UI", 10))
    window = SwimmingDashboard()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
