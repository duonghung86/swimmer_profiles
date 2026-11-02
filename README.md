# USA Swimming times collector

This script opens Google Chrome, lets you sign in manually, and saves the All
Times results for each swimmer in `swimmer_ids.txt` to `output\<swimmer-id>.csv`.
Each run appends a snapshot to that swimmer's CSV; `retrieved_at` records when
each set of results was collected.

## Run

From PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\collect_times.py
```

The script uses the installed Google Chrome browser and a separate, persistent
`chrome-profile` folder. Sign in there when Chrome opens, then press Enter in
PowerShell. Your password is not read or stored by the script. The profile
contains the browser's login session, so keep it private.

`swimmer_ids.txt` accepts one swimmer per line, either `ID` or `ID: Name`.
Blank lines and lines beginning with `#` are ignored.

The collector reads rendered HTML tables or All Times result cards and follows
visible Next/Load More controls. If the site changes its page layout or results
cannot be recognized, the script reports an error instead of writing an
apparently complete CSV. Use it only in accordance with USA Swimming's terms
and your authorization to access the swimmer records.

## Summarize best times

After collecting swimmer data, run:

```powershell
.\.venv\Scripts\python.exe .\summarize_times.py
```

The script reads swimmer CSV files in `output` and writes
`output\best_times.csv`. Each swimmer gets one row, each event gets a column,
and each value is that swimmer's fastest recorded time for the event across
all collected snapshots.

## Extract time standards

The USA motivational standards and Texas TAGS PDFs in `input` can be combined
for one gender and age. Install the dependencies with the existing
`requirements.txt`, then run:

```powershell
.\.venv\Scripts\python.exe .\extract_time_standards.py Girls 12
```

The `get_time_standards(gender, age)` function returns a pandas DataFrame and
saves the same rows to `output\time_standards_<gender>_<age>.csv`. Gender may
be Girls/Boys or Female/Male, and age is an integer from 1 through 18. Texas
TAGS standards are available only through age 14; for older ages, the Texas
column is empty while USA standards are still included.


## Run the dashboard

Install the dependencies from this folder, then launch the app:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\swimming_dashboard.py
```

The swimmer dropdowns are populated only from `swimmer_ids.txt`. The dashboard
shows girls' standards, compares both swimmers on the selected course, and
defaults to SCY. Choose LCM from the shared course dropdown to switch the
tables and charts. The progression event selector is to the left of the
progression chart; dashed lines indicate each swimmer's next faster standard
for that event when one is available.

SCY is short-course yards; LCM is long-course meters. A swimmer remains
selectable when listed but has no saved times yet.

Use **Add new swimmer** to enter an ID that is not already in
`swimmer_ids.txt`. The dashboard collects the swimmer's times in Chrome and
adds the ID to the roster after collection succeeds. Use **Update swimmer
times** to select one or more swimmers, or select all, and append their latest
times. Chrome opens with the collector's persistent profile; sign in if
needed, then confirm in the dashboard to begin collection. Collection runs in
the background, and the dashboard reloads saved times when it finishes. When
adding a swimmer, the collector looks up the member record by ID through USA
Swimming's visible profile heading first and uses the member record by ID as
a fallback. The name is saved with the ID in `swimmer_ids.txt`. If the member
name cannot be retrieved, the new swimmer is not added.

The radar chart legend is in its lower-right corner, and the medley label is
shortened to IM. The progression event selector is to the left of the taller
age progression chart. Radar progress scores are 1 for slower than B, 2 at B,
then 3 through 7 at BB through AAAA. Times between adjacent standards are
linearly interpolated between their scores.
