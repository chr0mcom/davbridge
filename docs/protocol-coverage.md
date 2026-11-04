# Protocol coverage (v0.1)

This table says what DAVbridge actually does per protocol operation. It is reviewed by hand and checked against the tests. If an
operation is not listed, it is not implemented.

**Status legend**

| Status | Meaning |
|---|---|
| **functional** | Implemented for calendar data, within [limitations.md](limitations.md) |
| **empty** | Answers truthfully with an empty or neutral result; nothing is faked |
| **rejected** | Refused with a defined error / status code |

Tested with (as of 2026-11-04; other versions may work but are untested): Nextcloud server 34.0.5 (CalDAV / sabre/dav);
Outlook for Mac 16.114 (build 26102031, `MacOutlook/16.114.26102031`) via EWS; Outlook for iOS (App Store version current on
2026-11-04, cloud service `Outlook-iOS-Android/1.0`) via Exchange ActiveSync 14.1. Outlook for Android was not tested.
Real Outlook binaries are not part of the automated tests; they form a manual release matrix.

Spec sources are listed in [protocol-sources.md](protocol-sources.md).

## Exchange Web Services (Outlook for Mac)

Endpoint `/EWS/Exchange.asmx`, HTTP Basic. GET returns a short text. Impersonation requests are refused.

| Operation | Status | Notes | Spec |
|---|---|---|---|
| `GetFolder` | functional | Calendar folders and the distinguished folders Outlook asks for | MS-OXWSFOLD |
| `FindFolder` | functional | | MS-OXWSFOLD |
| `SyncFolderHierarchy` | functional | Folder list from configuration / discovery | MS-OXWSSYNC |
| `FindItem` | functional | Calendar view and listing; same visibility filter as all reads | MS-OXWSFOLD, MS-OXWSMTGS |
| `GetItem` | functional | `IdOnly` + additional properties returns the full field set | MS-OXWSCORE, MS-OXWSMTGS |
| `CreateItem` | functional | Single `CalendarItem` only; attendees / meeting requests / messages rejected | MS-OXWSCORE, MS-OXWSMTGS |
| `UpdateItem` | functional | Listed fields only; recurrence, attachments, attendees rejected; empty updates succeed without writing; conditional on last delivered ETag | MS-OXWSCORE |
| `DeleteItem` | functional | Conditional on last delivered ETag | MS-OXWSCORE |
| `SyncFolderItems` | functional | CalDAV `sync-collection` based; full resync on invalid or regressed token | MS-OXWSSYNC |
| `Subscribe` | functional | Pull and streaming subscriptions; RAM only | MS-OXWSNTIF |
| `Unsubscribe` | functional | | MS-OXWSNTIF |
| `GetEvents` | functional | | MS-OXWSNTIF |
| `GetStreamingEvents` | functional | Unbuffered; keep-alives; closes after each event chunk and after `stream_max_seconds` | MS-OXWSNTIF |
| `GetUserConfiguration`, `CreateUserConfiguration`, `UpdateUserConfiguration`, `DeleteUserConfiguration` | functional | Stored in the SQLite state (bounded) | MS-OXWSUSRCFG |
| `GetDelegate` | empty | No delegates | per-operation MS-OXWS* documents |
| `GetAppManifests` | empty | No apps | per-operation MS-OXWS* documents |
| `GetRoomLists` | empty | No room lists | per-operation MS-OXWS* documents |
| `GetUserPhoto` | empty / not available | No photos | per-operation MS-OXWS* documents |
| `GetUserOofSettings` | empty | Own mailbox: out-of-office reported as disabled (no replies, no external audience); another mailbox: `ErrorNonExistentMailbox` | per-operation MS-OXWS* documents |
| `SendItem` | rejected | Sending is not supported | MS-OXWSCORE |
| any other operation | rejected | `ErrorInvalidRequest` | - |
| Attachments (`CreateAttachment`) | rejected | Outlook gives no visible error | - |

Response code names come from the EWS `ResponseCode` enumeration (`tests/data/response_codes.txt`).

## Exchange ActiveSync 14.1 (Outlook for iOS / Android)

Endpoint `/Microsoft-Server-ActiveSync`, HTTP Basic. Only protocol version **14.1** is accepted (`400` otherwise). `OPTIONS` advertises
the 17 commands below. Commands that are not advertised return `501`; unknown command codes `400`. Disabled unless the double opt-in is set.

