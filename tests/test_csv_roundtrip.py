from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from bms_collector import CsvLogger
from bms_gui import count_csv_samples, load_samples_from_csv
from jbd_hv_protocol import BmsSample


class CsvRoundTripTests(unittest.TestCase):
    def test_preserves_offline_cell_position_and_warning_lines(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bms-csv-test-") as directory:
            path = Path(directory) / "roundtrip.csv"
            sample = BmsSample(
                timestamp="2026-06-11T12:00:00",
                acquisition_duration_s=1.234,
                record_event="BATTERY CHANGED: HV140, SN A -> HV140, SN B",
                total_cell_count=3,
                cell_voltages_mv=[3300, None, 3400],
                temperature_warning_lines=[(-20.0, "#3b82f6", "UT")],
                cell_voltage_warning_lines=[(2500.0, "#ef4444", "UV")],
                total_voltage_warning_lines_v=[(40.0, "#ef4444", "Pack UV")],
                bms_parameters=[
                    {
                        "group": "Cell Over Voltage",
                        "name": "Cell OV Alarm",
                        "value": "3.65",
                        "unit": "V",
                        "status": "ok",
                    }
                ],
                bms_parameter_errors=["one command failed"],
            )
            logger = CsvLogger(path, max_cells=3, max_temps=0)
            logger.write(sample)
            logger.close()

            loaded = load_samples_from_csv(path)[0]

            self.assertEqual(loaded.cell_voltages_mv, [3300, None, 3400])
            self.assertEqual(loaded.acquisition_duration_s, 1.234)
            self.assertEqual(loaded.record_event, sample.record_event)
            self.assertEqual(loaded.temperature_warning_lines, sample.temperature_warning_lines)
            self.assertEqual(loaded.cell_voltage_warning_lines, sample.cell_voltage_warning_lines)
            self.assertEqual(
                loaded.total_voltage_warning_lines_v,
                sample.total_voltage_warning_lines_v,
            )
            self.assertEqual(loaded.bms_parameters, sample.bms_parameters)
            self.assertEqual(
                loaded.bms_parameter_errors,
                sample.bms_parameter_errors,
            )

    def test_large_log_loader_reads_a_bounded_timeline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bms-csv-limit-test-") as directory:
            path = Path(directory) / "limited.csv"
            logger = CsvLogger(path, max_cells=1, max_temps=0)
            for index in range(100):
                logger.write(
                    BmsSample(
                        timestamp=f"2026-06-11T12:{index // 60:02d}:{index % 60:02d}",
                        voltage_v=float(index),
                        total_cell_count=1,
                        cell_voltages_mv=[3300 + index],
                    )
                )
            logger.close()

            loaded = load_samples_from_csv(path, max_samples=10)

            self.assertEqual(count_csv_samples(path), 100)
            self.assertEqual(len(loaded), 10)
            self.assertEqual(loaded[0].voltage_v, 0.0)
            self.assertEqual(loaded[-1].voltage_v, 99.0)

    def test_static_metadata_is_written_once_and_inherited_on_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bms-csv-static-test-") as directory:
            path = Path(directory) / "static.csv"
            parameters = [
                {
                    "group": "Voltage",
                    "name": "OV",
                    "value": "3.65",
                    "unit": "V",
                    "status": "ok",
                }
            ]
            first = BmsSample(
                timestamp="2026-06-11T12:00:00",
                product_model="HV140",
                serial_number="SERIAL",
                software_version="V1.1",
                voltage_v=50.0,
                bms_parameters=parameters,
                config_raw="STATIC-CONFIG",
            )
            second = BmsSample(
                timestamp="2026-06-11T12:00:01",
                product_model="HV140",
                serial_number="SERIAL",
                software_version="V1.1",
                voltage_v=51.0,
                bms_parameters=parameters,
                config_raw="STATIC-CONFIG",
            )
            logger = CsvLogger(path, max_cells=0, max_temps=0)
            logger.write(first)
            logger.write(second)
            logger.close()

            with path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["serial_number"], "SERIAL")
            self.assertNotEqual(rows[0]["bms_parameters"], "")
            self.assertEqual(rows[1]["serial_number"], "")
            self.assertEqual(rows[1]["bms_parameters"], "")
            self.assertEqual(rows[1]["config_raw"], "")

            loaded = load_samples_from_csv(path)
            self.assertEqual(loaded[1].serial_number, "SERIAL")
            self.assertEqual(loaded[1].software_version, "V1.1")
            self.assertEqual(loaded[1].bms_parameters, parameters)
            self.assertEqual(loaded[1].config_raw, "STATIC-CONFIG")


if __name__ == "__main__":
    unittest.main()
