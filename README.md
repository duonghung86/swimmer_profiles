# USA Swimming times collector

This script opens Microsoft Edge, lets you sign in manually, and saves the All
Times results for each swimmer in `swimer_ids.txt` to `output\<swimmer-id>.csv`.
Each run appends a snapshot to that swimmer's CSV; `retrieved_at` records when
each set of results was collected.

## Run

From PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\collect_times.py
```

The script uses the installed Microsoft Edge browser and a separate, persistent
`edge-profile` folder. Sign in there when Edge opens, then press Enter in
PowerShell. Your password is not read or stored by the script. The profile
contains the browser's login session, so keep it private.

`swimer_ids.txt` accepts one swimmer per line, either `ID` or `ID: Name`.
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
