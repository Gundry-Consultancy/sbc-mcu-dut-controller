# Device identity discovery

This guide separates physical identity from compatibility and connection location.
It summarizes public sensor identity evidence reviewed on 2026-10-11 and describes
requirements for future model-specific readers. The matrix is documentation of
device interfaces, not a claim that every reader is implemented or that any
particular sensor has been reached.

## What identifies a twin

A controller-assigned asset UUID identifies an inventory record. A manufacturer
serial, when available and verified, helps bind that record to a physical unit.
Keep both. Board family, architecture, FQBN, firmware version, I2C address, mux
channel and USB port describe compatibility or attachment; they do not uniquely
identify a replaceable physical unit.

Record identifiers with their namespace, subject, source and confidence. A sensor
IC, its breakout board, a USB-to-UART bridge, a debugger and the MCU behind that
bridge can be different assets. A chip serial must not silently become proof of
the identity of an entire assembly. Associate them explicitly.

A replacement receives a new twin and identity observations, even when it inherits
the old unit's role and compatible capabilities. Preserve the old record and
replacement relationship. For hardware without a documented readable serial,
use an assigned asset tag and an explicit operator binding; an unchanged port
does not establish that the physical unit is unchanged.

## Discovery outcomes

Use independent fields for documentation support and the result of an observation:

| Documentation category | Meaning |
| --- | --- |
| Unique immutable | The manufacturer documents a unique, immutable or factory hard-coded unit identifier. |
| Documented unique or serial | A manufacturer interface returns a unit identity, but this review does not establish immutability for every implementation or firmware version. |
| Mutable factory identity | Factory-programmed identity exists in user-writable storage; provenance and overwrite risk must remain visible. |
| Not documented | The reviewed public interface does not establish a readable unit identity. This is not proof that no internal production identifier exists. |
| Unknown | Evidence is incomplete or the exact variant has not been established. |

An observation can separately be successful, unreachable, unsupported by the
installed reader, invalid (for example a CRC failure), ambiguous, or blocked by
an unavailable route or lease. A failed read must not be converted into
"no serial". Preserve an earlier verified identity when a later discovery fails.

Reject empty or malformed values and retain raw response evidence where useful.
Repeated or placeholder USB serial strings need collision handling, not automatic
merging. Calibration coefficients may be useful diagnostic fingerprints, but
without a documented uniqueness guarantee they are not hardware serial numbers.

## Implemented model-specific readers

`hil_controller.twin_sensor_readers.read_sensor_identity` provides a synchronous,
injected-transport library facility. It is not a live scan endpoint or automatic
job stage. The separate Linux/S31 inventory collector remains responsible for its
own observation collection; these sensor recipes do not invoke that collector.

The implemented recipes cover SGP30/40/41, HDC3020/3021/3022 (also selected as
HDC302x), STCC4 and TMP119. The caller supplies an exact model, explicit 7-bit
address, an `IdentityBus` implementation, and `SensorReadContext` attestations
that the route is verified, its lease is held and the sensor is awake and idle
after required startup timing. These assertions do not acquire a lease or inspect
physical routing; the integrating controller must enforce those conditions.

The raw transport distinguishes separate write/read transactions from a combined
write/read with repeated START. It must not add SMBus length bytes, swap bytes or
strip CRCs. Its wait operation must meet the requested minimum delay, and I/O must
have bounded timeouts. The reader uses documented command/read transactions only;
it provides no scan, mux, reset, heater-control or EEPROM-programming operation.

| Reader | Implemented transaction |
| --- | --- |
| SGP30 | Serial command, at least 0.5 ms wait, 9-byte response. |
| SGP40 / SGP41 | Serial command in idle, at least 1 ms wait, 9-byte response. |
| HDC302x | Three combined command/read transactions, each returning two identity bytes and CRC. |
| STCC4 | Product/serial command, at least 1 ms wait, 18-byte response; validate product ID and separate it from the 64-bit serial. |
| TMP119 | Combined pointer/read transactions for EEPROM1/2/3, two bytes each; preserve raw words with `unique=false` and reported confidence. |
| SCD30 | Explicitly unsupported by this reader pending review of raw transport framing and firmware/state prerequisites. The documented manufacturer API remains separate evidence of serial support. |

Every CRC-bearing word must validate before an identity is returned. Wrong lengths,
wrong STCC4 product identity, CRC errors and empty or placeholder identities
produce `invalid_response`; transport errors produce `read_failed`. Unknown
models produce `unsupported`, and unmet context/address checks produce
`blocked`, without transport access. A failure never returns a partial identity
or attempts a different model/address.

