from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

import serial


HV_DEFAULT_ADDRESS = 0x01
HV_READ_FUNCTION = 0x78
HV_STATISTICS_FUNCTION = 0x50
HV_DEFAULT_CELLS_PER_PACK = 16
HV_UNDETECTED_CELL_MV = 100
HV_PDU_TEMP_ADDRESSES = (0x1010, 0x1034, 0x1038, 0x103A)
HV_BMU_TEMPS_PER_PACK = 6
TEMP_THRESHOLD_GROUPS = [
    ("Charge High", 0x18A8),
    ("Charge Low", 0x18C0),
    ("Discharge High", 0x18D8),
    ("Discharge Low", 0x18F0),
    ("Ambient High", 0x1908),
    ("Ambient Low", 0x1920),
]
TEMP_LEVEL_COLORS = {
    1: "#3b82f6",
    2: "#ff7a1a",
    3: "#ef4444",
}
CELL_VOLTAGE_THRESHOLD_GROUPS = [
    ("Cell OV", 0x1800, 1.0),
    ("Cell UV", 0x1818, 1.0),
]
TOTAL_VOLTAGE_THRESHOLD_GROUPS = [
    ("Pack OV", 0x1830, 0.1),
    ("Pack UV", 0x1848, 0.1),
]


class JbdHvProtocolError(Exception):
    pass


@dataclass
class HvRegisterFrame:
    address: int
    function: int
    start: int
    end: int
    data: bytes
    raw: bytes
    checksum_ok: bool


@dataclass
class BmsSample:
    timestamp: str
    product_model: str = "HV140"
    voltage_v: float | None = None
    current_a: float | None = None
    remaining_capacity_ah: float | None = None
    full_capacity_ah: float | None = None
    soc_percent: float | None = None
    cycle_count: int | None = None
    cells_per_pack: int | None = None
    configured_pack_count: int | None = None
    total_cell_count: int | None = None
    ntc_count: int | None = None
    temperatures_c: list[float] = field(default_factory=list)
    temperature_sensor_names: list[str] = field(default_factory=list)
    temperature_warning_lines: list[tuple[float, str, str]] = field(default_factory=list)
    cell_voltage_warning_lines: list[tuple[float, str, str]] = field(default_factory=list)
    total_voltage_warning_lines_v: list[tuple[float, str, str]] = field(default_factory=list)
    cell_voltages_mv: list[int | None] = field(default_factory=list)
    cell_balance_states: list[int | None] = field(default_factory=list)
    pack_voltages_v: list[float] = field(default_factory=list)
    protection_status: int | None = None
    fet_status: int | None = None
    software_version: str | None = None
    serial_number: str = ""
    alarm_status: int | None = None
    fault_status: int | None = None
    charge_state: int | None = None
    basic_checksum_ok: bool | None = None
    cells_checksum_ok: bool | None = None
    basic_raw: str = ""
    cells_raw: str = ""
    config_raw: str = ""
    stats_raw: str = ""


@dataclass
class ScanResult:
    port: str
    baud: int
    voltage_v: float | None
    soc_percent: float | None
    cells_per_pack: int | None
    raw: str


ALARM_2BIT_FIELDS = [
    "Cell Over Voltage",
    "Cell Under Voltage",
    "Pack Over Voltage",
    "Pack Under Voltage",
    "Voltage Difference Too Large",
    "Charge Overcurrent",
    "Discharge Overcurrent",
    "Charge High Temperature",
    "Charge Low Temperature",
    "Discharge High Temperature",
    "Discharge Low Temperature",
    "Temperature Difference Too Large",
    "Temperature Rise Too Fast",
    "Pole High Temperature",
    "Ambient High Temperature",
    "Ambient Low Temperature",
    "SOC Too Low",
    "Insulation Resistance Too Low",
    "Full Charge Calibration Alarm",
]

