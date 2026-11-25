Implement a unified recording and decoding workflow
for Starlab Observatory.

OBJECTIVE

Users should be able to browse recordings, inspect metadata,
run supported decoders, and manage storage.

RECORDING LIBRARY

Display:

- Satellite
- Receiver
- Recording host
- Frequency
- Sample rate
- Start/end time
- Duration
- File format
- File size
- Decode status
- Recording status

Allow filtering and searching.

DECODING

Integrate with existing SatDump functionality.

Support configurable decoder profiles.

Associate profiles with satellites and transmission modes.

Allow automatic decoding after successful recording.

Allow manual reprocessing.

Preserve decoder logs.

Distinguish:

- Recording successful
- Signal detected
- Frames decoded
- Image generated

These are separate outcomes.

Never mark a satellite transmission as successfully decoded
merely because IQ capture succeeded.

STORAGE

Support configurable storage directories.

Allow recordings to remain on remote hosts.

Avoid unnecessary transfer of multi-gigabyte IQ files.

Implement optional transfer to central storage.

Implement configurable retention policies.

Never delete files without applying the user's
configured retention policy.

ARCHIVE

Provide:

- Recording details
- Generated images
- Decoder logs
- Download links
- Processing history
- Storage usage

TESTING

Verify decoding failures do not incorrectly mark
recordings as failed.

Verify partial recordings are handled correctly.

DELIVERABLE

Integrated recording archive and processing workflow.