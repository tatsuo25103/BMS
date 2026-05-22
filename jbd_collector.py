from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import serial
import serial.tools.list_ports

import jbd_hv_protocol as hv
import pace_rs232_protocol as pace

READ_BASIC_INFO = hv.build_read_request(0x1000, 0x107D)
READ_CELL_VOLTAGES = hv.build_read_request(0x2200, 0x2211)
DEFAULT_BAUD_RATES = [9600, 19200, 38400, 115200]
PRODUCT_HV140 = "HV140"
PRODUCT_PS5120E = "PS5120E"
SUPPORTED_PRODUCT_MODELS = [PRODUCT_HV140, PRODUCT_PS5120E]
PROBE_REQUESTS = [
    ("jbd_hv_main_1000_107d", READ_BASIC_INFO),
    ("jbd_hv_config_2200_2211", READ_CELL_VOLTAGES),
    ("jbd_hv_statistics_4000_4014", hv.build_read_request(0x4000, 0x4014, function=0x50)),
]


class JbdProtocolError(Exception):
    pass


def charge_discharge_state(current_a: float | hv.BmsSample | None) -> str:
    if isinstance(current_a, hv.BmsSample):
        return hv.charge_discharge_state(current_a)
    if current_a is None:
        return "unknown"
    if current_a > 0.05:
        return "charging"
    if current_a < -0.05:
        return "discharging"
    return "idle"


def sample_error_codes(sample: hv.BmsSample) -> list[str]:
    errors = hv.decode_error_codes(sample)
    errors.extend(getattr(sample, "pace_warn_errors", []) or [])
    deduped: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error in seen:
            continue
        seen.add(error)
        deduped.append(error)
    return deduped


def sample_warnings(sample: hv.BmsSample) -> list[str]:
    return sample_error_codes(sample)


@dataclass
class ScanResult:
    port: str
    baud: int
    product_model: str
    voltage_v: float | None
    soc_percent: int | None
    cells_per_pack: int | None
    raw: str


def read_any_bytes(ser: serial.Serial, *, timeout: float, idle_timeout: float = 0.25) -> bytes:
    deadline = time.monotonic() + timeout
    idle_deadline: float | None = None
    buffer = bytearray()

    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buffer.extend(chunk)
            idle_deadline = time.monotonic() + idle_timeout
            continue
        if buffer and idle_deadline is not None and time.monotonic() >= idle_deadline:
            break

    return bytes(buffer)


def poll_bms(
    ser: serial.Serial,
    *,
    response_timeout: float,
    enforce_checksum: bool,
    invert_current: bool,
    pack_count: int | None,
    cells_per_pack: int | None,
    max_packs: int,
    product_model: str = PRODUCT_HV140,
) -> hv.BmsSample:
    if product_model == PRODUCT_PS5120E:
        try:
            return pace.poll_ps5120_bms(
                ser,
                response_timeout=response_timeout,
                max_packs=max_packs,
            )
        except pace.PaceProtocolError as exc:
            raise JbdProtocolError(str(exc)) from exc
    if product_model != PRODUCT_HV140:
        raise JbdProtocolError(f"Unsupported product model: {product_model}")
    try:
        return hv.poll_hv_bms(
            ser,
            response_timeout=response_timeout,
            max_packs=max_packs,
            pack_count_override=pack_count,
            cells_per_pack_override=cells_per_pack,
        )
    except hv.JbdHvProtocolError as exc:
        raise JbdProtocolError(str(exc)) from exc


def available_ports() -> list[str]:
    return [port.device for port in serial.tools.list_ports.comports()]


def parse_baud_rates(value: str) -> list[int]:
    rates = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            rates.append(int(part))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid baud rate: {part}") from exc
    if not rates:
        raise argparse.ArgumentTypeError("At least one baud rate is required")
    return rates


