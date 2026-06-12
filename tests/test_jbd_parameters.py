import unittest

from jbd_hv_protocol import (
    HV_PARAMETER_FIELDS,
    HvRegisterFrame,
    decode_discharge_overcurrent_parameters,
    decode_parameter_alarm_group,
)


def _frame(start: int, words: list[int]) -> HvRegisterFrame:
    data = b"".join(word.to_bytes(2, "big") for word in words)
    return HvRegisterFrame(
        address=1,
        function=0x78,
        start=start,
        end=start + len(data) - 1,
        data=data,
        raw=b"",
        checksum_ok=True,
    )


class JbdParameterTests(unittest.TestCase):
    def test_hv_parameter_page_matches_full_factory_template(self) -> None:
        self.assertEqual(len(HV_PARAMETER_FIELDS), 165)
        self.assertEqual(
            len({(group, name) for group, name, _address, _type in HV_PARAMETER_FIELDS}),
            165,
        )
        groups = {group for group, _name, _address, _type in HV_PARAMETER_FIELDS}
        self.assertEqual(len(groups), 18)
        self.assertIn("Voltage Difference", groups)
        self.assertIn("Temperature Difference", groups)
        self.assertIn("Temperature Rise", groups)
        self.assertIn("Terminal High Temperature", groups)
        self.assertIn("Insulation Resistance Low", groups)

    def test_key_hv_parameter_addresses(self) -> None:
        addresses = {
            (group, name): address
            for _group, name, address, _value_type in HV_PARAMETER_FIELDS
            for group in [_group]
        }
        self.assertEqual(
            addresses[("Cell Over Voltage", "L2 Release SOC")],
            0x180E,
        )
        self.assertEqual(
            addresses[("Total Under Voltage", "L3 Protect Value")],
            0x1858,
        )
        self.assertEqual(
            addresses[("Discharge Overcurrent", "L3 Recovery Delay")],
            0x18A6,
        )
        self.assertEqual(
            addresses[("Temperature Difference", "L1 Alarm Value")],
            0x1938,
        )
        self.assertEqual(
            addresses[("Insulation Resistance Low", "L3 Protect Value")],
            0x1990,
        )
        self.assertEqual(addresses[("SOC Low", "L1 Alarm Value")], 0x1998)

    def test_decode_cell_over_voltage_group(self) -> None:
        frame = _frame(
            0x1800,
            [3650, 1000, 3550, 5, 3700, 800, 3600, 5, 3750, 500, 3650, 1],
        )
        values = decode_parameter_alarm_group(
            frame,
            group="Cell Over Voltage",
            value_type="voltage_mv",
            prefix="Cell OV",
        )

        self.assertEqual(
            [(item["name"], item["value"], item["unit"]) for item in values],
            [
                ("Cell OV Alarm", "3.650", "V"),
                ("Cell OV Protect", "3.750", "V"),
                ("Cell OVP Release", "3.650", "V"),
                ("Cell OVP Delay Time", "500", "ms"),
            ],
        )

    def test_decode_temperature_group(self) -> None:
        frame = _frame(
            0x18A8,
            [1000, 1000, 950, 5, 1100, 800, 1000, 5, 1200, 500, 1100, 1],
        )
        values = decode_parameter_alarm_group(
            frame,
            group="High Temperature",
            value_type="temperature",
            prefix="CHG OT",
        )

        self.assertEqual(values[0]["value"], "50.0")
        self.assertEqual(values[1]["value"], "70.0")
        self.assertEqual(values[2]["value"], "60.0")

    def test_decode_discharge_overcurrent_levels(self) -> None:
        frame = _frame(
            0x1890,
            [1000, 1000, 900, 5, 1500, 800, 1200, 5, 2000, 500, 3, 10],
        )
        values = decode_discharge_overcurrent_parameters(frame)

        self.assertEqual(
            [(item["name"], item["value"]) for item in values],
            [
                ("DSG OC Alarm", "100.0"),
                ("DSG OC 1 Protect", "150.0"),
                ("DSG OCP 1 Delay Time", "800"),
                ("DSG OC 2 Protect", "200.0"),
                ("DSG OCP 2 Delay Time", "500"),
            ],
        )
