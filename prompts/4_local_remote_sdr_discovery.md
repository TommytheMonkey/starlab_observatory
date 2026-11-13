Implement automatic SDR hardware discovery for Starlab.

OBJECTIVE

Users must be able to discover and configure SDR receivers
connected locally or to remote Linux hosts over SSH.

ARCHITECTURE

Create a hardware discovery abstraction that works identically
for local and remote hosts.

Each discovered receiver must have a stable identity.

SUPPORTED HARDWARE

Implement discovery adapters for available hardware tools:

RTL-SDR:
- rtl_test
- rtl_eeprom
- USB enumeration

Airspy:
- airspy_info

SDRplay:
- SDRplay API
- SoapySDR where available

HackRF:
- hackrf_info

Additional hardware:
- PlutoSDR
- LimeSDR
- USRP
- Generic SoapySDR devices

Only report capabilities actually exposed by installed drivers.

Do not fabricate unsupported capabilities.

REMOTE DISCOVERY

Allow users to register remote Linux hosts using:

- Hostname or IP
- SSH port
- Username
- SSH key authentication

Test connectivity before saving.

Implement host key verification.

Do not disable SSH host key checking.

Do not store plaintext SSH passwords.

DISCOVERY RESULTS

For each receiver, populate where available:

- Manufacturer
- Model
- Serial number
- USB vendor/product ID
- Driver
- Firmware version
- Supported frequency range
- Supported sample rates
- Gain controls
- Bias tee support
- Hostname
- Connection type
- Current availability

DEVICE IDENTITY

Use hardware serial numbers when reliable.

Do not rely exclusively on USB device indexes.

Handle duplicate serial numbers and devices without serials.

Allow users to assign friendly names.

INTERFACE

Add:

Settings → Receiver Hosts

Settings → Receivers

Provide:

- Discover Devices
- Refresh
- Test Connection
- Identify Device
- Configure
- Enable/Disable

Do not claim that passive antennas or filters can be
automatically identified through USB.

TESTING

Use mocked command output for hardware not physically available.

Ensure discovery cannot interrupt active recordings.

DELIVERABLE

Working local and remote receiver discovery integrated
with persistent configuration.