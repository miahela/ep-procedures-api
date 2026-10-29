# Sample live report

Captured on 2026-10-29 from the user's successful local run against the
[European Parliament Open Data API](https://data.europarl.europa.eu/api/v2).
The copied exports are unchanged. The run.json file records the scope and
summary from the terminal output. The ../sample-output.txt file preserves
selected terminal lines; ../screenshot.png renders that excerpt.

The run processed procedure 2026-0210: 19 participations, 3 events, and
15 resolved MEPs. All 11 report checks passed; the command exited with code 0.

The meeting endpoint returned plenary rows. None passed the strict committee
and date filters, so no meeting, agenda, or cross-reference CSV was produced.
This sample verifies procedure collection, MEP resolution, exports, and the
meeting filters. Agenda response handling is covered by offline tests, not
by this live run.

Public data source: European Parliament, https://data.europarl.europa.eu/.
The API specification identifies its data license as CC BY 4.0:
https://creativecommons.org/licenses/by/4.0/.
