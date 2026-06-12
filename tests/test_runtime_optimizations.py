from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from bms_collector import (
    apply_cached_static_data,
    battery_identity,
    battery_identity_changed,
    battery_identity_label,
)
from bms_gui import (
    acquisition_rate_index,
    acquisition_rate_label,
    acquisition_rate_seconds,
    chart_display_points,
    minimum_cycle_quiet_time,
    poll_bms_with_retry,
)
from jbd_hv_protocol import BmsSample


class RuntimeOptimizationTests(unittest.TestCase):
    def test_battery_identity_prefers_model_and_serial(self) -> None:
        battery_a = BmsSample(
            timestamp="a",
            product_model="HV140",
            serial_number="SERIAL-A",
            software_version="V1.1",
            configured_pack_count=4,
            cells_per_pack=16,
        )
        battery_b = BmsSample(
            timestamp="b",
            product_model="HV140",
            serial_number="SERIAL-B",
            software_version="V1.1",
            configured_pack_count=4,
            cells_per_pack=16,
        )
        identity_a = battery_identity(battery_a)
        identity_b = battery_identity(battery_b)

        self.assertTrue(battery_identity_changed(identity_a, identity_b))
        self.assertIn("SN SERIAL-A", battery_identity_label(identity_a))

    def test_battery_identity_uses_structure_when_serial_is_missing(self) -> None:
        previous = battery_identity(
            BmsSample(
                timestamp="a",
                product_model="PS5120E",
                software_version="V2.5",
                configured_pack_count=1,
                cells_per_pack=16,
            )
        )
        current = battery_identity(
            BmsSample(
                timestamp="b",
                product_model="PS5120E",
                software_version="V2.5",
                configured_pack_count=2,
                cells_per_pack=16,
            )
        )

        self.assertTrue(battery_identity_changed(previous, current))

    def test_five_step_acquisition_rates(self) -> None:
        self.assertEqual(acquisition_rate_index(0.2), 0)
        self.assertEqual(acquisition_rate_index(1.6), 2)
        self.assertEqual(acquisition_rate_seconds(0), 0.0)
        self.assertEqual(acquisition_rate_seconds(1), 10.0)
        self.assertEqual(acquisition_rate_seconds(2), 30.0)
        self.assertEqual(acquisition_rate_seconds(3), 60.0)
        self.assertEqual(acquisition_rate_seconds(4), 300.0)
        self.assertEqual(acquisition_rate_label("Real-time"), "Real-time")
        self.assertEqual(
            acquisition_rate_label("Real-time", 2.34),
            "Real-time  last 2.3s",
        )

    def test_hv140_real_time_cycles_have_a_bus_quiet_period(self) -> None:
        self.assertEqual(minimum_cycle_quiet_time("HV140"), 0.2)
        self.assertEqual(minimum_cycle_quiet_time("PS5120E"), 0.0)

    @patch("bms_gui.time.sleep")
    @patch("bms_gui.poll_bms")
    def test_poll_retries_once_before_reporting_packet_loss(
        self,
        poll_mock: Mock,
        sleep_mock: Mock,
    ) -> None:
        expected = BmsSample(timestamp="ok")
        poll_mock.side_effect = [TimeoutError("temporary timeout"), expected]
        serial_mock = Mock()

        result = poll_bms_with_retry(
            serial_mock,
            response_timeout=3.0,
            pack_count=4,
            cells_per_pack=16,
            max_packs=14,
            product_model="HV140",
            include_static=False,
        )

        self.assertIs(result, expected)
        self.assertEqual(poll_mock.call_count, 2)
        serial_mock.reset_input_buffer.assert_called_once_with()
        sleep_mock.assert_called_once_with(0.2)

    def test_cached_static_data_is_applied_without_overwriting_live_values(self) -> None:
        cached = BmsSample(
            timestamp="cached",
            voltage_v=50.0,
            serial_number="SERIAL",
            software_version="V1.2",
            full_capacity_ah=100.0,
            configured_pack_count=4,
            cells_per_pack=16,
            total_cell_count=64,
            temperature_warning_lines=[(70.0, "#ef4444", "OT")],
            bms_parameters=[
                {
                    "group": "Voltage",
                    "name": "OV",
                    "value": "3.65",
                    "unit": "V",
                    "status": "ok",
                }
            ],
            cells_checksum_ok=True,
        )
        live = BmsSample(
            timestamp="live",
            voltage_v=51.0,
            remaining_capacity_ah=75.0,
        )

        apply_cached_static_data(live, cached)

        self.assertEqual(live.voltage_v, 51.0)
        self.assertEqual(live.serial_number, "SERIAL")
        self.assertEqual(live.configured_pack_count, 4)
        self.assertEqual(live.temperature_warning_lines, cached.temperature_warning_lines)
        self.assertEqual(live.bms_parameters, cached.bms_parameters)
        self.assertIs(live.temperature_warning_lines, cached.temperature_warning_lines)
        self.assertIs(live.bms_parameters, cached.bms_parameters)
        self.assertEqual(live.full_capacity_ah, 100.0)
        self.assertEqual(live.soc_percent, 75.0)
        self.assertTrue(live.cells_checksum_ok)

    def test_chart_downsampling_preserves_extremes_and_endpoints(self) -> None:
        values = [float(index) for index in range(1000)]
        values[501] = 5000.0
        values[502] = -5000.0

        points = chart_display_points(values, max_points=100)
        point_map = dict(points)

        self.assertEqual(point_map[0], 0.0)
        self.assertEqual(point_map[999], 999.0)
        self.assertEqual(point_map[501], 5000.0)
        self.assertEqual(point_map[502], -5000.0)
        self.assertLessEqual(len(points), 104)


if __name__ == "__main__":
    unittest.main()
