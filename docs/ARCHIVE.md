# Recording archive, decoding and retention

Brief 8. One place to browse recordings, see what each one achieved, decode them again, and control how long
their raw IQ is kept and where it lives.

Open it with **Recordings** in the page header. It has three tabs: **Library**, **Decoder profiles** and
**Storage & retention**.

## Library

Every recording that produced a file is listed: manual, automatic (rule or legacy planner), imported, signal-path
tests, and the legacy recorder's files. Failed recordings that kept a file are listed too.

Each row shows:
- satellite or target;
- receiver and recording host (`local`, or the receiver host it was recorded on);
- centre frequency and sample rate;
- start and end in station time, and duration;
- file format and size;
- the recording status;
- the four outcomes (below).

Filters:
- free-text search (satellite, recording id, job name, receiver);
- receiver;
- host;
- outcome reached;
- where the raw IQ is (central storage, a receiver host, deleted);
- recording status;
- date range.

The list can be ordered newest first, oldest first or largest first. Dates without a time zone are station time
(Settings → Observatory).

Click a row for its page:
- the four outcomes with their evidence;
- images and plots (SatDump images, waterfall, spectrum, power over time);
- every recording field;
- storage and retention;
- the decode attempts;
- every file, with a viewer for logs and JSON and downloads;
- the processing history: state changes, attempts, transfers and deletions, newest first.

Files over 100 MB, and all raw IQ, download through a signed link that expires after an hour. Nothing large
passes through the page.

## Outcomes

These are separate outcomes, each with its own evidence. A recording is never called decoded because its capture
succeeded.

| Outcome | Shown as | Evidence |
|---|---|---|
| Recording | recorded / partial / failed | The file passed output validation (size, samples, duration, recorder exit, device errors, IQ health). A partial recording (stopped on request) keeps its file. |
| Signal | detected / not detected / unknown | The decoder locked (CADU frames or SYNCED reports), **or** an analysis measured the target frequency's *time-averaged* power at least 3 dB over the noise floor. Peak hold is not used: over a whole pass, noise alone peaks about 10 dB up. A spectral detection is a candidate; the analysis does not identify the transmitter. |
| Frames decoded | count | CADU frames from the best decode attempt (1024 bytes per frame). |
| Image generated | count | Images with real content. A blank or unreadable image does not count. |

**A failed decode never fails the recording.** The job stays COMPLETED; the decode attempt is FAILED with its
reason ("no CADU frames: the decoder never achieved frame sync"). If the decode cannot even start (for example,
the target channel does not fit inside the recorded band), the recording's analysis still runs. The decode outcome
then says "not started", with the reason.

The "Outcome reached" filter uses the highest level reached: image > frames > signal > recorded. A failed recording
is always listed as failed.

## Decoding and decoder profiles

A decoder profile names:
- a SatDump pipeline;
- the satellites (NORAD IDs) and/or transmission modes it applies to (`LRPT`, `APT`, …; a recording's mode is the
  first word of the satellite catalog's mode);
- optional settings: target frequency, shift method, fine-search offsets and extra SatDump parameters.

Profiles are checked with the same rules as a decode request, so a profile can never ask for something the
processor would refuse. A pipeline SatDump does not have is a warning: the profile is kept but not used until the
pipeline is installed.

Choosing a profile:
- a decode without a pipeline (the **Decode** button, `/radio-decode`, or `kind=decode` in the API) uses the best
  matching profile: enabled, installed, a satellite match before a mode match, then the higher priority;
- `profile_id` picks one explicitly;
- settings in the request itself always win over the profile's.

Every attempt records the profile's id and revision.

**Decode automatically** on a profile makes decoding the default for new rules and bookings of its satellites. A
rule or booking still decides for itself. Changing a profile never rewrites what is already booked. Rules that
decode without naming a pipeline use the matching profile's pipeline. A satellite with no built-in decoder (for
example the ISS) can be decoded once a profile names it.

Profiles keep every revision (**History**). Saving over a newer revision is refused ("changed elsewhere"). Deleting
a profile keeps its history.

On first start, Starlab creates one profile from its satellite catalog: *METEOR-M2 LRPT* (`meteor_m2-x_lrpt`,
METEOR-M2 3 and 4, mode LRPT). It is not re-created after you delete it. METEOR decodes still search ±6 kHz in
1 kHz steps unless a profile or request says otherwise.

Every decode is a new attempt in its own folder, run at the lowest CPU and disk priority, and paused while any
receiver records. Earlier attempts and their files are never overwritten.

## Storage

- **Folders.** Settings → Storage: manual recordings, automatic recordings, signal-path tests, and decodes or
  analyses of legacy and imported recordings (restart to change the last one). Other recordings keep their
  results in a `processing/` folder next to the recording.
- **Usage.** The Storage tab shows:
  - free and total space for each disk that holds a storage folder;
  - what the archive holds, by kind of file;
  - raw IQ by origin;
  - raw IQ still on receiver hosts.

### Receiver hosts: keep or transfer

Settings → Receiver hosts → **Recordings**:

- **Copy here after each recording** (default, as in brief 7): the file is copied, verified by size and SHA-256,
  then removed from the host.
