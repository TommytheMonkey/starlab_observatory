# Slack integration (optional)

Starlab works fully without Slack. The integration is off until you switch it on, and while it is off nothing of it
runs.

It does two things:
- posts notifications to one channel;
- answers a `/starlab` slash command.

It does not use a language model. The conversational observatory bot (`agent/obsagent`) is a separate program with
its own Slack app; this integration does not replace or change it.

## Set it up

Everything is in **Settings**:

1. **Slack** settings card. Choose:
   - **Enable Slack integration**;
   - the **Connection**:
     - *socket* (recommended): Starlab connects out to Slack, so no public address, port forwarding or certificate
       is needed;
     - *http*: Slack calls a public HTTPS address of this server, `https://<address>/slack/commands`;
   - the **channel ID** for notifications;
   - which **notifications** you want;
   - the **slash command**, and whether **record/cancel** are allowed and for which Slack user IDs.

   Then **Save**.
2. **Slack setup** card → **Generate the app manifest**, then **Copy** or **Download** it. In Slack, go to
   <https://api.slack.com/apps?new_app=1> → *From an app manifest* → choose the workspace → paste it → *Create*.
   The manifest asks only for the permissions your choices need:

   | Scope | Needed for |
   |---|---|
   | `chat:write` | any notification (and command replies in HTTP mode) |
   | `files:write` | decoded image notifications (the images are uploaded) |
   | `commands` | the slash command |

3. In the Slack app:
   - **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`, OAuth & Permissions).
   - Socket Mode: Basic Information → App-Level Tokens → *Generate* with scope `connections:write`, and copy the
     **App-Level Token** (`xapp-…`).
   - HTTP mode: copy the **Signing Secret** (Basic Information).
4. Paste the credentials into the Slack setup card and **Save credentials**. In Slack, invite the app to the
   channel: `/invite @starlab`.
5. **Test connection**. It checks:
   - the bot token, and the workspace it belongs to;
   - that the granted scopes match the manifest;
   - that the channel exists and the app is in it;
   - the App-Level Token (Socket Mode), or the Signing Secret (HTTP mode).

   **Send a test message** posts one line to the channel.
6. **Apply** starts the integration. No broker restart is needed. After changing settings later: Save, then Apply.

To turn it off: **Disable Slack** (or untick *Enable* and Apply). Credentials are kept until you **Remove** them.

Socket Mode needs the optional Python package `slack_sdk`. The installer adds it when you choose Slack, or run
`pip install 'starlab[slack]'` in Starlab's environment. Notifications and HTTP mode need nothing extra.

## Credentials

- Stored in `<secrets>/slack.json`, mode 0600, in a 0700 folder, written atomically.
- Write-only: the page and the API only ever say whether each one is set.
- Never logged: only the names of changed credentials are logged, never values. Tokens travel only in the
  `Authorization` header to slack.com.
- Never exported: the configuration export contains settings documents only. The Slack settings document holds no
  secrets, and the export drops anything that looks like a secret anyway.
- Checked when saved (`xoxb-…`, `xapp-…`, a 32-hex signing secret), so a pasted-in wrong value is refused
  immediately.

## Notifications

All go to the configured channel. Each has its own switch:

| Notification | When |
|---|---|
| Upcoming passes | a booked recording starts within *N* minutes (default 15), once per recording |
| Recording started | a recording begins |
| Recording completed | it completed (with the reason: GB recorded, decode result) |
| Recording failed | it failed, was missed, or ended partial (with the reason) |
| Receiver offline | a receiver stops being seen, and again when it is back |
| Storage low | a storage disk is below 1.5 × the space Starlab keeps free (at most every 6 hours) |
| Decoded images | a decode produced an image: a line with the frame count, plus up to three images |

- They come from Starlab's own records: job state changes, decode attempts, presence and disk space.
- Signal-path tests are never announced.
- On first start the cursors are set to "now", so old events are never replayed.
- Starlab sends at most 30 messages a minute, and honours Slack's rate limit (`Retry-After`).

## Slash command

`/starlab` (the name is configurable). Replies are visible only to the person who asked.

| Command | |
|---|---|
| `/starlab status` | receivers, storage, what is recording, what is next, the last failure |
| `/starlab passes [hours]` | predicted passes (24 h by default, up to 72) |
| `/starlab receivers` | every receiver: connected, mode, antenna, last recording |
| `/starlab recordings [n]` | the latest recordings with their outcomes (recorded, signal, frames, images) |
| `/starlab record <satellite> [receiver]` | book the next pass that starts more than 3 minutes from now |
| `/starlab cancel <job id>` | cancel a booked recording that has not started |
| `/starlab help` | the list |

**record** and **cancel** are off unless *Allow record and cancel from Slack* is on. Even then, only the Slack user
IDs under *Authorized users* may use them; anyone else is told their ID so an owner can add it. Every user is
limited to *Commands per user per minute* (default 20).

## Security

- **HTTP mode:** every request to `/slack/commands` must carry a valid Slack signature (HMAC-SHA256 with the Signing
  Secret) with a timestamp within 5 minutes. A missing, wrong or replayed signature gets 401 and nothing runs.
  Bodies over 64 kB are refused. The endpoint answers 404 unless HTTP mode is enabled.
- **Socket Mode:** Starlab opens the connection, authenticated with the App-Level Token, so nothing listens for
  Slack.
- **Privileged commands:** an explicit switch plus an allow-list of Slack user IDs.
- **Errors:** a Slack outage or error never affects recording. It is shown on the Slack setup card ("last error"),
  and notifications resume on the next pass.

## API (owner session or broker bearer token)

| Method | Path | |
|---|---|---|
| GET | `/v1/slack` | settings, which credentials are set, running state, last error |
| GET | `/v1/slack/manifest` | the manifest (`?download=1` as a file) |
| PUT | `/v1/slack/credentials` | `{bot_token, signing_secret, app_token}`: a value sets it, `""` keeps it, `null` removes it |
| POST | `/v1/slack/test` | connection checks (`{post_message: true}` also posts a test line) |
| POST | `/v1/slack/apply` | start, restart or stop with the saved settings |
| POST | `/slack/commands` | Slack's slash-command requests (HTTP mode; signature required) |
| GET/PUT | `/v1/config/slack` | the Slack settings document (versioned, like every settings document) |

## This station

The observatory bot on monkeybrain keeps its own app, tokens and behaviour, and it reads the notification outbox as
before. If you also enable this integration in the same channel, both will post. Use one or the other per channel.
