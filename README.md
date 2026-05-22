# BMS Data Collector

Windows GUI and CSV logger for battery management systems used in MES battery products.

Supported models:

- `HV140`: JBD high-voltage BMS over RS485
- `PS5120E`: PACE RS232 BMS protocol

The application scans serial ports, identifies the connected BMS model, reads live pack data, plots trends, records CSV logs, and can reload previous logs for review.

## Features

- Tkinter dashboard with live status, error codes, and packet health
- Auto scan for COM port, baud rate, and supported BMS model
- Time-series charts for total voltage/current/SOC, pack voltage, cell voltage, and temperature sensors
- Event markers for start, stop, packet loss, warning, protect, and fault events
- CSV logging with generated filenames containing date, time, product model, and serial number
- CSV log loading for offline chart review
- Multi-pack support:
  - HV140: up to 14 packs, with PDU temperature sensors
  - PS5120E: up to 30 packs, no PDU

## Setup

Install Python 3.10 or newer, then install the dependency:

```powershell
python -m pip install -r requirements.txt
```

## Run The GUI

```powershell
python .\jbd_gui.py
```

Typical workflow:

1. Select `HV140` or `PS5120E`, or leave the current model and click `Scan`.
2. Confirm the detected COM port and baud rate.
3. Click `Start` to begin logging.
4. Click `Stop` to stop logging and render the full chart.
5. Click `Export CSV...` to save a copy of the current log.

The current CSV file path is shown in the left status panel under `CSV File`.

## Command Line

List ports:

```powershell
python .\jbd_collector.py --list-ports
```

Scan:

```powershell
python .\jbd_collector.py --scan
```

Run with a known port:

```powershell
python .\jbd_collector.py --port COM4 --baud 9600 --csv .\bms_log.csv
```

## Project Files

- `jbd_gui.py`: GUI dashboard and charting
- `jbd_collector.py`: CLI, CSV logging, scan helpers
- `jbd_hv_protocol.py`: JBD HV protocol implementation
- `pace_rs232_protocol.py`: PACE RS232 protocol implementation
- `assets/`: MES logo assets
- `docs/`: protocol notes and reference documents

## Notes

- Runtime CSV logs, virtual environments, caches, and local test outputs are intentionally excluded from Git.
- Source code comments are written in Traditional Chinese where comments are needed.
