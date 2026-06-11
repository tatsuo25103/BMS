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
CID2_PARAM_CELL_OV = 0xD1
CID2_PARAM_CELL_UV = 0xD3
CID2_PARAM_TOTAL_OV = 0xD5
CID2_PARAM_TOTAL_UV = 0xD7
CID2_PARAM_CHARGE_OC = 0xD9
CID2_PARAM_DISCHARGE_OC_1 = 0xDB
CID2_PARAM_DISCHARGE_OC_2 = 0xE3
CID2_PARAM_SHORT_CIRCUIT = 0xE5
CID2_PARAM_BALANCE = 0xB6
CID2_PARAM_SLEEP = 0xA0
CID2_PARAM_FULL_CHARGE = 0xAF
CID2_PARAM_TEMP_HIGH = 0xDD
CID2_PARAM_MOS_TEMP = 0xE1
CID2_PARAM_ENV_TEMP = 0xE7
CID2_PARAM_TEMP_LOW = 0xDF

PACE_PARAMETER_COMMANDS = (
    CID2_PARAM_CELL_OV,
    CID2_PARAM_TOTAL_OV,
    CID2_PARAM_CELL_UV,
    CID2_PARAM_TOTAL_UV,
    CID2_PARAM_CHARGE_OC,
    CID2_PARAM_DISCHARGE_OC_1,
    CID2_PARAM_DISCHARGE_OC_2,
    CID2_PARAM_SHORT_CIRCUIT,
    CID2_PARAM_BALANCE,
    CID2_PARAM_SLEEP,
    CID2_PARAM_FULL_CHARGE,
    CID2_PARAM_TEMP_HIGH,
    CID2_PARAM_MOS_TEMP,
    CID2_PARAM_ENV_TEMP,
    CID2_PARAM_TEMP_LOW,
)

