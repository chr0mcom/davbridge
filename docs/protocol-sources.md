# Protocol sources

Inventory of the specifications DAVbridge was implemented from. DAVbridge was written independently from the public
documents; no source code of Microsoft or of other implementations is used. The only reproduced material is described in
[NOTICE](../NOTICE) (WBXML token tables, a list of EWS response-code names, CLDR data).

> Microsoft's documentation is copyrighted by Microsoft. Referencing it here does not grant any rights; the MIT license of this project
> does not cover Microsoft patents or trademarks and the project makes no statement that patent licenses are unnecessary (see [NOTICE](../NOTICE)).
> Specification text must not be pasted into this repository; describe behavior in your own words and cite the section.

Base URL for Open Specifications documents: `https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/<id-lowercase>/`
(for example `ms-ascmd`). The revision is the "Published Version" (protocol revision and date) shown on the Learn page of the document when it was looked up on 2026-11-04.
Microsoft updates these documents from time to time; the page shows the current version and keeps a list of previous ones.

## Exchange ActiveSync

| Document | Title | Revision | Used for | Implemented in |
|---|---|---|---|---|
| MS-ASHTTP | Exchange ActiveSync: HTTP Protocol | 23.0 (2025-05-20) | OPTIONS headers, plain and Base64 query, command codes, status line handling (401/403/413/501/503), content types, version negotiation | `src/davbridge/eas/http.py`, `eas/request.py`, `eas/devices.py` |
| MS-ASCMD | Exchange ActiveSync: Command Reference Protocol | 28.0 (2025-05-20) | Sync, FolderSync, FolderCreate/Delete/Update, Ping, Settings, GetItemEstimate, ItemOperations, MoveItems, MeetingResponse, Search, ResolveRecipients, SendMail / SmartReply / SmartForward; element order, status values; the Autodiscover `mobilesync` examples and schema | `src/davbridge/eas/commands/{sync,folders,ping,settings,items,rejected}.py`, `eas/skeleton.py`, `eas/mobile_autodiscover.py` |
| MS-ASWBXML | Exchange ActiveSync: WAP Binary XML (WBXML) Algorithm | 24.0, document version v20250520 (2025-05-20), section 2.1.2.1 | Code pages and tokens, encoding rules | `src/davbridge/eas/wbxml.py`, `eas/tags.py`, `eas/tags_table.py`; data check `tests/data/eas_tags.txt` |
| MS-ASCAL | Exchange ActiveSync: Calendar Class Protocol | 22.0 (2025-05-20) | Calendar elements (time zone, all-day, busy status, sensitivity, reminder, UID, recurrence, exceptions) | `src/davbridge/eas/calendar.py`, `eas/recurrence.py`, `eas/timezone.py` |
| MS-ASAIRS | Exchange ActiveSync: AirSyncBase Namespace Protocol | 22.0 (2025-05-20) | Body elements and types | `src/davbridge/eas/calendar.py` |
| MS-ASDTYPE | Exchange ActiveSync: Data Types | 21.0 (2025-05-20) | Compact DateTime, TimeZone structure | `src/davbridge/eas/calendar.py`, `eas/timezone.py` |
| MS-ASPROV | Exchange ActiveSync: Provisioning Protocol | 23.0 (2025-05-20) | Provision handshake, `EASProvisionDoc`, policy status values | `src/davbridge/eas/commands/provision.py` |
| W3C WBXML 1.2 | WAP Binary XML Content Format (W3C Note) | 1.2 | Binary token / string table layout, `mb_u_int32` | `src/davbridge/eas/wbxml.py` |

## Exchange Web Services and Autodiscover

