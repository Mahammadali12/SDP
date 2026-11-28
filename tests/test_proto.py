"""Self-checks for the packet decoders. Run: python -m unittest discover -s tests"""

import unittest

from linkband_capture import proto
from linkband_capture.transport import _enc_acc, _enc_eeg, _enc_ppg


class TestDecoders(unittest.TestCase):
    def test_eeg_roundtrip(self):
        samples = [(0, 1_200_000, -1_200_000), (5, -1, 8_388_607)]
        ts, out = proto.decode_eeg(_enc_eeg(123456, samples))
        self.assertEqual(ts, 123456)
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["ch1_raw"], out[0]["ch2_raw"]), (1_200_000, -1_200_000))
        self.assertTrue(out[1]["leadoff_ch1"])
        self.assertTrue(out[1]["leadoff_ch2"])
        self.assertEqual((out[1]["ch1_raw"], out[1]["ch2_raw"]), (-1, 8_388_607))

    def test_eeg_negative_sign_extension(self):
        # 0x800000 is the most negative 24-bit value, 0xFFFFFF is -1.
        _, out = proto.decode_eeg(_enc_eeg(0, [(0, -8_388_608, -1)]))
        self.assertEqual(out[0]["ch1_raw"], -8_388_608)
        self.assertEqual(out[0]["ch2_raw"], -1)

    def test_ppg_roundtrip_unsigned(self):
        _, out = proto.decode_ppg(_enc_ppg(7, [(1_440_187, 1_434_835)]))
        self.assertEqual(out[0]["red_raw"], 1_440_187)
        self.assertEqual(out[0]["ir_raw"], 1_434_835)

    def test_acc_roundtrip_signed(self):
        _, out = proto.decode_acc(_enc_acc(9, [(1, -2, 32_767)]))
        self.assertEqual((out[0]["x_raw"], out[0]["y_raw"], out[0]["z_raw"]), (1, -2, 32_767))

    def test_ppg_timestamp_is_little_endian(self):
        # device.py:708 reads bytes [3,2,1,0] -> little endian.
        payload = bytes([0x01, 0x00, 0x00, 0x00]) + bytes(6)
        ts, _ = proto.decode_ppg(payload)
        self.assertEqual(ts, 1)

    def test_bad_length_raises(self):
        with self.assertRaises(proto.ProtoError):
            proto.decode_eeg(bytes(4 + 7 * 2 + 3))
        with self.assertRaises(proto.ProtoError):
            proto.decode_ppg(bytes(3))

    def test_uv_scale(self):
        self.assertAlmostEqual(proto.EEG_LSB_UV, 0.040064, places=6)


if __name__ == "__main__":
    unittest.main()