PS5120E_DEFAULT_TEMPERATURE_WARNING_LINES = [
    (-20.0, "#3b82f6", "PS5120E default UT -20 C"),
    (50.0, "#ff7a1a", "PS5120E default OT 50 C"),
    (70.0, "#ef4444", "PS5120E default OT 70 C"),
]
PS5120E_DEFAULT_CELL_VOLTAGE_WARNING_LINES = [
    (2500.0, "#ef4444", "PS5120E default UV 2500 mV"),
    (3650.0, "#ef4444", "PS5120E default OV 3650 mV"),
]
PS5120E_DEFAULT_TOTAL_VOLTAGE_WARNING_LINES = [
    (40.0, "#ef4444", "PS5120E default UV 40.0 V"),
    (58.4, "#ef4444", "PS5120E default OV 58.4 V"),
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


def _parameter(group: str, name: str, value: float | int | str, unit: str = "") -> dict[str, str]:
    if isinstance(value, float):
        value_text = f"{value:g}"
    else:
        value_text = str(value)
    return {
        "group": group,
        "name": name,
        "value": value_text,
        "unit": unit,
        "status": "ok",
    }


def _failed_parameters(group: str, names: list[tuple[str, str]], error: str) -> list[dict[str, str]]:
    return [
        {
            "group": group,
            "name": name,
            "value": "--",
            "unit": unit,
            "status": error,
        }
        for name, unit in names
    ]


def _u16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise PaceProtocolError("PACE parameter response ended early")
    return int.from_bytes(data[offset : offset + 2], "big")


def _s16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise PaceProtocolError("PACE parameter response ended early")
    return int.from_bytes(data[offset : offset + 2], "big", signed=True)


def _temp_c(raw_value: int) -> float:
    return round(raw_value / 10.0 - 273.0, 1)


def decode_parameter_info(command: int, info: bytes, *, cells_per_pack: int = PACE_CELLS_PER_PACK) -> list[dict[str, str]]:
    if command in {
        CID2_PARAM_CELL_OV,
        CID2_PARAM_TOTAL_OV,
        CID2_PARAM_CELL_UV,
        CID2_PARAM_TOTAL_UV,
    }:
        if len(info) < 8:
            raise PaceProtocolError(f"PACE parameter 0x{command:02X} response is too short")
        protect = _u16(info, 1)
        alarm = _u16(info, 3)
        release = _u16(info, 5)
        delay_ms = info[7] * 100
        is_pack = command in {CID2_PARAM_TOTAL_OV, CID2_PARAM_TOTAL_UV}
        is_over = command in {CID2_PARAM_CELL_OV, CID2_PARAM_TOTAL_OV}
        prefix = "Pack" if is_pack else "Cell"
        protection = "OV" if is_over else "UV"
        group = f"{prefix} {'Over' if is_over else 'Under'} Voltage"
        if is_pack:
            alarm_value = round(alarm / 1000.0, 3)
            protect_value = round(protect / 1000.0, 3)
            release_value = round(release / 1000.0, 3)
        else:
            alarm_value = round(alarm / 1000.0, 3)
            protect_value = round(protect / 1000.0, 3)
            release_value = round(release / 1000.0, 3)
        return [
            _parameter(group, f"{prefix} {protection} Alarm", alarm_value, "V"),
            _parameter(group, f"{prefix} {protection} Protect", protect_value, "V"),
            _parameter(group, f"{prefix} {protection}P Release", release_value, "V"),
            _parameter(group, f"{prefix} {protection}P Delay Time", delay_ms, "ms"),
        ]

    if command in {CID2_PARAM_CHARGE_OC, CID2_PARAM_DISCHARGE_OC_1}:
        if len(info) < 6:
            raise PaceProtocolError(f"PACE parameter 0x{command:02X} response is too short")
        protect = abs(_s16(info, 1))
        alarm = abs(_s16(info, 3))
        delay_ms = info[5] * 100
        if command == CID2_PARAM_CHARGE_OC:
            group = "Charge Overcurrent"
            return [
                _parameter(group, "CHG OC Alarm", alarm, "A"),
                _parameter(group, "CHG OC Protect", protect, "A"),
                _parameter(group, "CHG OCP Delay Time", delay_ms, "ms"),
            ]
        group = "Discharge Overcurrent"
        return [
            _parameter(group, "DSG OC Alarm", alarm, "A"),
            _parameter(group, "DSG OC 1 Protect", protect, "A"),
            _parameter(group, "DSG OCP 1 Delay Time", delay_ms, "ms"),
        ]

    if command == CID2_PARAM_DISCHARGE_OC_2:
        if len(info) < 3:
            raise PaceProtocolError("PACE discharge overcurrent 2 response is too short")
        ranges: list[tuple[int, int]] = []
        for offset in range(0, len(info) - 2, 3):
            ranges.append((abs(_s16(info, offset)), info[offset + 2] * 100))
        protect_values = "/".join(str(value) for value, _delay in ranges)
        delay_values = "/".join(str(delay) for _value, delay in ranges)
        group = "Discharge Overcurrent"
        return [
            _parameter(group, "DSG OC 2 Protect", protect_values, "A"),
            _parameter(group, "DSG OCP 2 Delay Time", delay_values, "ms"),
        ]

    if command == CID2_PARAM_SHORT_CIRCUIT:
        if not info:
            raise PaceProtocolError("PACE short circuit response is empty")
        return [_parameter("Discharge Overcurrent", "SCP Delay Time", info[-1] * 25, "us")]

    if command == CID2_PARAM_BALANCE:
        if len(info) < 4:
            raise PaceProtocolError("PACE balance response is too short")
        return [
            _parameter("Balance / Sleep", "Balance Threshold", _u16(info, 0) / 1000.0, "V"),
            _parameter("Balance / Sleep", "Balance Delta Vcell", _u16(info, 2), "mV"),
        ]

    if command == CID2_PARAM_SLEEP:
        if len(info) < 4:
            raise PaceProtocolError("PACE sleep response is too short")
        return [
            _parameter("Balance / Sleep", "Sleep Vcell", _u16(info, 0) / 1000.0, "V"),
            _parameter("Balance / Sleep", "Delay Time", _u16(info, 2), "min"),
        ]

    if command == CID2_PARAM_FULL_CHARGE:
        if len(info) < 5:
            raise PaceProtocolError("PACE full charge response is too short")
        return [
            _parameter("Full Charge / SOC", "Pack FullCharge Voltage", _u16(info, 0) / 1000.0, "V"),
            _parameter("Full Charge / SOC", "Pack FullCharge Current", _u16(info, 2), "mA"),
            _parameter("Full Charge / SOC", "SOC Low Alarm", info[4], "%"),
        ]

    if command in {CID2_PARAM_TEMP_HIGH, CID2_PARAM_TEMP_LOW, CID2_PARAM_MOS_TEMP, CID2_PARAM_ENV_TEMP}:
        payload = info[1:] if info and info[0] in (0x00, 0x01) else info
        values = [_temp_c(_u16(payload, offset)) for offset in range(0, len(payload) - 1, 2)]
        if command == CID2_PARAM_TEMP_HIGH and len(values) >= 6:
            group = "High Temperature"
            return [
                _parameter(group, "CHG OT Alarm", values[1], "C"),
                _parameter(group, "CHG OT Protect", values[0], "C"),
                _parameter(group, "CHG OTP Release", values[2], "C"),
                _parameter(group, "DSG OT Alarm", values[4], "C"),
                _parameter(group, "DSG OT Protect", values[3], "C"),
                _parameter(group, "DSG OTP Release", values[5], "C"),
            ]
        if command == CID2_PARAM_TEMP_LOW and len(values) >= 6:
            group = "Low Temperature"
            return [
                _parameter(group, "CHG UT Alarm", values[1], "C"),
                _parameter(group, "CHG UT Protect", values[0], "C"),
                _parameter(group, "CHG UTP Release", values[2], "C"),
                _parameter(group, "DSG UT Alarm", values[4], "C"),
                _parameter(group, "DSG UT Protect", values[3], "C"),
                _parameter(group, "DSG UTP Release", values[5], "C"),
            ]
        if command == CID2_PARAM_MOS_TEMP and len(values) >= 3:
            group = "MOS Temperature"
            return [
                _parameter(group, "MOS OT Alarm", values[1], "C"),
                _parameter(group, "MOS OT Protect", values[0], "C"),
                _parameter(group, "MOS OTP Release", values[2], "C"),
            ]
        if command == CID2_PARAM_ENV_TEMP and len(values) >= 6:
            group = "Environment Temperature"
            return [
                _parameter(group, "ENV UT Alarm", values[1], "C"),
                _parameter(group, "ENV UT Protect", values[0], "C"),
                _parameter(group, "ENV UTP Release", values[2], "C"),
                _parameter(group, "ENV OT Alarm", values[4], "C"),
                _parameter(group, "ENV OT Protect", values[3], "C"),
                _parameter(group, "ENV OTP Release", values[5], "C"),
            ]

    raise PaceProtocolError(f"Unsupported or incomplete PACE parameter response: 0x{command:02X}")


def parameter_names_for_command(command: int) -> tuple[str, list[tuple[str, str]]]:
    definitions = {
        CID2_PARAM_CELL_OV: ("Cell Over Voltage", [("Cell OV Alarm", "V"), ("Cell OV Protect", "V"), ("Cell OVP Release", "V"), ("Cell OVP Delay Time", "ms")]),
        CID2_PARAM_TOTAL_OV: ("Pack Over Voltage", [("Pack OV Alarm", "V"), ("Pack OV Protect", "V"), ("Pack OVP Release", "V"), ("Pack OVP Delay Time", "ms")]),
        CID2_PARAM_CELL_UV: ("Cell Under Voltage", [("Cell UV Alarm", "V"), ("Cell UV Protect", "V"), ("Cell UVP Release", "V"), ("Cell UVP Delay Time", "ms")]),
        CID2_PARAM_TOTAL_UV: ("Pack Under Voltage", [("Pack UV Alarm", "V"), ("Pack UV Protect", "V"), ("Pack UVP Release", "V"), ("Pack UVP Delay Time", "ms")]),
        CID2_PARAM_CHARGE_OC: ("Charge Overcurrent", [("CHG OC Alarm", "A"), ("CHG OC Protect", "A"), ("CHG OCP Delay Time", "ms")]),
        CID2_PARAM_DISCHARGE_OC_1: ("Discharge Overcurrent", [("DSG OC Alarm", "A"), ("DSG OC 1 Protect", "A"), ("DSG OCP 1 Delay Time", "ms")]),
        CID2_PARAM_DISCHARGE_OC_2: ("Discharge Overcurrent", [("DSG OC 2 Protect", "A"), ("DSG OCP 2 Delay Time", "ms")]),
        CID2_PARAM_SHORT_CIRCUIT: ("Discharge Overcurrent", [("SCP Delay Time", "us")]),
        CID2_PARAM_BALANCE: ("Balance / Sleep", [("Balance Threshold", "V"), ("Balance Delta Vcell", "mV")]),
        CID2_PARAM_SLEEP: ("Balance / Sleep", [("Sleep Vcell", "V"), ("Delay Time", "min")]),
        CID2_PARAM_FULL_CHARGE: ("Full Charge / SOC", [("Pack FullCharge Voltage", "V"), ("Pack FullCharge Current", "mA"), ("SOC Low Alarm", "%")]),
        CID2_PARAM_TEMP_HIGH: ("High Temperature", [("CHG OT Alarm", "C"), ("CHG OT Protect", "C"), ("CHG OTP Release", "C"), ("DSG OT Alarm", "C"), ("DSG OT Protect", "C"), ("DSG OTP Release", "C")]),
        CID2_PARAM_MOS_TEMP: ("MOS Temperature", [("MOS OT Alarm", "C"), ("MOS OT Protect", "C"), ("MOS OTP Release", "C")]),
        CID2_PARAM_ENV_TEMP: ("Environment Temperature", [("ENV UT Alarm", "C"), ("ENV UT Protect", "C"), ("ENV UTP Release", "C"), ("ENV OT Alarm", "C"), ("ENV OT Protect", "C"), ("ENV OTP Release", "C")]),
        CID2_PARAM_TEMP_LOW: ("Low Temperature", [("CHG UT Alarm", "C"), ("CHG UT Protect", "C"), ("CHG UTP Release", "C"), ("DSG UT Alarm", "C"), ("DSG UT Protect", "C"), ("DSG UTP Release", "C")]),
    }
    return definitions[command]


def read_bms_parameters(
    ser: serial.Serial,
    *,
    response_timeout: float,
    cells_per_pack: int = PACE_CELLS_PER_PACK,
) -> tuple[list[dict[str, str]], list[str], str]:
    parameters: list[dict[str, str]] = []
    errors: list[str] = []
    raw_parts: list[str] = []
    for command in PACE_PARAMETER_COMMANDS:
        group, names = parameter_names_for_command(command)
        try:
            frame = send_command(
                ser,
                command,
                response_timeout=response_timeout,
                versions=(PACE_REQUEST_VERSION,),
            )
            raw_parts.append(f"0x{command:02X}={frame.info.hex().upper()}")
            parameters.extend(
                decode_parameter_info(command, frame.info, cells_per_pack=cells_per_pack)
            )
        except (TimeoutError, PaceProtocolError, serial.SerialException) as exc:
            error = f"0x{command:02X} {group}: {exc}"
            errors.append(error)
            parameters.extend(_failed_parameters(group, names, str(exc)))
    return parameters, errors, "; ".join(raw_parts)


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

    if len(text) < 16:
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


def read_threshold_settings(ser: serial.Serial, *, response_timeout: float) -> tuple[
    list[tuple[float, str, str]],
    list[tuple[float, str, str]],
    list[tuple[float, str, str]],
    str,
]:
    # PACE 公開 RS232 文件未列出參數讀取命令，這組 CID2 來自官方上位機 ParamsRead 按鈕。
    raw_parts: list[str] = []

    def read_param(command: int) -> bytes:
        frame = send_command(ser, command, response_timeout=response_timeout, versions=(PACE_REQUEST_VERSION,))
        raw_parts.append(f"0x{command:02X}={frame.info.hex().upper()}")
        return frame.info

    def u16_values(info: bytes) -> list[int]:
        payload = info[1:] if info and info[0] in (0x00, 0x01) else info
        if len(payload) % 2:
            payload = payload[:-1]
        return [int.from_bytes(payload[index : index + 2], "big") for index in range(0, len(payload), 2)]

    def temp_c(raw_value: int) -> float:
        return round(raw_value / 10.0 - 273.0, 1)

    try:
        cell_ov = u16_values(read_param(CID2_PARAM_CELL_OV))
        cell_uv = u16_values(read_param(CID2_PARAM_CELL_UV))
        total_ov = u16_values(read_param(CID2_PARAM_TOTAL_OV))
        total_uv = u16_values(read_param(CID2_PARAM_TOTAL_UV))
        temp_high = [temp_c(value) for value in u16_values(read_param(CID2_PARAM_TEMP_HIGH))]
        temp_low = [temp_c(value) for value in u16_values(read_param(CID2_PARAM_TEMP_LOW))]
        temp_mixed = [temp_c(value) for value in u16_values(read_param(CID2_PARAM_ENV_TEMP))]
    except (TimeoutError, PaceProtocolError, serial.SerialException) as exc:
        return [], [], [], f"PACE threshold read failed: {exc}"

    cell_lines: list[tuple[float, str, str]] = []
    total_lines: list[tuple[float, str, str]] = []
    temp_lines: list[tuple[float, str, str]] = []

    if len(cell_uv) >= 2:
        cell_lines.append((float(min(cell_uv[:2])), "#ef4444", f"PACE BMS UV min {min(cell_uv[:2])} mV"))
    if len(cell_ov) >= 2:
        cell_lines.append((float(min(cell_ov[:2])), "#ff7a1a", f"PACE BMS OV min {min(cell_ov[:2])} mV"))

    if len(total_uv) >= 2:
        uv_v = round(min(total_uv[:2]) / 1000.0, 3)
        total_lines.append((uv_v, "#ef4444", f"PACE BMS UV min {uv_v:g} V"))
    if len(total_ov) >= 2:
        ov_v = round(min(total_ov[:2]) / 1000.0, 3)
        total_lines.append((ov_v, "#ff7a1a", f"PACE BMS OV min {ov_v:g} V"))

    over_temp_candidates = temp_high + [value for value in temp_mixed if value >= 30.0]
    under_temp_candidates = temp_low + [value for value in temp_mixed if value <= 10.0]
    if under_temp_candidates:
        temp_lines.append((min(under_temp_candidates), "#3b82f6", f"PACE BMS UT low {min(under_temp_candidates):g} C"))
        high_ut = max(under_temp_candidates)
        if high_ut != min(under_temp_candidates):
            temp_lines.append((high_ut, "#3b82f6", f"PACE BMS UT high {high_ut:g} C"))
    if over_temp_candidates:
        temp_lines.append((min(over_temp_candidates), "#ff7a1a", f"PACE BMS OT low {min(over_temp_candidates):g} C"))
        high_ot = max(over_temp_candidates)
        if high_ot != min(over_temp_candidates):
            temp_lines.append((high_ot, "#ef4444", f"PACE BMS OT high {high_ot:g} C"))

    raw_parts.append("source=PACE official ParamsRead")
    return temp_lines, cell_lines, total_lines, "; ".join(raw_parts)


def default_threshold_settings() -> tuple[
    list[tuple[float, str, str]],
    list[tuple[float, str, str]],
    list[tuple[float, str, str]],
]:
    # PACE 文件未提供門檻讀取命令時，使用 PS5120E 既有預設值作為警示線。
    return (
        PS5120E_DEFAULT_TEMPERATURE_WARNING_LINES.copy(),
        PS5120E_DEFAULT_CELL_VOLTAGE_WARNING_LINES.copy(),
        PS5120E_DEFAULT_TOTAL_VOLTAGE_WARNING_LINES.copy(),
    )


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
    full_capacity_ah: float | None = None
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
            if trailing_bytes >= 2 and full_capacity_ah is None:
                full_capacity_ah = round(
                    int.from_bytes(info[offset : offset + 2], "big") * 0.01,
                    2,
                )
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
        "full_capacity_ah": full_capacity_ah,
        "cycle_count": cycle_count,
        "pack_voltages_v": pack_voltages_v,
    }


def poll_ps5120_bms(
    ser: serial.Serial,
    *,
    response_timeout: float,
    max_packs: int = 30,
    include_static: bool = True,
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
    if include_static:
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

    if include_static and sample.full_capacity_ah is None:
        remaining, full, _design, raw = read_capacity_info(
            ser,
            response_timeout=response_timeout,
        )
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
    if include_static:
        (
            sample.temperature_warning_lines,
            sample.cell_voltage_warning_lines,
            sample.total_voltage_warning_lines_v,
            thresholds_raw,
        ) = read_threshold_settings(ser, response_timeout=response_timeout)
        sample.config_raw = thresholds_raw
        if (
            not sample.temperature_warning_lines
            and not sample.cell_voltage_warning_lines
            and not sample.total_voltage_warning_lines_v
        ):
            (
                sample.temperature_warning_lines,
                sample.cell_voltage_warning_lines,
                sample.total_voltage_warning_lines_v,
            ) = default_threshold_settings()
            thresholds_raw = f"{thresholds_raw}; using PS5120E defaults"
            sample.config_raw = thresholds_raw
        raw_parts.append(f"thresholds={thresholds_raw}")
    sample.stats_raw = " | ".join(raw_parts)
    return sample