| Command | Status | Notes | Spec |
|---|---|---|---|
| `Provision` | functional (compat.) | Two-phase handshake with a permissive policy that is never enforced; remote-wipe acknowledgement is answered but nothing is wiped | MS-ASPROV |
| `FolderSync` | functional | Static hierarchy: default calendar (type 8), further calendars (type 13), plus **empty** compatibility folders Inbox, Drafts, Deleted Items, Sent Items, Outbox, Tasks, Contacts. Stale key -> status 9 | MS-ASCMD |
| `FolderCreate` | functional (compat.) | **Only type 12 (user mail folder)**: accepted as an empty, per-device folder. Other types: status 3 | MS-ASCMD |
| `FolderDelete` | functional (compat.) | Only those device-local compatibility folders; else status 3 | MS-ASCMD |
| `FolderUpdate` | rejected | status 2 | MS-ASCMD |
| `Sync` | functional | Calendar folders: initial sync, paging (`window_max`), Add / Change / Delete / Fetch, `Wait` / heartbeat; read-only series; CalDAV version wins on conflict (status 7); delete brake; mail folders are always empty | MS-ASCMD, MS-ASCAL, MS-ASAIRS |
| `GetItemEstimate` | functional | Same visibility filter as Sync | MS-ASCMD |
| `Ping` | functional | Heartbeat 60-540 s; folder limit; waits without a thread | MS-ASCMD |
| `ItemOperations` | functional (partial) | `Fetch` of calendar items the device knows. `Move` and `EmptyFolderContents`: status 2 | MS-ASCMD |
| `Settings` | functional (partial) | `Oof` get (always off) and set-off accepted; `DeviceInformation` accepted; `UserInformation` get (send disabled); setting out-of-office or a device password -> status 7 | MS-ASCMD |
| `Search` | empty | Success with total 0 | MS-ASCMD |
| `ResolveRecipients` | empty | Status 1 overall, "no match" per recipient | MS-ASCMD |
| `MeetingResponse` | rejected | status 2 per request | MS-ASCMD |
| `MoveItems` | rejected | status 2 per item | MS-ASCMD |
| `SendMail`, `SmartReply`, `SmartForward` | rejected | Sending is disabled; a send-disabled status is returned | MS-ASCMD |

Calendar item fields (EAS `Calendar` / `AirSyncBase:Body`): subject, location, start/end, all-day, time zone, busy status, sensitivity,
reminder, categories, body (plain / HTML as requested, truncated at a limit), UID, organizer (read), recurrence and exceptions (read-only,
if exactly mappable). Attendees, attachments, meeting status changes and recurrence writes are rejected or ignored in a defined way (see
[limitations.md](limitations.md)). Events with protected custom properties (`features.protected_property_prefixes`) accept only the
configured writable fields.

## Autodiscover

| Variant | Path | Auth | Status | Notes |
|---|---|---|---|---|
| EWS POX | `/autodiscover/autodiscover.xml` | Basic | functional | Answers only for the signed-in account's own address; EWS URL from configuration (never from `Host`) |
| Mobile POX (`mobilesync`) | `/autodiscover/autodiscover.xml` | anonymous | functional (EAS enabled) | Account-neutral for configured domains; returns the `/Microsoft-Server-ActiveSync` URL; no `WWW-Authenticate` |
| JSON v2 | `/autodiscover/autodiscover.json[/v1.0/<address>]?Protocol=...` | anonymous | functional (EAS enabled) | `ActiveSync` and `AutodiscoverV1` protocols; 404 for foreign domains, 400 for other protocols |

## CalDAV assumptions

| Feature | Use |
|---|---|
| `PROPFIND` (principal, calendar home, collection properties, privileges) | Calendar info; optional discovery |
| `REPORT sync-collection` (RFC 6578) | Incremental change detection; invalid / regressed tokens trigger a full resync |
| `REPORT calendar-multiget`, `calendar-query` | Bulk fetch, time-range queries, UID lookup |
| `GET` / `PUT` / `DELETE` with `ETag`, `If-Match`, `If-None-Match` | Conditional writes; no cookies are kept; `Accept-Encoding: identity` is sent |
| `current-user-privilege-set` | Effective write permission |

Primarily tested against sabre/dav as shipped with Nextcloud (34.0.5). Other servers are not claimed to work. Observed sabre/dav
behaviors that DAVbridge handles: read-only shares answer `PUT` with 404; `DELETE` with `If-Match` on a missing object answers 412; UID conflicts
come as 409/412 with `no-uid-conflict`; an invalid sync token answers 403 `valid-sync-token`; compressed responses alter ETags.