- **Keep on the host**: nothing crosses the network. When the recording ends, its size and SHA-256 are recorded
  and the job completes as "kept on <host>". Nothing is analysed or decoded automatically, because that needs the
  file here; the IQ health check is skipped for the same reason.
  - **Transfer** (or **Transfer, then decode / analyse**) on the recording's page queues a transfer attempt. It
    streams the file over SSH, verifies size and SHA-256 against what the host reported, moves it into place, and
    then removes the host's copy.
  - A transfer is queued, paused while recording, and logged like any other attempt. Decodes queued behind it run
    once it has finished.

## IQ health

Every completed recording gets an IQ health check, and its result is shown on the recording's page and kept in
`<raw>.health.json`.
- **RTL-SDR CU8 recordings** get the detailed check (docs/RTL_SDR.md). It is the only one that can fail a
  recording, and the only one that alerts (stuck dongle, capture failure).
- **Every other recording** (SDRplay, Airspy, simulated, cs8/cs16/cf32) gets a **basic check**: RMS level,
  clipping, all-zero or frozen samples, and DC offset, read from evenly spaced blocks. It is informational only:
  it never fails a recording and sends no notification.
- A recording kept on a receiver host is checked when it is transferred.

## Retention

Retention is the only code that deletes recordings, and it deletes only **raw IQ**. Logs, plots, images, decoded
products and the metadata sidecar are always kept.

Which policy applies to a recording (first match wins):

1. **Keep raw IQ** on the recording's page → never deleted.
2. Imported files and the legacy recorder's files → never deleted by Starlab. The legacy recorder prunes its own.
3. The booking said *retain raw IQ: no* → deleted once a decode produced an image.
4. Booked by a rule → the rule's own policy, as it was when the pass was booked.
5. The class default in Settings → Storage:

| Class | Default | Choices |
|---|---|---|
| Automatic recordings | 48 hours | keep, hours, after_decode |
| Manual recordings | keep | keep, hours, after_decode |
| Signal-path tests | keep | keep, hours |

These defaults are what Starlab already did before brief 8.

`hours` counts from the end of the recording. `after_decode` waits for an image with content; a decode that found
nothing is not enough.

Nothing is deleted when any of these holds:
- the recording is still recording or processing;
- an attempt on it is queued or running;
- the file is outside the allowed output folders;
- **Delete raw IQ by the policies below** is off. The Storage tab then still lists what would be deleted.

Retention runs every five minutes, after each rule planning cycle, and right after a recording's post-processing
(for *retain raw IQ: no*). The Storage tab shows:
- what is due now, with **Delete these now**;
- what is due in the next 7 days;
- what is waiting, and why.

Every deletion is recorded:
- on the recording, in `archive.deletions` and the processing history;
- on the file's index row (`deleted_at`, `deleted_reason`).

Raw IQ kept on a receiver host is deleted there over SSH under the same policy. If the host cannot be reached,
the deletion is retried on the next pass.

## API

Owner session (the page) or the broker bearer token:

| Method | Path | |
|---|---|---|
| GET | `/v1/archive/recordings` | Library. Query: `q, receiver, satellite, host, status, outcome, where, source, since, until, format, sort, page, per_page` |
| GET | `/v1/archive/recordings/{id}` | One recording: outcomes, files, attempts, history, retention, profiles |
| POST | `/v1/archive/recordings/{id}/process` | `{kind: decode\|analyze\|transfer, params, transfer_first}` |
| POST | `/v1/archive/recordings/{id}/keep` | `{keep: true\|false}` |
| POST | `/v1/archive/attempts/{id}/cancel` | Cancel a queued attempt |
| GET | `/v1/archive/files/{artifact_id}` | A file (images inline; `?download=1` to save) |
| GET | `/v1/archive/files/{artifact_id}/text` | Log/JSON text: `tail_lines`, `contains` |
| POST | `/v1/archive/files/{artifact_id}/link` | Signed download link, valid for 1 hour |
| GET | `/v1/archive/storage` | Disk usage, archive contents, retention plan |
| POST | `/v1/archive/retention/apply` | Delete what is due now |
| GET/POST | `/v1/archive/profiles` | List / create decoder profiles |
| PUT/DELETE | `/v1/archive/profiles/{id}` | Update (with `revision`) / delete |
| GET | `/v1/archive/profiles/{id}/history` | Every revision |

The existing `/v1/catalog/*` and `/v1/recordings/*` routes used by the Slack agent are unchanged. Catalog rows now
also carry `outcome`, `location`, `host`, `end` and `keep`.

## Schema 8

The migration only adds:
- the tables `decoder_profiles` and `decoder_profile_versions`;
- the nullable columns `sched_jobs.archive`, `artifacts.host` and `artifacts.deleted_reason`.

Existing recordings, attempts and files are untouched.

## Limits

- Signal detection from the spectrum is a candidate, not an identification.
- Recordings kept on a host get no automatic analysis, decode or IQ health check until they are transferred.
- The basic IQ check is a sanity check (level, clipping, frozen samples), not calibrated per receiver like the RTL-SDR one.
- Retention removes raw IQ only. Decoded products and plots are kept indefinitely; remove them by hand if needed.
- Remote keep, transfer and remote deletion have been tested only against a throwaway sshd on 127.0.0.1 with a
  simulated receiver, not on a real remote host.
