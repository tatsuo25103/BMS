from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

import serial

from jbd_hv_protocol import BmsSample


PACE_REQUEST_VERSION = 0x25
PACE_FALLBACK_REQUEST_VERSIONS = (0x25, 0x20)
PACE_CID1 = 0x46
PACE_DEFAULT_ADDRESS = 0x00
PACE_CELLS_PER_PACK = 16
PACE_TEMPS_PER_PACK = 6

CID2_PACK_NUMBER = 0x90
CID2_ANALOG = 0x42
CID2_WARN = 0x44
CID2_SOFTWARE_VERSION = 0xC1
CID2_PRODUCT_INFO = 0xC2
CID2_CAPACITY = 0xA6

PS5120E_CELL_VOLTAGE_WARNING_LINES = [
    (2500.0, "#ef4444", "PS5120E UV 2500 mV"),
    (3650.0, "#ef4444", "PS5120E OV 3650 mV"),
]
PS5120E_TOTAL_VOLTAGE_WARNING_LINES = [
    (40.0, "#ef4444", "PS5120E UV 40.0 V"),
    (58.4, "#ef4444", "PS5120E OV 58.4 V"),
]


class PaceProtocolError(Exception):
    pass


@dataclass
class PaceFrame:
    version: int
    address: int
    cid1: int
    rtn: int
    info: bytes
    raw: bytes
    checksum_ok: bool


def _ascii_hex_byte(value: int) -> str:
    return f"{value & 0xFF:02X}"


def length_field(info_hex: str) -> str:
    length = len(info_hex)
    if length > 0x0FFF:
        raise ValueError("PACE INFO field is too long")
    nibbles = [(length >> 8) & 0x0F, (length >> 4) & 0x0F, length & 0x0F]
    lchksum = (-sum(nibbles)) & 0x0F
    return f"{lchksum:X}{length:03X}"


def validate_length_field(field: str, actual_info_hex_len: int) -> None:
    if len(field) != 4:
        raise PaceProtocolError("Invalid PACE LENGTH size")
    value = int(field, 16)
    lchksum = (value >> 12) & 0x0F
    length = value & 0x0FFF
    expected_lchksum = (-(((length >> 8) & 0x0F) + ((length >> 4) & 0x0F) + (length & 0x0F))) & 0x0F
    if lchksum != expected_lchksum:
        raise PaceProtocolError(f"PACE LENGTH checksum mismatch: {field}")
    if length != actual_info_hex_len:
        raise PaceProtocolError(f"PACE INFO length mismatch: expected {length}, got {actual_info_hex_len}")


def checksum_ascii(body: str) -> int:
    total = sum(body.encode("ascii")) & 0xFFFF
    return ((~total + 1) & 0xFFFF)


def build_frame(
    cid2: int,
    info: bytes = b"",
    *,
    address: int = PACE_DEFAULT_ADDRESS,
    version: int = PACE_REQUEST_VERSION,
) -> bytes:
    info_hex = info.hex().upper()
    body = (
        _ascii_hex_byte(version)
        + _ascii_hex_byte(address)
        + _ascii_hex_byte(PACE_CID1)
        + _ascii_hex_byte(cid2)
        + length_field(info_hex)
        + info_hex
    )
    checksum = checksum_ascii(body)
    return f"~{body}{checksum:04X}\r".encode("ascii")