FAULT_BITS = {
    0: "E00-EEP Fault",
    1: "E01-RTC Abnormal",
    2: "E02-Relay Sticking",
    3: "E03-Relay Open Circuit",
    4: "E04-Cell Offline",
    5: "E05-BMU Offline",
    6: "E06-NTC Offline",
    7: "E07-High Voltage Sampling Fault",
    8: "E08-Current Sampling Fault",
}

CHARGE_STATES = {
    0: "standby",
    1: "charging",
    2: "discharging",
}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
            crc &= 0xFFFF
    return crc


def build_read_request(
    start: int,
    end: int,
    *,
    address: int = HV_DEFAULT_ADDRESS,
    function: int = HV_READ_FUNCTION,
) -> bytes:
    if end < start:
        raise ValueError("end address must be >= start address")
    data_length = end - start + 1
    payload = bytes([address, function]) + start.to_bytes(2, "big")
    payload += end.to_bytes(2, "big") + data_length.to_bytes(2, "big")
    return payload + crc16_modbus(payload).to_bytes(2, "little")


def read_register_frame(
    ser: serial.Serial,
    *,
    timeout: float,
    expected_function: int | None = None,
) -> HvRegisterFrame:
    deadline = time.monotonic() + timeout
    header = bytearray()

    while len(header) < 8 and time.monotonic() < deadline:
        chunk = ser.read(8 - len(header))
        if chunk:
            header.extend(chunk)

    if len(header) < 8:
        raise TimeoutError("Timed out waiting for a JBD-HV response header")

    address = header[0]
    function = header[1]
    start = int.from_bytes(header[2:4], "big")
    end = int.from_bytes(header[4:6], "big")
    data_length = int.from_bytes(header[6:8], "big")

    if expected_function is not None and function != expected_function:
        raise JbdHvProtocolError(
            f"Unexpected function 0x{function:02X}, expected 0x{expected_function:02X}"
        )
    if data_length > 4096:
        raise JbdHvProtocolError(f"Unreasonable data length: {data_length}")

    tail = bytearray()
    expected_tail_len = data_length + 2
    while len(tail) < expected_tail_len and time.monotonic() < deadline:
        chunk = ser.read(expected_tail_len - len(tail))
        if chunk:
            tail.extend(chunk)

    if len(tail) < expected_tail_len:
        raise TimeoutError("Timed out waiting for a complete JBD-HV response")

    raw = bytes(header + tail)
    data = bytes(tail[:data_length])
    received_crc = int.from_bytes(tail[data_length : data_length + 2], "little")
    calculated_crc = crc16_modbus(raw[:-2])
    checksum_ok = received_crc == calculated_crc
    if not checksum_ok:
        raise JbdHvProtocolError(
            "CRC mismatch: "
            f"received 0x{received_crc:04X}, calculated 0x{calculated_crc:04X}, "
            f"raw={raw.hex(' ')}"
        )

    return HvRegisterFrame(
        address=address,
        function=function,
        start=start,
        end=end,
        data=data,
        raw=raw,
        checksum_ok=checksum_ok,
    )


def read_range(
    ser: serial.Serial,
    start: int,
    end: int,
    *,
    response_timeout: float,
    function: int = HV_READ_FUNCTION,
) -> HvRegisterFrame:
    ser.reset_input_buffer()
    request = build_read_request(start, end, function=function)
    ser.write(request)
    return read_register_frame(ser, timeout=response_timeout, expected_function=function)


def u16(frame: HvRegisterFrame, address: int) -> int | None:
    offset = address - frame.start
    if offset < 0 or offset + 2 > len(frame.data):
        return None
    return int.from_bytes(frame.data[offset : offset + 2], "big", signed=False)


def u32(frame: HvRegisterFrame, address: int) -> int | None:
    high = u16(frame, address)
    low = u16(frame, address + 2)
    if high is None or low is None:
        return None
    return (high << 16) | low


def raw_bytes(frame: HvRegisterFrame, address: int, length: int) -> bytes:
    offset = address - frame.start
    if offset < 0 or offset + length > len(frame.data):
        return b""
    return frame.data[offset : offset + length]


