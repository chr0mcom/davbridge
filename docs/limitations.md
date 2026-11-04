# Limitations

DAVbridge is an **alpha** (v0.1) interoperability layer, not an Exchange server. This page lists every known
functional and consistency limit without marketing shortcuts. If something is not described as working in
[protocol-coverage.md](protocol-coverage.md), assume it does not work.

## Scope

- **Calendar only.** No mail, no contacts, no tasks, no notes. Outlook's Mail, People and Tasks views stay empty or show
  errors. Outlook mobile may show empty mail views; sending mail is rejected.
- **No invitations or attendee handling.** No inviting people, editing attendees, answering meeting requests
  (accept/decline/tentative), proposing new times, attachments, or sending anything.
  Events that already carry an organizer or attendees (created elsewhere) are shown read-only.
- **No free/busy, scheduling assistant, room finder, delegation, out-of-office or photos.** Such operations return a truthful
  empty answer or are rejected in a defined way.
- **No Modern Authentication / OAuth.** Only HTTP Basic over TLS with CalDAV (app) passwords.
- **Clients:** EWS only for the tested Outlook for Mac usage; EAS exclusively version **14.1** for the tested
  Outlook mobile versions. No EAS 16.x promise. No general Microsoft 365 compatibility; other Exchange clients (Apple Mail,
  Thunderbird, Windows Outlook, ...) are untested and not supported.
- **CalDAV servers:** primarily tested against sabre/dav and Nextcloud. No blanket compatibility claim for other servers.
- **Microsoft behavior can change.** Outlook, Autodiscover and the Microsoft cloud used by Outlook mobile are outside
  this project; an update on Microsoft's side can break the connection at any time.

## Recurring events

- **Outlook for Mac (EWS):** recurring events are **not exposed at all** (they do not appear), and recurrence cannot be
  created or edited. Events whose iCalendar object contains several `VEVENT`s, to-dos and invalid objects are hidden as well.
- **Outlook mobile (EAS):** recurring events that can be mapped losslessly to EAS 14.1 are shown **read-only**.
  Series that cannot be mapped exactly are hidden rather than shown approximately - for example monthly series on day 29-31
  or on 29 February, yearly series with an interval other than 1, series whose first instance does not match the rule, counts or intervals
  above 999, `THISANDFUTURE` exceptions, and some weekly patterns that depend on the week start. Writing a series from
  the phone is rejected.
- **Silent divergence:** Outlook does not show an error when DAVbridge refuses a recurrence, an attachment or invitees. It
  creates the event, sends the recurrence/attachment afterwards, DAVbridge refuses it, and Outlook keeps the series locally
  while the server holds a single plain event. **Do not create recurring events, attachments or invitations in Outlook through DAVbridge.**

## Freshness (changes made outside Outlook)

- **Outlook for Mac does not reliably pick up CalDAV-side changes automatically.** DAVbridge reports changes through
  streaming notifications, but Outlook does not fetch the calendar because of them. Changes made in Nextcloud or another
  CalDAV client usually become visible only after right-click on the calendar -> **Synchronize**, or a restart of Outlook.
  Outlook's own periodic synchronization does not cover the calendar folder. A reliable interval has not been established.
- **EWS subscriptions are RAM state.** After a restart or upgrade of DAVbridge they are gone; Outlook does not resubscribe
  by itself. Outdated subscription IDs are answered with a deliberate delay to avoid hot loops. Use "Synchronize".
- **Outlook mobile** refreshes on its own schedule (via Microsoft's cloud). After a DAVbridge restart, pull to refresh the
  calendar once.

## Conflicts and deletions

- **Outlook for Mac:** the CalDAV version wins when the event changed since Outlook last received it; Outlook shows no
  error (the CalDAV version arrives with the next synchronization). Outlook sends no change key, so DAVbridge uses the last
  ETag it delivered to Outlook as the anchor.
- **Outlook mobile:** conflicts keep the CalDAV version (EAS status 7 plus the server version).
- **Loop breaker and delete brake can delay or eventually execute client wishes.** A repeatedly rejected deletion is
  rejected a limited number of times and then executed; and at most `max(delete_brake_minimum, 10 % of known items)` client
  deletions per user and hour are carried out. A legitimate bulk deletion on the phone may take hours.
- **Private events:** marking an event "Private" in Outlook sets `CLASS:PRIVATE`; other CalDAV tools or automations may hide it.

## Mobile compatibility behavior

- Outlook mobile requires an empty, device-specific "custom mail folder" to complete setup. DAVbridge accepts it as an
  **always-empty** folder. These compatibility folders (and the standard Inbox, Drafts, ... folders it advertises) never
  contain mail.
- Settings requests that cannot be honored (out-of-office, device password) are answered with an error status.
- Provisioning returns a permissive policy that is never enforced; DAVbridge never wipes a device.

## Fidelity of events

- Unknown or non-mappable iCalendar objects are not shown "approximately".
- Only the fields listed in `features.event_fields` are mapped. Other properties of an existing event are **preserved** when
  Outlook edits it (lossless patching) but are not visible or editable in Outlook.
- Time zones are mapped with the CLDR Windows-zone table. Zones without a mapping fall back to `server.default_timezone`.
- Descriptions are converted between HTML/plain text; formatting can be lost. Very large bodies are truncated for EAS.

## Operations

- **Exactly one instance.** No horizontal scaling, no high availability, no shared state across replicas.
- Losing `davbridge.sqlite` or `hmac.key` forces clients to rebuild their view (load, time; possibly repeated client actions).
- After restoring a CalDAV server from backup, sync state and devices must be reset by hand.
- Public reachability is required for Outlook mobile (Microsoft's cloud connects to you) and a publicly trusted TLS certificate for both clients.
- Calendar auto-discovery results change when calendars are renamed or moved on the server (new IDs, full resync).
- A self-check against your public URL does not prove that it is reachable from the internet.

## Legal and support

- No warranty, no support, no availability or compatibility guarantee (see [LICENSE](../LICENSE)).
- The project makes **no statement about patents** and does not claim patent clearance ([NOTICE](../NOTICE)). The MIT license
  grants no rights in Microsoft patents or trademarks. Operators are responsible for their own licensing, patent,
  trademark, privacy, security and backup assessment, including the terms of the Outlook clients they use.