The typed result exposes `identities` using the existing Observation identity
contract, plus raw exchanges and per-word CRC results.
`to_observation_attributes()` returns JSON-safe evidence, including hexadecimal
raw responses, status and recipe provenance. Integrators supply the Observation
ID, timestamp, source and verified route provenance, then reconcile explicitly.
A successful read identifies the selected sensor IC; it does not establish the
identity of a breakout assembly or authenticate the hardware cryptographically.

Protocol tests use a fake bus and recorded command/response fixtures. No physical
sensor has been validated by those tests.

## Sensor evidence matrix

The links below are manufacturer documentation or official driver material.
"Not documented" is limited to the sources reviewed. None of the model IDs below
should be stored as per-unit serials.

| Sensor | Category | Identity evidence and scope |
| --- | --- | --- |
| SGP30 | Documented unique | A 48-bit serial is returned as three big-endian words, each with CRC. See [Sensirion datasheet, section 6.5](https://sensirion.com/file/datasheet_sgp30/). |
| SGP40 | Unique immutable | The datasheet explicitly describes a unique serial hard-coded during production. Three words plus per-word CRC; read in idle. Use the later datasheet rather than the older transition guide that omitted this facility. See [Sensirion datasheet, sections 3.4 and 4](https://sensirion.com/media/documents/296373BB/6203C5DF/Sensirion_Gas_Sensors_Datasheet_SGP40.pdf). |
| SGP41 | Documented unique | A serial is returned as three words plus per-word CRC. Observe the documented idle-state and timing rules. See [Sensirion datasheet](https://sensirion.com/resource/datasheet/sgp41). |
| HDC302x | Unique immutable | TI documents an unchangeable 48-bit NIST traceability ID. Three reads return successive two-byte portions, each with CRC. The manufacturer ID is separate. See [TI datasheet, section 7.5.7.9](https://www.ti.com/lit/ds/symlink/hdc3020.pdf). |
| STCC4 | Documented unique | Product identification returns a 32-bit product ID followed by a 64-bit unique serial, with CRC on each word. Split the fields; do not use the product ID as serial. See [Sensirion datasheet, section 3.4.16](https://sensirion.com/media/documents/6AED4B15/684A889C/CD_DS_STCC4_D1_3.pdf) and [official driver API](https://sensirion.github.io/python-i2c-stcc4/api.html). |
| TMP119 | Mutable factory identity | TI documents a factory unique NIST identity in user-writable EEPROM. Preserve raw EEPROM1, EEPROM2 and EEPROM3 values; never unlock or program them during discovery. The prose references four locations while the register map lists three general-purpose registers, so do not invent a fourth address. Model/revision register 0x0F is not a serial. See [TI datasheet, sections 7.5.1 and 8.5](https://www.ti.com/lit/ds/symlink/tmp119.pdf). |
| SCD30 | Documented serial | The current manufacturer driver includes a null-terminated ASCII serial read with up to 32 payload bytes. Verify support on the installed firmware and use the documented transport framing. See [Sensirion driver](https://raw.githubusercontent.com/Sensirion/python-i2c-scd30/master/sensirion_i2c_scd30/commands.py). |
| APDS9999 | Not documented in reviewed driver | The official breakout driver exposes PART_ID at 0x06 as part identification, not unit identity. See [Adafruit driver definitions](https://adafruit.github.io/Adafruit_APDS9999/html/_adafruit___a_p_d_s9999_8h_source.html). |
| AS7331 | Not documented | AGEN describes device/API generation, not unit identity. Its register address has a different meaning in measurement state. See [ams OSRAM datasheet, section 8.2.2](https://look.ams-osram.com/m/1856fd2c69c35605/original/AS7331-Spectral-UVA-B-C-Sensor.pdf). |
| BME280 | Not documented | Register 0xD0 contains model ID 0x60. Individual calibration values have no documented uniqueness guarantee. See [Bosch datasheet, section 5.4.1](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf). |
| BMP280 | Not documented | Register 0xD0 contains model ID 0x58. Calibration values are not guaranteed serials. See [Bosch datasheet, section 4.3.1](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmp280-ds001.pdf). |
| BMP388 | Not documented | CHIP_ID at 0x00 identifies the model; no public per-unit serial was established. See [Bosch datasheet, section 4.3.1](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmp388-ds001.pdf). |
| BMP581 | Not documented | ASIC ID and ASIC revision registers identify type and revision. See [Bosch datasheet, sections 7.1 and 7.2](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmp581-ds004.pdf). |
| BMP580 / BMP585 | Unknown | Variant-specific datasheet review is incomplete. The shared driver exposes chip/revision identifiers; these alone are not serial evidence. See [Bosch BMP5 driver definitions](https://raw.githubusercontent.com/boschsensortec/BMP5_SensorAPI/master/bmp5_defs.h). |
| DS2484 | Not documented for bridge | This is an I2C-to-1-Wire bridge. A discovered downstream 1-Wire ROM identifies the downstream component, not the bridge. See [Analog Devices datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/ds2484.pdf). |
| ENS160 | Not documented | PART_ID is the shared part number, not a unit serial. See [ScioSense datasheet, section 16.2.1](https://www.sciosense.com/wp-content/uploads/2023/12/ENS160-Datasheet.pdf). |
| LPS28DFW | Not documented for I2C | WHO_AM_I is a shared model identifier. No per-unit I2C identity was established; I3C identity semantics require separate evidence. See [ST datasheet](https://www.st.com.cn/resource/en/datasheet/lps28dfw.pdf). |
| MAX44009 | Not documented | The public register map describes status, configuration, measurements and thresholds; it does not establish a serial interface. See [Analog Devices datasheet, register map](https://www.analog.com/MAX44009/datasheet). |
| PMSA003I | Not documented | The published I2C measurement frame does not establish a per-unit serial. A printed module label, if present, is a separate observation. See [Plantower manual hosted by Adafruit](https://cdn-shop.adafruit.com/product-files/4632/4505_PMSA003I_series_data_manual_English_V2.6.pdf). |
| SPA06-003 | Not documented | The ID register contains product and revision nibbles; calibration coefficients are not guaranteed identities. See [manufacturer datasheet hosted by Seeed](https://files.seeedstudio.com/wiki/Grove-Barometer_Sensor-SPA06-003/res/Datasheet.PDF). |
| VCNL4030 | Not documented | The ID word contains model, revision and address-option information. See [Vishay datasheet](https://www.vishay.com/docs/84250/vcnl4030x01.pdf). |
| VCNL4200 | Not documented | The ID word contains model, revision and address information. See [Vishay datasheet](https://www.vishay.com/docs/84430/vcnl4200.pdf). |

## SBC, MCU, USB and debugger scope

For SBCs, distinguish a platform-reported board or silicon serial from the OS
installation identity. Hostnames, network addresses and filesystem locations
can change. An OS machine ID can identify an installation and may be cloned;
it is not proof of the physical board's identity. Retain the source and scope
of each observation rather than assigning every host identifier to the board.

For MCUs, prefer a documented silicon unique-ID interface when supported by the
reader and execution environment. A flash-chip ID identifies the flash component.
A bootloader's reported serial or USB descriptor might derive from silicon
identity, or be supplied by firmware; only a documented mapping establishes that
relationship. Do not infer it from a matching format.

USB vendor/product IDs identify a device class or product, not an individual.
A USB-to-UART adapter serial identifies the adapter unless separate evidence binds
it to the target MCU. Likewise a probe serial identifies the debugger, not the
SWD/JTAG target. Record USB interfaces and serial ports as endpoints belonging to
their observed USB device, and connect adapters/probes to DUTs through explicit
relationships. A single physical device can expose multiple interfaces and change
descriptors between application and bootloader modes.

USB topology paths, exported USBIP bus IDs and imported device nodes are
attachment observations. They may help locate a known asset but must not become
immutable identity keys. Power-control outlets and hub ports belong to the
connection topology; moving a device does not create a new physical identity.

## Requirements for future routed readers

A future sensor reader should accept an explicit resource selection, known model,
host bus and complete route. An address response establishes presence on that
route, not a sensor model or serial. Ambiguous models should remain unresolved
until there is model-specific evidence or an operator binding.

Before touching a shared bus, acquire leases covering the host interface, route,
mux controls and affected physical resources. Confirm that no other job owns a
conflicting path or can drive the bus. Read and record the initial routing state.
For a controller that supports isolation, first select its documented disconnected
state and verify it, then select and verify the intended host route. If route
verification fails, stop before sensor access.

Use a reviewed reader for the exact model and supported firmware. It should perform
only documented identity reads, validate response length and checksums, and enforce
the documented device state and timing. Avoid broad register sweeps, reserved
registers, calibration writes, EEPROM unlocking, general-call resets and automatic
flashing. An identity read that requires a mode transition needs an explicit,
restorable procedure under the same lease.

Store the observation before releasing the lease: reader version, timestamp,
identifier namespace and subject, raw response or digest, CRC result, route and
endpoint provenance, and any uncertainty. On success or failure, perform the
controller's verified teardown policy, such as returning the mux to disconnected
state. Record teardown failures and retain resource ownership until the controller
has made the state safe or escalated it for operator intervention.

Keep collection separate from reconciliation. An offline collector can produce
observations for review or import, but it must not silently reassign a target based
on a reused port or weak identity. Conflicting strong identifiers should require
resolution rather than merging two physical assets. Failed and partial discovery
must leave earlier verified identity and replacement history intact.
