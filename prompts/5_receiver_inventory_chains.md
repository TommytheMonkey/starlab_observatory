Implement receiver inventory and RF signal-chain management
for Starlab Observatory.

OBJECTIVE

Starlab must understand not only which SDR receivers exist,
but also what antennas, amplifiers, filters, and accessories
are connected to them.

DATA MODELS

Implement configurable inventory records for:

- SDR receivers
- Antennas
- LNAs
- Bandpass filters
- Notch filters
- Attenuators
- Splitters
- RF switches
- Rotators
- Cables and connectors where useful

ANTENNA PROPERTIES

Support:

- Name
- Manufacturer
- Model
- Antenna type
- Frequency range
- Polarization
- Mounting location
- Orientation
- Fixed or rotatable
- Description
- Notes

RECEIVER PROPERTIES

Support:

- Hardware identity
- Host
- Friendly name
- Supported frequencies
- Sample rates
- Gain profiles
- Bias tee settings
- Recording formats
- Notes

SIGNAL CHAINS

Allow users to define connections such as:

Antenna → Filter → LNA → Receiver

Support more complex paths with splitters and switches.

Display a visual representation of the signal chain.

VALIDATION

Warn when:

- Satellite frequency is outside configured antenna range.
- Receiver does not support requested frequency.
- Sample rate is unsupported.
- Bias tee configuration may be incompatible.
- Required external amplifier power is unavailable.
- Multiple receivers are assigned an impossible exclusive path.

Do not infer physical connections without user confirmation.

CONFIGURATION

All inventory and signal-chain settings must persist.

Provide explicit Save and Cancel controls.

DELIVERABLE

Receiver inventory, antenna inventory, signal-chain editor,
validation, and persistent configuration.