def scan_one(
    port: str,
    baud: int,
    *,
    response_timeout: float,
    enforce_checksum: bool,
    invert_current: bool,
    product_model: str = PRODUCT_HV140,
    max_packs: int = 14,
) -> ScanResult | None:
    if product_model == PRODUCT_PS5120E:
        try:
            with serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            ) as ser:
                probe_timeout = min(response_timeout, 0.8)
                analog_frame = pace.send_command(
                    ser,
                    pace.CID2_ANALOG,
                    b"\xFF",
                    response_timeout=probe_timeout,
                )
                analog = pace.decode_analog_info(analog_frame.info, max_packs=max_packs)
                return ScanResult(
                    port=port,
                    baud=baud,
                    product_model=product_model,
                    voltage_v=analog.get("voltage_v"),
                    soc_percent=None,
                    cells_per_pack=analog.get("cells_per_pack"),
                    raw=analog_frame.raw.hex(" "),
                )
        except (TimeoutError, pace.PaceProtocolError, serial.SerialException, OSError, ValueError):
            return None
    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=1.0,
        ) as ser:
            sample = poll_bms(
                ser,
                response_timeout=response_timeout,
                enforce_checksum=enforce_checksum,
                invert_current=invert_current,
                pack_count=None,
                cells_per_pack=None,
                max_packs=max_packs,
                product_model=product_model,
            )
            return ScanResult(
                port=port,
                baud=baud,
                product_model=product_model,
                voltage_v=sample.voltage_v,
                soc_percent=sample.soc_percent,
                cells_per_pack=sample.cells_per_pack,
                raw=sample.basic_raw,
            )
    except (TimeoutError, JbdProtocolError, serial.SerialException, OSError, ValueError):
        return None


def scan_bms(
    *,
    ports: list[str],
    baud_rates: list[int],
    response_timeout: float,
    enforce_checksum: bool,
    invert_current: bool,
    product_model: str = PRODUCT_HV140,
    max_packs: int = 14,
) -> list[ScanResult]:
    results = []
    product_models = [product_model] + [
        model for model in SUPPORTED_PRODUCT_MODELS if model != product_model
    ]
    for port in ports:
        for baud in baud_rates:
            found = False
            for candidate_model in product_models:
                result = scan_one(
                    port,
                    baud,
                    response_timeout=response_timeout,
                    enforce_checksum=enforce_checksum,
                    invert_current=invert_current,
                    product_model=candidate_model,
                    max_packs=max_packs,
                )
                if result:
                    results.append(result)
                    found = True
                    break
            if found:
                break
    return results


def probe_port(port: str, baud: int, *, response_timeout: float) -> list[tuple[str, bytes, bytes]]:
    observations = []
    with serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
        write_timeout=1.0,
    ) as ser:
        for name, request in PROBE_REQUESTS:
            ser.reset_input_buffer()
            ser.write(request)
            response = read_any_bytes(ser, timeout=response_timeout)
            observations.append((name, request, response))
            time.sleep(0.2)
    return observations


def print_sample(sample: hv.BmsSample, *, show_raw: bool) -> None:
    online_cells = [value for value in sample.cell_voltages_mv if value is not None]
    min_cell = min(online_cells) if online_cells else None
    max_cell = max(online_cells) if online_cells else None
    delta_mv = max_cell - min_cell if min_cell is not None and max_cell is not None else None
    temps = ", ".join(f"{temp:.1f}C" for temp in sample.temperatures_c) or "n/a"

    print(
        f"[{sample.timestamp}] "
        f"V={format_value(sample.voltage_v, 'V')} "
        f"I={format_value(sample.current_a, 'A')} "
        f"SOC={format_value(sample.soc_percent, '%')} "
        f"Cap={format_value(sample.remaining_capacity_ah, 'Ah')}/"
        f"{format_value(sample.full_capacity_ah, 'Ah')} "
        f"Packs={sample.configured_pack_count or 'n/a'} "
        f"CellsPerPack={sample.cells_per_pack or 'n/a'} "
        f"TotalCells={sample.total_cell_count or 'n/a'} "
        f"CellsRead={len(sample.cell_voltages_mv) or 'n/a'} "
        f"Min/Max/Delta={min_cell or 'n/a'}/{max_cell or 'n/a'}/{delta_mv or 'n/a'} mV "
        f"Temps={temps}"
    )

    if sample.cell_voltages_mv:
        cells = ", ".join(
            f"C{index:03d}={value}mV" if value is not None else f"C{index:03d}=not detected"
            for index, value in enumerate(sample.cell_voltages_mv, start=1)
        )
        print(f"  cell voltages: {cells}")

    if show_raw:
        print(f"  basic: {sample.basic_raw}")
        print(f"  config: {getattr(sample, 'config_raw', '')}")
        print(f"  cells: {sample.cells_raw}")

    errors = sample_error_codes(sample)
    if errors:
        print(f"  errors: {'; '.join(errors)}")


