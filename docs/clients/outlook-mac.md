# Outlook for Mac

Outlook for Mac connects to DAVbridge as an **Exchange** account over Exchange Web Services (EWS). It talks to DAVbridge
directly; no Microsoft cloud service is involved in this path.

> DAVbridge is independent and not affiliated with, endorsed by or certified by Microsoft. It implements a limited
> calendar-only subset of EWS. The licensing terms of Outlook for Mac and of connecting it to non-Microsoft servers are your
> responsibility.

## Tested versions

As of 2026-11-04:

| Component | Tested version |
|---|---|
| Client | Outlook for Mac 16.114 (build 26102031, User-Agent `MacOutlook/16.114.26102031`), "new Outlook" on Apple silicon, via EWS |
| CalDAV server | Nextcloud server 34.0.5 (sabre/dav) |

Verified: account setup, create / edit / delete of single events, reminders, manual synchronization. Other versions may work but are
**untested**; Microsoft changes the client without notice. Please report your results (without secrets or calendar content).

## Before you start

- DAVbridge is reachable at a host name with a publicly trusted certificate ([installation.md](../installation.md)).
- You have a CalDAV **app password** for this Mac (for example named `davbridge-mac`). Do **not** use your normal login
  password; with two-factor authentication it would not work anyway, and a dedicated password can be revoked on its own.
- Your user exists in the DAVbridge configuration (`allowed_users`/`[[users]]`).

## Add the account

Menu names differ slightly between Outlook versions.

1. Open Outlook -> **Settings -> Accounts** (or *Tools -> Accounts*) -> **Add account** / **+**.
2. Enter the account address from your configuration, for example `alice@dav.example.com`, and continue.
3. If asked for the account type, choose **Exchange**.
4. Enter the credentials:
   - **User name:** the CalDAV user name, for example `alice` (the full address also works).
   - **Password:** the app password.
5. If Autodiscover is not available (you did not route `autodiscover.<domain>` or the address domain does not point to
   DAVbridge), choose manual configuration and enter the server `dav.example.com` (or the full address
   `https://dav.example.com/EWS/Exchange.asmx`).
6. Accept the account. Your calendar (display name from the configuration) appears under *Calendar*.

If setup fails, see [troubleshooting.md](../troubleshooting.md) (wrong password / 2FA, TLS chain, Autodiscover).

## What works

- Viewing single (non-recurring) events from the configured calendars.
- Creating, editing and deleting single events: subject, location, start/end, all-day, notes (body), busy status of the event,
  reminder, categories, private flag. Changes made in Outlook arrive in the CalDAV server immediately.
- Read-only calendars (`write = "never"` or no write privilege on the server) can be viewed but not changed.

## What does not work

- **Recurring events are not shown at all**, and cannot be created or edited.
- Invitations, attendees, meeting responses, attachments, scheduling assistant, rooms - not supported.
- Mail, People, Tasks and Notes are empty or unavailable.
- Events with an organizer or attendees (created elsewhere) are read-only.
- **Outlook does not tell you when DAVbridge refuses something.** If you add a recurrence, an attachment or invitees,
  Outlook keeps them locally while the server stores a plain single event. Do not do this.

Full list: [limitations.md](../limitations.md).

## Getting changes made elsewhere

Outlook for Mac does **not** automatically pick up changes made in Nextcloud or other CalDAV clients. After such a change:

- right-click the calendar in the sidebar -> **Synchronize**, or
- restart Outlook.

After DAVbridge itself was restarted or upgraded, Outlook's notification subscription is lost and is not renewed by Outlook;
use **Synchronize** once. (Changes you make in Outlook are sent immediately and are not affected.)

If two sides change the same event, the CalDAV version wins and Outlook shows no error; it adopts the CalDAV version on its
next synchronization.

## Remove or reset

- Remove the account in Outlook and revoke the app password on the CalDAV server.
- If Outlook shows a damaged or stale calendar after a CalDAV restore, an operator must reset the sync state
  ([operations.md](../operations.md#after-a-caldav-server-restore)), then remove and re-add the account.
