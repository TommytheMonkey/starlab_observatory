# Adding supported hardware

## A tracker board

1. Add an entry to `HARDWARE` in `src/aobs/stratolink/profiles.py`:
   * `hw_models`: the Meshtastic `HardwareModel` names the firmware reports (`meshtastic.HW_MODELS` lists them all);
   * `usb_ids`: `vid:pid` of its USB serial port (a hint for the tool only; identity always comes from the firmware);
   * `max_tx_dbm`, `min_firmware`, `firmware_target` (the web flasher's name), `gps`;
   * `power`: awake / light-sleep / GPS / RX current and a TX-current table by dBm, with `source` saying whether they
     are measured or nominal;
   * `status`: `experimental` → `planned` → `supported` as it is bench-tested and flown;
   * `notes`: anything a flight depends on (GNSS altitude limits, power saving quirks).
2. Add `overrides` for it in the profiles that need them (e.g. a lower TX power).
3. Run `tests/test_stratolink.py` and add a case to `test_profiles_validate_and_estimate`.
4. Provision one with a Ground Test bundle and check the report: every setting must read back (`mismatches` empty).
   Measure current draw and replace nominal power figures.

The provisioning tool needs no change for a board whose firmware exposes the same config fields; a setting the
firmware lacks is reported as a warning rather than applied.

## A ground-station gateway

Only the SenseCAP M2 US915 (`sensecap,wm7628n`) is supported. For another SX1302/SX1303 gateway:

1. Build its packet forwarder with `deploy/stratolink/firmware/patches/0002-meshtastic-syncword.patch` (the same HAL
   function exists in sx1302_hal 2.x).
2. Make a radio template with its SPI path, RSSI offset and `chan_Lora_std` at 906.875 MHz / 250 kHz / SF11,
   `lorawan_public: false`, `tx_enable: false`.
3. Extend `gwprov.Gateway.inspect` with its board name and region source, and `deploy` with its init system if it is
   not OpenWrt procd.
4. Archive its firmware as a new release directory with `RELEASE.json` (the format of the existing one).

SX1301-based gateways cannot receive 250 kHz on their single-SF channel with a custom sync word in the same way and
are not candidates without further work.
