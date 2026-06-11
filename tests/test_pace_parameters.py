from __future__ import annotations

import unittest

import pace_rs232_protocol as pace


class PaceParameterTests(unittest.TestCase):
    def values(self, command: int, info_hex: str) -> dict[str, str]:
        parameters = pace.decode_parameter_info(command, bytes.fromhex(info_hex))
        return {item["name"]: item["value"] for item in parameters}

    def test_voltage_parameters_follow_original_ui_order(self) -> None:
        values = self.values(pace.CID2_PARAM_CELL_OV, "010EA60E420D340A")
        self.assertEqual(values["Cell OV Alarm"], "3.65")
        self.assertEqual(values["Cell OV Protect"], "3.75")
        self.assertEqual(values["Cell OVP Release"], "3.38")
        self.assertEqual(values["Cell OVP Delay Time"], "1000")

    def test_pack_voltage_scales_by_series_cell_count(self) -> None:
        values = self.values(pace.CID2_PARAM_TOTAL_UV, "01B540AF00C1C00A")
        self.assertEqual(values["Pack UV Alarm"], "44.8")
        self.assertEqual(values["Pack UV Protect"], "46.4")
        self.assertEqual(values["Pack UVP Release"], "49.6")

    def test_charge_and_discharge_overcurrent(self) -> None:
        charge = self.values(pace.CID2_PARAM_CHARGE_OC, "01005F005A0A")
        discharge = self.values(pace.CID2_PARAM_DISCHARGE_OC_1, "01FF60FF921E")
        self.assertEqual(charge["CHG OC Alarm"], "90")
        self.assertEqual(charge["CHG OC Protect"], "95")
        self.assertEqual(discharge["DSG OC Alarm"], "110")
        self.assertEqual(discharge["DSG OC 1 Protect"], "160")

    def test_balance_sleep_and_full_charge(self) -> None:
        balance = self.values(pace.CID2_PARAM_BALANCE, "0D7A001E")
        sleep = self.values(pace.CID2_PARAM_SLEEP, "0C4E0005")
        full_charge = self.values(pace.CID2_PARAM_FULL_CHARGE, "DAC007D00A")
        self.assertEqual(balance["Balance Threshold"], "3.45")
        self.assertEqual(balance["Balance Delta Vcell"], "30")
        self.assertEqual(sleep["Sleep Vcell"], "3.15")
        self.assertEqual(sleep["Delay Time"], "5")
        self.assertEqual(full_charge["Pack FullCharge Voltage"], "56")
        self.assertEqual(full_charge["Pack FullCharge Current"], "2000")
        self.assertEqual(full_charge["SOC Low Alarm"], "10")

    def test_temperature_parameters(self) -> None:
        high = self.values(pace.CID2_PARAM_TEMP_HIGH, "010CD00CBC0C9E0CE40CD00C9E")
        low = self.values(pace.CID2_PARAM_TEMP_LOW, "010A960AAA0ADC09CE09E20A14")
        mos = self.values(pace.CID2_PARAM_MOS_TEMP, "010F0A0EF60DFC")
        environment = self.values(pace.CID2_PARAM_ENV_TEMP, "01099C09B009E20D7A0D660D34")
        self.assertEqual(high["CHG OT Alarm"], "53")
        self.assertEqual(high["CHG OT Protect"], "55")
        self.assertEqual(low["CHG UT Alarm"], "0")
        self.assertEqual(low["CHG UT Protect"], "-2")
        self.assertEqual(low["DSG UT Alarm"], "-20")
        self.assertEqual(low["DSG UT Protect"], "-22")
        self.assertEqual(mos["MOS OT Alarm"], "110")
        self.assertEqual(environment["ENV OT Protect"], "72")

    def test_short_circuit_delay(self) -> None:
        values = self.values(pace.CID2_PARAM_SHORT_CIRCUIT, "0C")
        self.assertEqual(values["SCP Delay Time"], "300")

    def test_analog_define_values_include_full_capacity(self) -> None:
        info = bytes.fromhex(
            "00"      # info flag
            "01"      # pack count
            "01"      # cell count
            "0CE4"    # cell voltage: 3300 mV
            "00"      # temperature count
            "0000"    # current: 0 A
            "D2F0"    # pack voltage: 54.0 V
            "1388"    # remaining capacity: 50.00 Ah
            "03"      # three define values
            "2710"    # full capacity: 100.00 Ah
            "000C"    # cycle count: 12
            "2710"    # design capacity: 100.00 Ah
        )

        values = pace.decode_analog_info(info, max_packs=1)

        self.assertEqual(values["remaining_capacity_ah"], 50.0)
        self.assertEqual(values["full_capacity_ah"], 100.0)
        self.assertEqual(values["cycle_count"], 12)


if __name__ == "__main__":
    unittest.main()