def scaled_u16(
    frame: HvRegisterFrame,
    address: int,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    decimals: int = 1,
) -> float | None:
    value = u16(frame, address)
    if value is None:
        return None
    return round(value * scale + offset, decimals)


def decode_temperature(raw: int | None) -> float | None:
    if raw is None:
        return None
    return round(raw * 0.1 - 50.0, 1)


def decode_fw(value: int | None) -> str | None:
    if value is None:
        return None
    major = (value >> 8) & 0xFF
    minor = value & 0xFF
    return f"V{major}.{minor}"


def decode_sn(data: bytes) -> str:
    if not data:
        return ""
    return data.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def decode_alarm_bits(*words: int | None) -> int:
    value = 0
    shift = 0
    for word in words:
        if word is not None:
            value |= (word & 0xFFFF) << shift
        shift += 16
    return value


def decode_alarm_level_bits(*words: int | None) -> int:
    value = 0
    for word in words:
        value = (value << 16) | ((word or 0) & 0xFFFF)
    return value


def merge_alarm_level_words(*words: int | None) -> int:
    value = 0
    for index in range(8):
        level = 0
        for word in words:
            level = max(level, ((word or 0) >> (index * 2)) & 0x03)
        value |= level << (index * 2)
    return value


def decode_fault_bits(high_word: int | None, low_word: int | None) -> int:
    if high_word is None and low_word is None:
        return 0
    high_word = high_word or 0
    low_word = low_word or 0
    if high_word and low_word == 0:
        return high_word
    return (high_word << 16) | low_word


def decode_error_codes(sample: BmsSample) -> list[str]:
    errors: list[str] = []

    alarm_status = sample.alarm_status or 0
    fault_status = sample.fault_status or 0
    # HVBCUM01 可能在故障解除後仍保留告警/保護字，
    # 因此用故障字作為顯示條件，避免顯示過期的上位機錯誤。
    if fault_status:
        for index, name in enumerate(ALARM_2BIT_FIELDS):
            level = (alarm_status >> (index * 2)) & 0x03
            if level:
                errors.append(f"A{index:02d}-L{level}-{name}")

    for bit, name in FAULT_BITS.items():
        if fault_status & (1 << bit):
            errors.append(name)

    for bit in range(9, 32):
        if fault_status & (1 << bit):
            errors.append(f"E{bit:02d}-Reserved Fault Bit")

    return errors


def decode_main_frame(frame: HvRegisterFrame) -> dict[str, Any]:
    current_raw = u32(frame, 0x1004)
    current_a = None
    if current_raw is not None:
        current_a = round(current_raw * 0.1 - 3000.0, 1)

    temps = []
    for address in HV_PDU_TEMP_ADDRESSES:
        temp = decode_temperature(u16(frame, address))
        if temp is not None:
            temps.append(temp)

    alarm_status = u16(frame, 0x101C) or 0
    fault_status = decode_fault_bits(u16(frame, 0x101E), u16(frame, 0x1020))

    return {
        "voltage_v": scaled_u16(frame, 0x1000, scale=0.1, decimals=1),
        "current_a": current_a,
        "soc_percent": scaled_u16(frame, 0x1008, scale=0.01, decimals=2),
        "remaining_capacity_ah": scaled_u16(frame, 0x100A, scale=0.1, decimals=1),
        "full_capacity_ah": scaled_u16(frame, 0x100C, scale=0.1, decimals=1),
        "cycle_count": u16(frame, 0x1026),
        "charge_state": u16(frame, 0x1012),
        "software_version": decode_fw(u16(frame, 0x1044)),
        "serial_number": decode_sn(raw_bytes(frame, 0x1046, 30)),
        "alarm_status": alarm_status,
        "fault_status": fault_status,
        "protection_status": alarm_status & 0xFFFFFFFF,
        "fet_status": u16(frame, 0x1022),
        "temperatures_c": temps,
    }


