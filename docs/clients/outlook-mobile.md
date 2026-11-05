# Outlook for iOS and Android

> ## Privacy warning - read before you set anything up
>
> Outlook for iOS and Android does **not** connect from your phone to DAVbridge. It connects through **Microsoft-operated
> cloud services**, which synchronize with DAVbridge over the internet and hand the data to the app.
> As a result:
>
> - **A copy of the whole calendar - including private events, descriptions, locations and third-party data - can be
>   stored and processed by Microsoft.**
> - Microsoft's service needs your account credentials (the app password) to reach DAVbridge.
> - Neither DAVbridge nor the operator controls region, retention or deletion of that copy. Removing the account or
>   revoking the app password stops synchronization but does not delete what Microsoft already holds.
> - As the operator you must assess contracts, privacy notices, data of third parties and international transfers yourself.
>
> Details: [privacy.md](../privacy.md). Outlook mobile support is **disabled by default** and has to be enabled by the
> operator with an explicit acknowledgement ([configuration.md](../configuration.md#outlook-mobile-eas-double-opt-in)).
> If you do not accept this, use Outlook for Mac only, or a CalDAV-native calendar app on the phone.

DAVbridge is independent and not affiliated with, endorsed by or certified by Microsoft.

## Tested versions

As of 2026-11-04:

| Component | Tested version |
|---|---|
| Client | Outlook for iOS (App Store version current on 2026-11-04), via Exchange ActiveSync 14.1. The Microsoft cloud service that connects to DAVbridge identifies itself as `Outlook-iOS-Android/1.0`. |
| CalDAV server | Nextcloud server 34.0.5 (sabre/dav) |
| Outlook for Android | **Not tested.** It uses the same Microsoft service and may behave the same, but this is unverified. |

Verified: setup, synchronization of an existing calendar, create / edit / delete of single events. Other versions may work but are **untested**.

Outlook mobile is updated frequently and Microsoft's cloud may change at any time. Only Exchange ActiveSync **14.1** is
offered; if a future app version insists on a newer protocol version, it will stop working.

## Operator checklist

- `protocols.eas.enabled = true`, `acknowledge_mobile_cloud_processing = true`, and `eas_enabled = true` for the user.
- DAVbridge is reachable from the **public internet** with a publicly trusted certificate, no IP allow-list, no SSO, VPN or
  captcha in front, and the proxy settings from [reverse-proxy.md](../reverse-proxy.md) (Ping needs >= 650 s timeouts).
- Create a **separate app password per device** (for example `davbridge-mobile`) on the CalDAV server.

## Add the account

1. Open Outlook -> **Settings -> Add account -> Add email account**.
2. Enter the account address from the configuration, for example `alice@dav.example.com`, and tap **Continue**.
3. If asked for the provider, choose **Exchange**.
4. Enter the **app password** - not your normal CalDAV/Nextcloud login password.
5. If sign-in fails, open **Advanced settings** (or *Set up manually*) and enter:
   - **Server:** `dav.example.com` (host name only)
   - **Domain:** leave empty
   - **User name:** `alice`
   - **Password:** the app password
6. Wait for the first synchronization. The first sync of a large calendar issues many requests and can take several minutes.

If setup loops or fails, see [troubleshooting.md](../troubleshooting.md#outlook-mobile-setup-never-finishes).

## What to expect

- **Calendar:** your events appear. Create, edit and delete **single** events: subject, location, time, all-day, notes,
  busy status (free/tentative/busy/out of office), reminder, categories, sensitivity.
- **Mail, Contacts, Files:** empty. The inbox view (including Focused) stays empty and sending mail is rejected - this is
  normal; DAVbridge is calendar-only. Do not use this account for mail.
- **Recurring events:** shown if they map exactly to EAS 14.1, but **read-only**. Series that cannot be mapped exactly are
  hidden. Do not create recurring events or invitations on the phone.
- **Events with attendees/organizer** (created elsewhere): read-only.
- **Conflicts:** if the event changed on the server meanwhile, the server version wins and you see it after the next sync.
- **Mass deletions** are slowed by the delete brake and may take hours to complete.
- **Timing:** updates arrive via Microsoft's service on its own schedule; there is no guaranteed latency. After DAVbridge was
  restarted, pull to refresh the calendar once.

## Remove the account

1. Remove the account in the app.
2. Revoke the app password on the CalDAV server. Access stops at the latest after `auth.positive_cache_seconds` (default 300 s; see [privacy.md](../privacy.md#revocation-and-removal)); restart DAVbridge to end it at once.
3. Ask the operator to run `davbridge reset devices --user alice` to drop the device record.
4. If you want the copy at Microsoft removed, use Microsoft's account and privacy tools; DAVbridge cannot do it.