def format_value(value: Any, suffix: str) -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def csv_fieldnames(max_cells: int, max_temps: int) -> list[str]:
    fields = [
        "timestamp",
        "product_model",
        "voltage_v",
        "current_a",
        "remaining_capacity_ah",
        "full_capacity_ah",
        "soc_percent",
        "cycle_count",
        "serial_number",
        "configured_pack_count",
        "cells_per_pack",
        "total_cell_count",
        "detected_cell_voltage_count",
        "ntc_count",
        "protection_status",
        "fet_status",
        "alarm_status",
        "fault_status",
        "charge_state",
        "software_version",
        "basic_checksum_ok",
        "cells_checksum_ok",
        "error_codes",
    ]
    fields.extend(f"temp_{index:02d}_c" for index in range(1, max_temps + 1))
    fields.extend(f"cell_{index:02d}_mv" for index in range(1, max_cells + 1))
    fields.extend(["basic_raw", "config_raw", "cells_raw", "stats_raw"])
    return fields


def sample_to_row(sample: hv.BmsSample, *, max_cells: int, max_temps: int) -> dict[str, Any]:
    row = {
        "timestamp": sample.timestamp,
        "product_model": getattr(sample, "product_model", ""),
        "voltage_v": sample.voltage_v,
        "current_a": sample.current_a,
        "remaining_capacity_ah": sample.remaining_capacity_ah,
        "full_capacity_ah": sample.full_capacity_ah,
        "soc_percent": sample.soc_percent,
        "cycle_count": sample.cycle_count,
        "serial_number": sample.serial_number,
        "configured_pack_count": sample.configured_pack_count,
        "cells_per_pack": sample.cells_per_pack,
        "total_cell_count": sample.total_cell_count,
        "detected_cell_voltage_count": len(sample.cell_voltages_mv) or "",
        "ntc_count": sample.ntc_count,
        "protection_status": sample.protection_status,
        "fet_status": sample.fet_status,
        "alarm_status": getattr(sample, "alarm_status", None),
        "fault_status": getattr(sample, "fault_status", None),
        "charge_state": getattr(sample, "charge_state", None),
        "software_version": sample.software_version,
        "basic_checksum_ok": sample.basic_checksum_ok,
        "cells_checksum_ok": sample.cells_checksum_ok,
        "error_codes": "; ".join(sample_error_codes(sample)),
        "basic_raw": sample.basic_raw,
        "config_raw": getattr(sample, "config_raw", ""),
        "cells_raw": sample.cells_raw,
        "stats_raw": getattr(sample, "stats_raw", ""),
    }
    for index in range(max_temps):
        row[f"temp_{index + 1:02d}_c"] = (
            sample.temperatures_c[index] if index < len(sample.temperatures_c) else ""
        )
    for index in range(max_cells):
        row[f"cell_{index + 1:02d}_mv"] = (
            sample.cell_voltages_mv[index] if index < len(sample.cell_voltages_mv) else ""
        )
    return row


def build_log_path(base_path: Path, sample: hv.BmsSample) -> Path:
    timestamp = log_timestamp_for_filename(sample.timestamp)
    product_model = sanitize_filename_part(getattr(sample, "product_model", "") or "BMS")
    serial_number = sanitize_filename_part(sample.serial_number or "NO_SERIAL")
    return base_path.parent / f"{timestamp}_{product_model}_{serial_number}.csv"