def decode_config_frame(frame: HvRegisterFrame) -> dict[str, Any]:
    pack_count = u16(frame, 0x2200)
    cells_per_pack = u16(frame, 0x220C) or u16(frame, 0x2202)
    total_cells = u16(frame, 0x2202)
    ntc_count = u16(frame, 0x2204)

    if cells_per_pack in (None, 0) and pack_count:
        cells_per_pack = HV_DEFAULT_CELLS_PER_PACK
    if pack_count in (None, 0) and total_cells and cells_per_pack:
        pack_count = total_cells // cells_per_pack
    if total_cells in (None, 0) and pack_count and cells_per_pack:
        total_cells = pack_count * cells_per_pack

    return {
        "configured_pack_count": pack_count,
        "cells_per_pack": cells_per_pack,
        "total_cell_count": total_cells,
        "ntc_count": ntc_count,
    }


def decode_statistics_frame(frame: HvRegisterFrame) -> dict[str, Any]:
    current_raw = u16(frame, 0x4002)
    current_a = None if current_raw is None else round(current_raw * 0.1 - 3000.0, 1)
    alarm_status = merge_alarm_level_words(
        u16(frame, 0x400E),
        u16(frame, 0x4010),
        u16(frame, 0x4012),
        u16(frame, 0x4014),
    )
    return {
        "voltage_v": scaled_u16(frame, 0x4000, scale=0.1, decimals=1),
        "current_a": current_a,
        "soc_percent": scaled_u16(frame, 0x4004, scale=1.0, decimals=1),
        "cycle_count": u16(frame, 0x401A),
        "alarm_status": alarm_status,
        "protection_status": alarm_status & 0xFFFFFFFF,
    }


def decode_bmu_cell_voltages(
    frame: HvRegisterFrame,
    *,
    cells_per_pack: int | None,
) -> list[int | None]:
    words = [
        int.from_bytes(frame.data[offset : offset + 2], "big")
        for offset in range(0, len(frame.data) - 1, 2)
    ]
    if len(words) < 15:
        raise JbdHvProtocolError(f"BMU voltage payload too short: {len(frame.data)} bytes")

    cell_count = words[14]
    if cell_count <= 0 or cell_count > 64:
        raise JbdHvProtocolError(f"Unreasonable BMU cell count: {cell_count}")
    if len(words) < 15 + cell_count:
        raise JbdHvProtocolError(
            f"BMU voltage payload has {len(words) - 15} cells, expected {cell_count}"
        )

    voltages: list[int | None] = []
    limit = cell_count
    if cells_per_pack is not None and cells_per_pack > 0:
        limit = min(limit, cells_per_pack)

    for raw in words[15 : 15 + limit]:
        offline = bool(raw & 0x8000)
        voltage = raw & 0x1FFF
        undetected = voltage == HV_UNDETECTED_CELL_MV
        voltages.append(None if offline or undetected else voltage)
    return voltages


def decode_bmu_cell_balance_states(
    frame: HvRegisterFrame,
    *,
    cells_per_pack: int | None,
) -> list[int | None]:
    words = [
        int.from_bytes(frame.data[offset : offset + 2], "big")
        for offset in range(0, len(frame.data) - 1, 2)
    ]
    if len(words) < 15:
        return []
    cell_count = words[14]
    if cell_count <= 0 or cell_count > 64:
        return []
    limit = cell_count
    if cells_per_pack is not None and cells_per_pack > 0:
        limit = min(limit, cells_per_pack)
    states: list[int | None] = []
    for raw in words[15 : 15 + limit]:
        if raw & 0x8000:
            states.append(None)
        else:
            states.append((raw >> 13) & 0x03)
    return states


def decode_bmu_temperatures(
    frame: HvRegisterFrame,
    *,
    cells_per_pack: int | None,
) -> list[float]:
    words = [
        int.from_bytes(frame.data[offset : offset + 2], "big")
        for offset in range(0, len(frame.data) - 1, 2)
    ]
    if len(words) < 16:
        return []

    cell_count = words[14]
    if cell_count <= 0 or cell_count > 64:
        return []
    if cells_per_pack is not None and cells_per_pack > 0:
        cell_count = min(cell_count, cells_per_pack)

    temp_count_index = 15 + cell_count
    if temp_count_index >= len(words):
        return []
    temp_count = words[temp_count_index]
    if temp_count <= 0 or temp_count > 32:
        return []

    temperatures: list[float] = []
    start = temp_count_index + 1
    limit = min(temp_count, HV_BMU_TEMPS_PER_PACK, len(words) - start)
    for raw in words[start : start + limit]:
        temp = decode_temperature(raw)
        if temp is not None:
            temperatures.append(temp)
    return temperatures


