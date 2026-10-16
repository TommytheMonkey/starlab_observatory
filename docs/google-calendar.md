# Google Calendar

Starlab can put every booked recording on a Google calendar and keep the events up to date as plans change
(rescheduled, cancelled, completed, decoded). It's optional: the schedule lives in Starlab's database, and the
calendar is only a copy of it.

You set it up from the web page, in the setup wizard's **Integrations** step or later in **Settings → Integrations**.
No files to copy onto the server, no configuration file to edit, no restart.

## What you need

- A Google account with the calendar you want to use. A calendar just for Starlab is a good idea: in Google
  Calendar, *Other calendars → + → Create new calendar*.
- A **Google Cloud service account** with a **JSON key**. It's a robot account that only reaches calendars you
  share with it. Creating one is free.

## Set it up

1. **Create the service account** (one time):
   1. Open [Google Cloud](https://console.cloud.google.com/), and pick or create a project.
   2. [Enable the Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
      for that project.
   3. Go to [Service accounts](https://console.cloud.google.com/iam-admin/serviceaccounts) → *Create service
      account*. Give it a name such as `starlab`. It needs no roles: skip those steps.
   4. Open the new account → *Keys* → *Add key* → *Create new key* → *JSON*. Your browser downloads a key file.
2. **Upload the key** in Starlab's *Google Calendar* card. Starlab checks that it really is a service-account key,
   stores it in its secrets folder (readable only by Starlab), and shows the account's e-mail address
   (`…@….iam.gserviceaccount.com`). The key itself is never shown again, logged or exported.
3. **Share the calendar** with that e-mail address: in Google Calendar open the calendar's *Settings and sharing*
   → *Share with specific people or groups* → add the address with **Make changes to events**.
4. **Enter the Calendar ID**: the same *Settings and sharing* page → *Integrate calendar* → *Calendar ID*
   (`…@group.calendar.google.com`; for your main calendar it's your Gmail address).
5. **Test connection.** Starlab reads the calendar, adds a test event dated 1 January 2000 and removes it again.
6. **Turn on.** Starlab runs the same test first and turns publishing on only if it passes. It takes effect at
   once; booked recordings appear on the calendar within a few minutes.

To stop: **Turn off**. Events already on the calendar stay there. **Remove key** deletes the key file (turn the
calendar off first).

## When the test fails

| Message | What to do |
|---|---|
| "Google found no calendar with that ID that this account can see" | Check the Calendar ID, and that the calendar is shared with the service account's address. |
| "Starlab can see the calendar but may not add events" | The share is read-only. Change it to **Make changes to events**. |
| The share stays read-only, or *Make changes to events* is greyed out | Your **Google Workspace** blocks sharing calendars with accounts outside the organization, and a service account counts as outside. A Workspace administrator can allow external sharing with edit rights (Admin console → Apps → Google Workspace → Calendar → Sharing settings), or use a calendar in a personal Google account. |
| "Google refused the key" | The key was deleted or disabled in Google Cloud, or the Calendar API isn't enabled for its project. Create a new key and upload it. |
| "could not reach Google Calendar" | The Starlab computer has no internet connection, or Google is unreachable. Try again later. |

If syncing fails later (for example the share was removed), the card shows the last sync error, Starlab sends one
"Google Calendar sync is failing" notification, and recording carries on unaffected.

## Advanced: credentials in the configuration file

A station can instead name its credentials in `starlab.toml`. The web page then uses those and doesn't offer the
key upload:

```toml
[calendar]
auth_mode = "service_account"
service_account_file = "/srv/keys/google-calendar.json"   # mode 600, readable by the Starlab account
```

`auth_mode = "oauth"` (signing in with a Google account instead of a service account) is supported for stations
that already use it, but it is set up on the command line (`starlab calendar auth`) and can only create its own
calendar. Connecting a Google account from the web page isn't available yet; it will need Starlab on HTTPS (for
example its Tailscale HTTPS address).

## API (owner session or broker bearer token)

| Method | Path | |
|---|---|---|
| GET | `/v1/integrations` | Google Calendar and Slack: on/off and running |
| GET | `/v1/integrations/calendar` | state, calendar ID, the key's e-mail address (never the key), last sync |
| PUT | `/v1/integrations/calendar/key` | `{key_json}`: the key file's text |
| DELETE | `/v1/integrations/calendar/key` | removes the key (refused while the calendar is on) |
| POST | `/v1/integrations/calendar/test` | `{calendar_id}`: the checks above |
| POST | `/v1/integrations/calendar/enable` | `{calendar_id}`: tests, then turns publishing on |
| POST | `/v1/integrations/calendar/disable` | turns publishing off |