def log_timestamp_for_filename(timestamp: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        parsed = dt.datetime.now()
    return parsed.strftime("%Y%m%d_%H%M%S")


def sanitize_filename_part(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:80] or "UNKNOWN"


class CsvLogger:
    def __init__(self, path: Path, *, max_cells: int, max_temps: int) -> None:
        self.path = path
        self.max_cells = max_cells
        self.max_temps = max_temps
        self.fieldnames = csv_fieldnames(max_cells, max_temps)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=self.fieldnames)
        if self.path.stat().st_size == 0:
            self.writer.writeheader()
            self.file.flush()

    def write(self, sample: hv.BmsSample) -> None:
        self.writer.writerow(
            sample_to_row(sample, max_cells=self.max_cells, max_temps=self.max_temps)
        )
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect JBD-HVBCUM01 BMS data over RS485.")
    parser.add_argument("--port", help="Windows COM port, for example COM4.")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan COM ports and baud rates for a JBD BMS, then exit.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-detect COM port and baud rate, then start collecting.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Send diagnostic probe requests and print any raw bytes received.",
    )
    parser.add_argument("--baud", type=int, default=9600, help="Serial baud rate. Default: 9600.")
    parser.add_argument(
        "--baud-rates",
        type=parse_baud_rates,
        default=DEFAULT_BAUD_RATES,
        help="Comma-separated baud rates for --scan/--auto. Default: 9600,19200,38400,115200.",
    )
    parser.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds.")
    parser.add_argument("--count", type=int, help="Number of samples to collect before exiting.")
    parser.add_argument("--timeout", type=float, default=1.5, help="Response timeout in seconds.")
    parser.add_argument("--csv", type=Path, default=Path("bms_log.csv"), help="CSV output path.")
    parser.add_argument(
        "--pack-count",
        type=int,
        default=None,
        help="Optional manual pack-count override. By default pack count is read from HV configuration registers.",
    )
    parser.add_argument(
        "--cells-per-pack",
        type=int,
        help="Override cells per pack. By default this is read from the HV configuration registers.",
    )
    parser.add_argument(
        "--max-packs",
        type=int,
        default=14,
        help="Maximum allowed battery packs. Default: 14.",
    )
    parser.add_argument("--max-cells", type=int, default=224, help="Maximum CSV cell columns.")
    parser.add_argument("--max-temps", type=int, default=88, help="Maximum CSV temperature columns.")
    parser.add_argument("--raw", action="store_true", help="Print raw response frames.")
    parser.add_argument(
        "--no-checksum",
        action="store_true",
        help="Do not reject frames when checksum differs; still records checksum status.",
    )
    parser.add_argument(
        "--invert-current",
        action="store_true",
        help="Flip current sign if your firmware reports charge/discharge opposite of expectation.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.list_ports:
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            print("No serial ports found.")
            return 0
        for port in ports:
            print(f"{port.device}: {port.description}")
        return 0

    if args.scan or args.auto:
        ports = [args.port] if args.port else available_ports()
        if not ports:
            print("No serial ports found.")
            return 1

        print(
            "Scanning "
            f"{', '.join(ports)} at baud rates {', '.join(str(rate) for rate in args.baud_rates)}..."
        )
        results = scan_bms(
            ports=ports,
            baud_rates=args.baud_rates,
            response_timeout=args.timeout,
            enforce_checksum=not args.no_checksum,
            invert_current=args.invert_current,
        )

        if not results:
            print("No JBD BMS response found.")
            return 1

        print("Found:")
        for result in results:
            print(
                f"  {result.port} @ {result.baud}: "
                f"V={format_value(result.voltage_v, 'V')} "
                f"SOC={format_value(result.soc_percent, '%')} "
                f"CellsPerPack={result.cells_per_pack or 'n/a'}"
            )

        if args.scan:
            return 0

        args.port = results[0].port
        args.baud = results[0].baud

    if not args.port:
        print(
            "error: --port is required unless --list-ports, --scan, or --auto is used",
            file=sys.stderr,
        )
        return 2

    if args.probe:
        try:
            observations = probe_port(args.port, args.baud, response_timeout=args.timeout)
        except (serial.SerialException, OSError) as exc:
            print(f"Could not open {args.port}: {exc}", file=sys.stderr)
            return 1

        print(f"Probe results for {args.port} @ {args.baud}:")
        for name, request, response in observations:
            print(f"  {name}")
            print(f"    tx: {request.hex(' ')}")
            print(f"    rx: {response.hex(' ') if response else '(no response)'}")
        return 0

    logger: CsvLogger | None = None

    try:
        with serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=1.0,
        ) as ser:
            print(f"Connected to {args.port} at {args.baud} baud. CSV will be created after first sample.")
            print("Press Ctrl+C to stop.")

            samples_collected = 0
            next_poll = time.monotonic()
            while True:
                wait_time = next_poll - time.monotonic()
                if wait_time > 0:
                    time.sleep(wait_time)
                started_at = time.monotonic()
                try:
                    sample = poll_bms(
                        ser,
                        response_timeout=args.timeout,
                        enforce_checksum=not args.no_checksum,
                        invert_current=args.invert_current,
                        pack_count=args.pack_count,
                        cells_per_pack=args.cells_per_pack,
                        max_packs=args.max_packs,
                    )
                    print_sample(sample, show_raw=args.raw)
                    if logger is None:
                        log_path = build_log_path(args.csv, sample)
                        logger = CsvLogger(log_path, max_cells=args.max_cells, max_temps=args.max_temps)
                        print(f"Writing CSV to {log_path}")
                    logger.write(sample)
                    samples_collected += 1
                except (TimeoutError, JbdProtocolError, serial.SerialException) as exc:
                    timestamp = dt.datetime.now().isoformat(timespec="seconds")
                    print(f"[{timestamp}] read error: {exc}", file=sys.stderr)
                if args.count is not None and samples_collected >= args.count:
                    break
                next_poll += args.interval
                if next_poll <= started_at:
                    next_poll = started_at + args.interval
    except (serial.SerialException, OSError) as exc:
        print(f"Could not open/read {args.port}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if logger:
            logger.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