def decode_temperature_threshold_frame(
    frame: HvRegisterFrame,
    *,
    group_name: str,
) -> list[tuple[float, str, str]]:
    words = [
        int.from_bytes(frame.data[offset : offset + 2], "big")
        for offset in range(0, len(frame.data) - 1, 2)
    ]
    lines: list[tuple[float, str, str]] = []
    for level, word_index in enumerate((0, 4, 8), start=1):
        if word_index >= len(words):
            continue
        value = decode_temperature(words[word_index])
        if value is None:
            continue
        lines.append((value, TEMP_LEVEL_COLORS[level], f"{group_name} L{level}"))
    return lines


def read_temperature_warning_lines(
    ser: serial.Serial,
    *,
    response_timeout: float,
) -> list[tuple[float, str, str]]:
    lines: list[tuple[float, str, str]] = []
    seen: set[tuple[float, str]] = set()
    for group_name, start in TEMP_THRESHOLD_GROUPS:
        try:
            time.sleep(0.05)
            frame = read_range(ser, start, start + 0x17, response_timeout=response_timeout)
        except (TimeoutError, JbdHvProtocolError, serial.SerialException):
            continue
        for value, color, label in decode_temperature_threshold_frame(frame, group_name=group_name):
            key = (value, color)
            if key in seen:
                continue
            seen.add(key)
            lines.append((value, color, label))
    return lines


def decode_voltage_threshold_frame(
    frame: HvRegisterFrame,
    *,
    group_name: str,
    scale: float,
) -> list[tuple[float, str, str]]:
    words = [
        int.from_bytes(frame.data[offset : offset + 2], "big")
        for offset in range(0, len(frame.data) - 1, 2)
    ]
    lines: list[tuple[float, str, str]] = []
    for level, word_index in enumerate((0, 4, 8), start=1):
        if word_index >= len(words):
            continue
        value = round(words[word_index] * scale, 3)
        lines.append((value, TEMP_LEVEL_COLORS[level], f"{group_name} L{level}"))
    return lines


def read_voltage_warning_lines(
    ser: serial.Serial,
    groups: list[tuple[str, int, float]],
    *,
    response_timeout: float,
) -> list[tuple[float, str, str]]:
    lines: list[tuple[float, str, str]] = []
    seen: set[tuple[float, str]] = set()
    for group_name, start, scale in groups:
        try:
            time.sleep(0.05)
            frame = read_range(ser, start, start + 0x17, response_timeout=response_timeout)
        except (TimeoutError, JbdHvProtocolError, serial.SerialException):
            continue
        for value, color, label in decode_voltage_threshold_frame(
            frame,
            group_name=group_name,
            scale=scale,
        ):
            key = (value, color)
            if key in seen:
                continue
            seen.add(key)
            lines.append((value, color, label))
    return lines


def read_cell_voltages(
    ser: serial.Serial,
    *,
    pack_count: int,
    cells_per_pack: int,
    response_timeout: float,
) -> tuple[list[int | None], list[int | None], list[float], str]:
    voltages: list[int | None] = []
    balance_states: list[int | None] = []
    temperatures: list[float] = []
    raw_frames: list[str] = []

    for pack_index in range(pack_count):
        base = 0x5000 + pack_index * 0x0200
        frame: HvRegisterFrame | None = None
        for attempt in range(2):
            try:
                time.sleep(0.15)
                frame = read_range(ser, base, base + 0x003B, response_timeout=response_timeout)
                break
            except (TimeoutError, JbdHvProtocolError, serial.SerialException):
                if attempt:
                    frame = None
        if frame is None:
            continue
        raw_frames.append(frame.raw.hex(" "))
        voltages.extend(decode_bmu_cell_voltages(frame, cells_per_pack=cells_per_pack))
        balance_states.extend(decode_bmu_cell_balance_states(frame, cells_per_pack=cells_per_pack))
        temperatures.extend(decode_bmu_temperatures(frame, cells_per_pack=cells_per_pack))

    expected_total = pack_count * cells_per_pack
    expected_temps = pack_count * HV_BMU_TEMPS_PER_PACK
    return (
        voltages[:expected_total],
        balance_states[:expected_total],
        temperatures[:expected_temps],
        " | ".join(raw_frames),
    )


