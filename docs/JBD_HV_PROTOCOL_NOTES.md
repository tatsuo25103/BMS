# JBD HV Protocol Notes

Primary source:

`C:\Users\lf.wu\Downloads\JBD-HV系列-上位机通信协议-V3.37_20250308.pdf`

Project rule: use this document as the source of truth for JBD-HVBCUM01 protocol behavior. Do not use the low-voltage JBD `DD A5` protocol for this BMS.

## Serial Settings

- Baud rates in the document: `9600`, `19200`, `38400`, `115200`
- Default: `9600`
- Data format: `8N1`

## Internal Register Read

Request:

```text
address function start_hi start_lo end_hi end_lo len_hi len_lo crc_lo crc_hi
```

- Default address: `0x01`
- Read function: `0x78`
- Multi-byte register data is big-endian.
- CRC is standard Modbus CRC16, transmitted low byte first.

CRC check from the PDF sleep-command example:

```text
payload: 01 5F 10 00 10 01 00 01 5A A5
crc:     09 76
```

## Current First-Pass Reads

- Main live/register area: `0x1000` to `0x107D`
- Configuration area: `0x2200` to `0x2211`
- Statistics probe: `0x4000` to `0x4014` with function `0x50`
- Temperature warning/protection setting reads used for GUI reference lines:
  - Charge high temperature: `0x18A8`
  - Charge low temperature: `0x18C0`
  - Discharge high temperature: `0x18D8`
  - Discharge low temperature: `0x18F0`
  - Ambient high temperature: `0x1908`
  - Ambient low temperature: `0x1920`
  - Each group is decoded as L1/L2/L3 values at word offsets `0`, `4`, and `8`; raw temperature scale is `0.1 - 50 C`.
- Voltage warning/protection setting reads used for GUI reference lines:
  - Cell over voltage: `0x1800`, raw scale `1 mV`
  - Cell under voltage: `0x1818`, raw scale `1 mV`
  - Total/pack over voltage: `0x1830`, raw scale `0.1 V`
  - Total/pack under voltage: `0x1848`, raw scale `0.1 V`
  - Each group is decoded as L1/L2/L3 values at word offsets `0`, `4`, and `8`.
  - The GUI pack-voltage chart divides total-voltage thresholds by the detected pack count because the plotted pack voltage is derived per battery pack.

## Error Decoding

Alarm registers are decoded as 2-bit levels:

- `0`: no alarm
- `1`: level 1 alarm
- `2`: level 2 alarm
- `3`: level 3 protection

Fault bits from the PDF:

- `E00-EEP Fault`
- `E01-RTC Abnormal`
- `E02-Relay Sticking`
- `E03-Relay Open Circuit`
- `E04-Cell Offline`
- `E05-BMU Offline`
- `E06-NTC Offline`
- `E07-High Voltage Sampling Fault`
- `E08-Current Sampling Fault`

Observed on HVBCUM01 live reads:

- `0x101C` is used for the current visible alarm/protection code shown by the upper computer/LCD.
- `0x101E` and `0x1020` are the fault high/low words; the active fault bits observed so far are in `0x1020`.