| Document | Title | Revision | Used for | Implemented in |
|---|---|---|---|---|
| MS-OXWSCDATA | Common Web Service Data Types | 21.0 (2025-05-20) | SOAP / common types, namespaces | `src/davbridge/soap.py`, `operations/*.py` |
| MS-OXWSCORE | Core Items Web Service Protocol | 18.1 (2025-08-19) | `CreateItem`, `GetItem`, `UpdateItem`, `DeleteItem`, `SendItem`, response messages | `src/davbridge/operations/items.py`, `operations/auxiliary.py` |
| MS-OXWSFOLD | Folders and Folder Permissions Web Service Protocol | 14.0 (2025-05-20) | `GetFolder`, `FindFolder`, `FindItem` | `src/davbridge/operations/folders.py`, `operations/items.py` |
| MS-OXWSMTGS | Calendaring Web Service Protocol | 14.0 (2025-05-20) | `CalendarItem` fields, calendar view | `src/davbridge/ical.py`, `operations/items.py` |
| MS-OXWSSYNC | Mailbox Contents Synchronization Web Service Protocol | 10.0 (2025-05-20) | `SyncFolderHierarchy`, `SyncFolderItems` | `src/davbridge/operations/sync.py` |
| MS-OXWSNTIF | Notifications Web Service Protocol | 12.0 (2025-05-20) | `Subscribe`, `Unsubscribe`, `GetEvents`, `GetStreamingEvents` | `src/davbridge/operations/notifications.py`, `subscriptions.py`, `notifier.py` |
| MS-OXWSUSRCFG | User Configuration Web Service Protocol | 11.0 (2025-05-20) | `*UserConfiguration` | `src/davbridge/operations/auxiliary.py` |
| MS-OXWSDLGM | Delegate Access Management Web Service Protocol | 8.0 (2025-05-20) | Shape of the truthful empty `GetDelegate` answer | `src/davbridge/operations/auxiliary.py` |
| MS-OXWSGTRM | Get Rooms List Web Service Protocol | 10.1 (2025-08-19) | Shape of the truthful empty `GetRoomLists` answer | `src/davbridge/operations/auxiliary.py` |
| MS-OXWOOF | Out of Office (OOF) Web Service Protocol | 17.0 (2025-05-20) | Shape of the `GetUserOofSettings` answer (disabled state) | `src/davbridge/operations/auxiliary.py` |
| MS-OXWSCEXT | Client Extension Web Service Protocol | 10.0 (2025-05-20) | Shape of the truthful empty `GetAppManifests` answer | `src/davbridge/operations/auxiliary.py` |
| MS-OXWSPHOTO | Photo Web Service Protocol | 11.0 (2025-05-20) | Shape of the `GetUserPhoto` answer | `src/davbridge/operations/auxiliary.py` |
| EWS reference: `ResponseCode` | Exchange Web Services reference (not an Open Specifications document) | page retrieved 2026-11-04 | Names of response codes | `src/davbridge/errors.py`; `tests/data/response_codes.txt` |
| MS-OXDSCLI | Autodiscover Publishing and Lookup Protocol | 24.1 (2025-08-19) | POX Autodiscover request/response, JSON v2 | `src/davbridge/operations/autodiscover.py`, `eas/mobile_autodiscover.py` |
| MS-OXDISCO | Autodiscover HTTP Service Protocol | 19.0 (2025-05-20) | Lookup URLs and sequence on the client side | `src/davbridge/operations/autodiscover.py`, `eas/mobile_autodiscover.py` |

## CalDAV, WebDAV, iCalendar and time zones

| Document | Used for | Implemented in |
|---|---|---|
| RFC 4791 - Calendaring Extensions to WebDAV (CalDAV) | `calendar-query`, `calendar-multiget`, calendar collections, `supported-calendar-component-set`, `calendar-home-set` | `src/davbridge/caldav.py`, `calendar_info.py` |
| RFC 6578 - Collection Synchronization for WebDAV | `sync-collection` report, sync tokens | `src/davbridge/caldav.py` |
| RFC 5545 - Internet Calendaring and Scheduling Core Object Specification (iCalendar) | Event, alarm, recurrence, time zone properties; lossless patching | `src/davbridge/ical.py`, `eas/recurrence.py`, `timezones.py` |
| RFC 4918 - HTTP Extensions for WebDAV | `PROPFIND`, multistatus | `src/davbridge/caldav.py` |
| RFC 3744 - WebDAV Access Control Protocol | `current-user-principal`, `current-user-privilege-set` | `src/davbridge/caldav.py`, `permissions.py` |
| RFC 7232 - HTTP Conditional Requests | `ETag`, `If-Match`, `If-None-Match` | `src/davbridge/caldav.py` |
| Unicode CLDR `windowsZones.xml` | Windows <-> IANA time zone mapping (Unicode License v3) | `src/davbridge/timezones.py`; `data/windowsZones.xml` |

## Behavior documentation (not specifications)

- Microsoft Learn: *Using Basic authentication with Outlook for iOS and Android* and *Outlook for iOS and Android FAQ* - the
  cloud architecture that [privacy.md](privacy.md) describes.
- Nextcloud developer documentation and sabre/dav documentation - server behavior listed in [protocol-coverage.md](protocol-coverage.md).
- Observed client behavior (Outlook for Mac, Outlook for iOS) as of 2026-11-04, documented in [limitations.md](limitations.md).

## Maintenance rules

- When protocol behavior changes, update the relevant row, [protocol-coverage.md](protocol-coverage.md) and, if a new document is used, add it here.
- Before each release, compare every revision in this file with the "Published Version" on the Learn page, update changed rows and re-check [NOTICE](../NOTICE) against this inventory.
