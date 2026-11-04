# Data files

## windowsZones.xml

Maps Windows time zone identifiers (as sent by Outlook) to IANA time zone names and back. Used by `davbridge/timezones.py`
(territory `001`, the golden zone of each Windows identifier).

| | |
|---|---|
| Origin | Unicode CLDR, `common/supplemental/windowsZones.xml` (branch `main`, retrieved 2026-11-01) |
| URL | <https://raw.githubusercontent.com/unicode-org/cldr/main/common/supplemental/windowsZones.xml> |
| Data version | `<mapTimezones otherVersion="7e11800" typeVersion="2021a">` in the file header (the file's own `<version number>` is an unexpanded VCS keyword) |
| Header notice | `Copyright © 1991-2013 Unicode, Inc.`, terms of use: <http://www.unicode.org/copyright.html> (unchanged, as published) |
| SHA-256 | `5f902f94106a396cb7774f0b0e1ca4dfecb686494045350c0b4935d378e3913b` |
| License | Unicode License v3 (SPDX `Unicode-3.0`); license text and attribution: `NOTICE` and `THIRD_PARTY_NOTICES.md` |
| Modifications | none - the file is an unmodified copy |

The file is shipped inside the wheel as package data (`davbridge/data/windowsZones.xml`) and copied into the container image, so
`davbridge.timezones` finds it without the repository layout.

### Updating

Only by hand and reviewed: download the new file, replace it, record the new retrieval date, header version and SHA-256 here, and run the test suite
(`pytest tests/unit/ews/test_timezones.py`). The tests pin the golden mappings that Outlook clients depend on.