def charge_discharge_state(sample: BmsSample) -> str:
    if sample.charge_state in CHARGE_STATES:
        return CHARGE_STATES[sample.charge_state]
    if sample.current_a is None:
        return "unknown"
    if sample.current_a > 0.05:
        return "charging"
    if sample.current_a < -0.05:
        return "discharging"
    return "standby"


def poll_hv_bms(
    ser: serial.Serial,
    *,
    response_timeout: float,
    max_packs: int,
    pack_count_override: int | None = None,
    cells_per_pack_override: int | None = None,
) -> BmsSample:
    sample = BmsSample(timestamp=dt.datetime.now().isoformat(timespec="seconds"))

    main = read_range(ser, 0x1000, 0x107D, response_timeout=response_timeout)
    sample.basic_checksum_ok = main.checksum_ok
    sample.basic_raw = main.raw.hex(" ")
    for key, value in decode_main_frame(main).items():
        setattr(sample, key, value)

    for attempt in range(2):
        try:
            time.sleep(0.2)
            config = read_range(ser, 0x2200, 0x2211, response_timeout=response_timeout)
            sample.config_raw = config.raw.hex(" ")
            sample.cells_raw = sample.config_raw
            sample.cells_checksum_ok = config.checksum_ok
            for key, value in decode_config_frame(config).items():
                setattr(sample, key, value)
            break
        except (TimeoutError, JbdHvProtocolError, serial.SerialException):
            sample.cells_checksum_ok = False
            if attempt:
                break

    if pack_count_override is not None:
        if pack_count_override < 1 or pack_count_override > max_packs:
            raise ValueError(f"pack_count must be between 1 and {max_packs}")
        sample.configured_pack_count = pack_count_override
    if cells_per_pack_override is not None:
        sample.cells_per_pack = cells_per_pack_override

    if sample.cells_per_pack in (None, 0):
        sample.cells_per_pack = HV_DEFAULT_CELLS_PER_PACK
    if sample.configured_pack_count and sample.configured_pack_count > max_packs:
        sample.configured_pack_count = max_packs
    if sample.configured_pack_count and sample.cells_per_pack:
        sample.total_cell_count = sample.configured_pack_count * sample.cells_per_pack

    if sample.configured_pack_count and sample.cells_per_pack:
        try:
            bmu_temperatures: list[float]
            sample.cell_voltages_mv, sample.cell_balance_states, bmu_temperatures, sample.cells_raw = read_cell_voltages(
                ser,
                pack_count=sample.configured_pack_count,
                cells_per_pack=sample.cells_per_pack,
                response_timeout=response_timeout,
            )
            sample.temperatures_c.extend(bmu_temperatures)
            sample.ntc_count = len(sample.temperatures_c)
        except (TimeoutError, JbdHvProtocolError, serial.SerialException):
            pass

    sample.temperature_warning_lines = read_temperature_warning_lines(
        ser,
        response_timeout=response_timeout,
    )
    sample.cell_voltage_warning_lines = read_voltage_warning_lines(
        ser,
        CELL_VOLTAGE_THRESHOLD_GROUPS,
        response_timeout=response_timeout,
    )
    sample.total_voltage_warning_lines_v = read_voltage_warning_lines(
        ser,
        TOTAL_VOLTAGE_THRESHOLD_GROUPS,
        response_timeout=response_timeout,
    )

    return sample