def read_frame(ser: serial.Serial, *, response_timeout: float) -> PaceFrame:
    deadline = time.monotonic() + response_timeout
    raw = bytearray()
    started = False

    while time.monotonic() < deadline:
        chunk = ser.read(1)
        if not chunk:
            continue
        if not started:
            if chunk == b"~":
                started = True
                raw.extend(chunk)
            continue
        raw.extend(chunk)
        if chunk == b"\r":
            break

    if not raw:
        raise TimeoutError("No PACE response")
    if raw[-1:] != b"\r":
        raise TimeoutError(f"Incomplete PACE response: {raw.hex(' ')}")

    try:
        text = raw[1:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PaceProtocolError(f"PACE response is not ASCII: {raw.hex(' ')}") from exc

    if len(text) < 18:
        raise PaceProtocolError(f"PACE response too short: {raw.hex(' ')}")
    if len(text) % 2:
        raise PaceProtocolError(f"PACE response has odd ASCII hex length: {text}")

    checksum_text = text[-4:]
    body = text[:-4]
    try:
        checksum_received = int(checksum_text, 16)
        version = int(body[0:2], 16)
        address = int(body[2:4], 16)
        cid1 = int(body[4:6], 16)
        rtn = int(body[6:8], 16)
    except ValueError as exc:
        raise PaceProtocolError(f"PACE response contains non-hex fields: {text}") from exc

    length = body[8:12]
    info_hex = body[12:]
    validate_length_field(length, len(info_hex))
    checksum_ok = checksum_ascii(body) == checksum_received
    if not checksum_ok:
        raise PaceProtocolError(f"PACE checksum mismatch: {raw.hex(' ')}")

    try:
        info = bytes.fromhex(info_hex)
    except ValueError as exc:
        raise PaceProtocolError(f"PACE INFO is not valid hex: {info_hex}") from exc

    if cid1 != PACE_CID1:
        raise PaceProtocolError(f"Unexpected PACE CID1: 0x{cid1:02X}")
    if rtn != 0:
        raise PaceProtocolError(f"PACE RTN error: 0x{rtn:02X}")

    return PaceFrame(
        version=version,
        address=address,
        cid1=cid1,
        rtn=rtn,
        info=info,
        raw=bytes(raw),
        checksum_ok=checksum_ok,
    )


def send_command(
    ser: serial.Serial,
    cid2: int,
    info: bytes = b"",
    *,
    response_timeout: float,
    versions: tuple[int, ...] = PACE_FALLBACK_REQUEST_VERSIONS,
) -> PaceFrame:
    last_error: Exception | None = None
    for version in versions:
        try:
            ser.reset_input_buffer()
            ser.write(build_frame(cid2, info, version=version))
            ser.flush()
            return read_frame(ser, response_timeout=response_timeout)
        except TimeoutError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise TimeoutError("No PACE response")


def read_pack_number(ser: serial.Serial, *, response_timeout: float) -> tuple[int, str]:
    frame = send_command(ser, CID2_PACK_NUMBER, response_timeout=response_timeout)
    if len(frame.info) < 2:
        raise PaceProtocolError("PACE pack number response is too short")
    return frame.info[1], frame.raw.hex(" ")


def read_ascii_info(ser: serial.Serial, cid2: int, *, response_timeout: float) -> tuple[str, str]:
    frame = send_command(ser, cid2, response_timeout=response_timeout)
    text = frame.info.decode("ascii", errors="ignore").strip("\x00 \r\n\t")
    return text, frame.raw.hex(" ")


def read_capacity_info(ser: serial.Serial, *, response_timeout: float) -> tuple[float | None, float | None, float | None, str]:
    try:
        frame = send_command(ser, CID2_CAPACITY, b"\xFF", response_timeout=response_timeout)
    except (TimeoutError, PaceProtocolError, serial.SerialException):
        return None, None, None, ""
    if len(frame.info) < 6:
        return None, None, None, frame.raw.hex(" ")
    remaining = int.from_bytes(frame.info[0:2], "big") * 0.01
    full = int.from_bytes(frame.info[2:4], "big") * 0.01
    design = int.from_bytes(frame.info[4:6], "big") * 0.01
    return round(remaining, 2), round(full, 2), round(design, 2), frame.raw.hex(" ")


def read_warn_info(
    ser: serial.Serial,
    *,
    response_timeout: float,
    max_packs: int,
) -> tuple[list[str], list[int | None], str]:
    frame = send_command(ser, CID2_WARN, b"\xFF", response_timeout=response_timeout)
    errors, balance_states = decode_warn_info(frame.info, max_packs=max_packs)
    return errors, balance_states, frame.raw.hex(" ")


def decode_warn_info(info: bytes, *, max_packs: int) -> tuple[list[str], list[int | None]]:
    if len(info) < 3:
        raise PaceProtocolError("PACE warn response is too short")
    errors: list[str] = []
    balance_states: list[int | None] = []
    offset = 0
    offset += 1  # INFOFLAG 欄位
    pack_count = max(1, min(info[offset], max_packs))
    offset += 1

    for pack_index in range(1, pack_count + 1):
        if offset >= len(info):
            break
        cell_count = info[offset]
        offset += 1
        for cell_index in range(1, cell_count + 1):
            if offset >= len(info):
                raise PaceProtocolError("PACE warn cell data ended early")
            warn = info[offset]
            offset += 1
            if warn:
                errors.append(f"P{pack_index:02d}-C{cell_index:02d} voltage warn 0x{warn:02X}")

        if offset >= len(info):
            raise PaceProtocolError("PACE warn temperature count missing")
        temp_count = info[offset]
        offset += 1
        for temp_index in range(1, temp_count + 1):
            if offset >= len(info):
                raise PaceProtocolError("PACE warn temperature data ended early")
            warn = info[offset]
            offset += 1
            if warn:
                errors.append(f"P{pack_index:02d}-S{temp_index:02d} temperature warn 0x{warn:02X}")

        if offset + 12 > len(info):
            break
        charge_current_warn = info[offset]
        pack_total_voltage_warn = info[offset + 1]
        discharge_current_warn = info[offset + 2]
        protect_1 = info[offset + 3]
        protect_2 = info[offset + 4]
        fault = info[offset + 7]
        balance_1 = info[offset + 8]
        balance_2 = info[offset + 9]
        warn_1 = info[offset + 10]
        warn_2 = info[offset + 11]
        offset += 12

        for cell_index in range(16):
            mask = 1 << (cell_index % 8)
            source = balance_1 if cell_index < 8 else balance_2
            balance_states.append(1 if source & mask else 0)

        if charge_current_warn:
            errors.append(f"P{pack_index:02d} charge current warn")
        if pack_total_voltage_warn:
            errors.append(f"P{pack_index:02d} pack voltage warn")
        if discharge_current_warn:
            errors.append(f"P{pack_index:02d} discharge current warn")
        for bit, name in PACE_WARN1_BITS.items():
            if warn_1 & (1 << bit):
                errors.append(f"P{pack_index:02d} {name}")
        for bit, name in PACE_WARN2_BITS.items():
            if warn_2 & (1 << bit):
                errors.append(f"P{pack_index:02d} {name}")
        for value, label in [
            (protect_1, "protect state 1"),
            (protect_2, "protect state 2"),
            (fault, "fault state"),
        ]:
            if value:
                errors.append(f"P{pack_index:02d} {label} 0x{value:02X}")

    return errors, balance_states


PACE_WARN1_BITS = {
    0: "above cell voltage warn",
    1: "lower cell voltage warn",
    2: "above total voltage warn",
    3: "lower total voltage warn",
    4: "charge current warn",
    5: "discharge current warn",
}

PACE_WARN2_BITS = {
    0: "above charge temperature warn",
    1: "above discharge temperature warn",
    2: "low charge temperature warn",
    3: "low discharge temperature warn",
    4: "high environment temperature warn",
    5: "low environment temperature warn",
    6: "high MOS temperature warn",
    7: "low power warn",
}


def _signed_u16(raw: bytes) -> int:
    value = int.from_bytes(raw, "big", signed=False)
    return value - 0x10000 if value & 0x8000 else value


def decode_analog_info(info: bytes, *, max_packs: int) -> dict[str, object]:
    if len(info) < 3:
        raise PaceProtocolError("PACE analog response is too short")
    offset = 0
    info_flag = info[offset]
    offset += 1
    pack_count = max(1, info[offset])
    offset += 1
    pack_count = min(pack_count, max_packs)

    all_cells: list[int | None] = []
    all_temps: list[float] = []
    pack_voltages_v: list[float] = []
    pack_currents_a: list[float] = []
    remaining_capacity_ah: float | None = None
    cycle_count: int | None = None
    cells_per_pack: int | None = None

    for pack_index in range(pack_count):
        if offset >= len(info):
            break
        cell_count = info[offset]
        offset += 1
        cells_per_pack = cell_count
        for _cell_index in range(cell_count):
            if offset + 2 > len(info):
                raise PaceProtocolError("PACE analog cell data ended early")
            all_cells.append(int.from_bytes(info[offset : offset + 2], "big"))
            offset += 2

        if offset >= len(info):
            raise PaceProtocolError("PACE analog temperature count missing")
        temp_count = info[offset]
        offset += 1
        for _temp_index in range(temp_count):
            if offset + 2 > len(info):
                raise PaceProtocolError("PACE analog temperature data ended early")
            raw_temp = int.from_bytes(info[offset : offset + 2], "big")
            all_temps.append(round(raw_temp / 10.0 - 273.0, 1))
            offset += 2

        if offset + 6 > len(info):
            raise PaceProtocolError("PACE analog pack summary ended early")
        current_raw = _signed_u16(info[offset : offset + 2])
        offset += 2
        voltage_mv = int.from_bytes(info[offset : offset + 2], "big")
        offset += 2
        remaining_raw = int.from_bytes(info[offset : offset + 2], "big")
        offset += 2
        pack_currents_a.append(round(current_raw * 0.01, 2))
        pack_voltages_v.append(round(voltage_mv / 1000.0, 3))
        if remaining_capacity_ah is None:
            remaining_capacity_ah = round(remaining_raw * 0.01, 2)

        # TY16S 文件把尾端欄位標成帶數量的 define set。
        # 實測 PS5120E 在剩餘容量後的第一個 byte 是 define 數量，
        # 後面依序是滿充容量、循環次數與設計容量；這裡保守推進 offset，
        # 以免之後多包資料解析錯位。
        if offset < len(info):
            define_count = info[offset]
            offset += 1
            trailing_bytes = min(max(define_count, 0) * 2, len(info) - offset)
            if trailing_bytes >= 4 and cycle_count is None:
                cycle_count = int.from_bytes(info[offset + 2 : offset + 4], "big")
            offset += trailing_bytes

    voltage_v = round(sum(pack_voltages_v), 3) if pack_voltages_v else None
    current_a = round(sum(pack_currents_a), 2) if pack_currents_a else None
    actual_pack_count = len(pack_voltages_v) or pack_count
    return {
        "info_flag": info_flag,
        "configured_pack_count": actual_pack_count,
        "cells_per_pack": cells_per_pack or PACE_CELLS_PER_PACK,
        "total_cell_count": actual_pack_count * (cells_per_pack or PACE_CELLS_PER_PACK),
        "cell_voltages_mv": all_cells,
        "temperatures_c": all_temps,
        "ntc_count": len(all_temps),
        "voltage_v": voltage_v,
        "current_a": current_a,
        "remaining_capacity_ah": remaining_capacity_ah,
        "cycle_count": cycle_count,
        "pack_voltages_v": pack_voltages_v,
    }


def poll_ps5120_bms(
    ser: serial.Serial,
    *,
    response_timeout: float,
    max_packs: int = 30,
) -> BmsSample:
    sample = BmsSample(timestamp=dt.datetime.now().isoformat(timespec="seconds"))
    sample.basic_checksum_ok = True
    sample.cells_checksum_ok = True
    sample.charge_state = None
    sample.protection_status = 0
    sample.fault_status = 0
    sample.alarm_status = 0
    sample.pace_warn_errors = []

    raw_parts: list[str] = []
    try:
        sample.configured_pack_count, raw = read_pack_number(ser, response_timeout=response_timeout)
        raw_parts.append(f"pack_number={raw}")
    except (TimeoutError, PaceProtocolError, serial.SerialException):
        sample.configured_pack_count = None

    try:
        sample.software_version, raw = read_ascii_info(ser, CID2_SOFTWARE_VERSION, response_timeout=response_timeout)
        raw_parts.append(f"fw={raw}")
    except (TimeoutError, PaceProtocolError, serial.SerialException):
        pass

    try:
        sample.serial_number, raw = read_ascii_info(ser, CID2_PRODUCT_INFO, response_timeout=response_timeout)
        raw_parts.append(f"product={raw}")
    except (TimeoutError, PaceProtocolError, serial.SerialException):
        pass

    analog_frame = send_command(ser, CID2_ANALOG, b"\xFF", response_timeout=response_timeout)
    sample.basic_raw = analog_frame.raw.hex(" ")
    sample.cells_raw = sample.basic_raw
    raw_parts.append(f"analog={sample.basic_raw}")
    analog = decode_analog_info(analog_frame.info, max_packs=max_packs)
    for key, value in analog.items():
        setattr(sample, key, value)

    remaining, full, _design, raw = read_capacity_info(ser, response_timeout=response_timeout)
    if raw:
        raw_parts.append(f"capacity={raw}")
    if remaining is not None:
        sample.remaining_capacity_ah = remaining
    if full is not None:
        sample.full_capacity_ah = full
    if sample.full_capacity_ah and sample.remaining_capacity_ah is not None:
        sample.soc_percent = round(sample.remaining_capacity_ah / sample.full_capacity_ah * 100.0, 1)

    if sample.current_a is not None:
        sample.charge_state = 1 if sample.current_a > 0.05 else 2 if sample.current_a < -0.05 else 0

    try:
        sample.pace_warn_errors, sample.cell_balance_states, raw = read_warn_info(
            ser,
            response_timeout=response_timeout,
            max_packs=max_packs,
        )
        raw_parts.append(f"warn={raw}")
    except (TimeoutError, PaceProtocolError, serial.SerialException):
        pass

    pack_count = sample.configured_pack_count or 0
    sample.temperature_sensor_names = [
        f"P{pack_index:02d}-S{sensor_index:02d}"
        for pack_index in range(1, pack_count + 1)
        for sensor_index in range(1, PACE_TEMPS_PER_PACK + 1)
    ][: len(sample.temperatures_c)]
    sample.product_model = "PS5120E"
    sample.cell_voltage_warning_lines = PS5120E_CELL_VOLTAGE_WARNING_LINES.copy()
    sample.total_voltage_warning_lines_v = PS5120E_TOTAL_VOLTAGE_WARNING_LINES.copy()
    sample.stats_raw = " | ".join(raw_parts)
    return sample
