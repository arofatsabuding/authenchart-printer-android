# usb_ccid.py
# ==============================================================
# ไดรเวอร์เครื่องอ่านบัตร USB CCID แบบ userspace — แทน PC/SC (pyscard) ซึ่งไม่มีบน Android
# คุยกับเครื่องอ่านโดยตรงผ่าน Android USB Host API ตามมาตรฐาน USB CCID Rev 1.1
# รองรับเครื่องอ่านที่เป็น CCID มาตรฐาน (ACS, Alcor, Feitian, Identiv, ฯลฯ — เกือบทุกรุ่นในไทย)
# ==============================================================

import struct
import threading
import time

import android_utils as au

PC_TO_RDR_SET_PARAMETERS = 0x61
PC_TO_RDR_ICC_POWER_ON = 0x62
PC_TO_RDR_ICC_POWER_OFF = 0x63
PC_TO_RDR_GET_SLOT_STATUS = 0x65
PC_TO_RDR_XFR_BLOCK = 0x6F

RDR_TO_PC_DATA_BLOCK = 0x80
RDR_TO_PC_SLOT_STATUS = 0x81

ICC_PRESENT_ACTIVE = 0
ICC_PRESENT_INACTIVE = 1
ICC_NOT_PRESENT = 2

_CMD_FAILED = 1
_CMD_TIME_EXTENSION = 2

_ERR_ICC_MUTE = 0xFE
_ERR_BAD_POWER_SELECT = 0x07

_HEADER_LEN = 10

# dwFeatures (CCID Rev 1.1 ตาราง 5.1-1)
_FEAT_AUTO_PARAM_ATR = 0x00000002   # เครื่องอ่านตั้งความเร็วตาม ATR เอง
_FEAT_AUTO_PPS_PROP = 0x00000040    # เครื่องอ่านต่อรองความเร็ว (PPS) กับบัตรเอง
_FEAT_AUTO_PPS_CUR = 0x00000080     # เครื่องอ่านทำ PPS ให้เมื่อได้รับ SetParameters
_FEAT_LEVEL_MASK = 0x00070000
_FEAT_LEVEL_TPDU = 0x00010000


class CcidError(Exception):
    pass


class CcidNoCardError(CcidError):
    pass


class CcidPermissionError(CcidError):
    pass


def find_ccid_interface(device):
    """คืน (interface, bulk_out, bulk_in) ของ interface Smart Card หรือ None"""
    for intf in au.iter_interfaces(device):
        if intf.getInterfaceClass() != au.USB_CLASS_CSCID:
            continue
        ep_out = ep_in = None
        for ep in au.iter_endpoints(intf):
            if ep.getType() != au.USB_ENDPOINT_XFER_BULK:
                continue
            if ep.getDirection() == au.USB_DIR_IN:
                ep_in = ep
            else:
                ep_out = ep
        if ep_out is not None and ep_in is not None:
            return intf, ep_out, ep_in
    return None


def list_ccid_devices():
    """คืน [(label, device)] ของเครื่องอ่านบัตรที่เสียบอยู่"""
    result = []
    for dev in au.list_usb_devices():
        if find_ccid_interface(dev) is not None:
            result.append((au.usb_device_label(dev), dev))
    return result


def build_message(msg_type, seq, payload=b"", params=b"\x00\x00\x00", slot=0):
    return struct.pack("<BIBB", msg_type, len(payload), slot, seq) + bytes(params) + bytes(payload)


def parse_message(msg):
    """คืน dict ของ response header + data"""
    if len(msg) < _HEADER_LEN:
        raise CcidError(f"response สั้นเกินไป ({len(msg)} bytes)")
    msg_type, length, slot, seq, status, error, chain = struct.unpack_from("<BIBBBBB", msg, 0)
    return {
        "type": msg_type,
        "length": length,
        "slot": slot,
        "seq": seq,
        "icc_status": status & 0x03,
        "cmd_status": (status >> 6) & 0x03,
        "error": error,
        "chain": chain,
        "data": bytes(msg[_HEADER_LEN:_HEADER_LEN + length]),
    }


def parse_ccid_features(raw_descriptors):
    """หา dwFeatures จาก CCID class descriptor (bDescriptorType 0x21, ยาว 54 ไบต์)"""
    raw = bytes((b & 0xFF) for b in raw_descriptors)
    i = 0
    while i + 2 <= len(raw):
        length, dtype = raw[i], raw[i + 1]
        if length < 2:
            break
        if dtype == 0x21 and length >= 54 and i + 44 <= len(raw):
            return struct.unpack_from("<I", raw, i + 40)[0]
        i += length
    return None


def parse_atr(atr):
    """ดึงค่าที่ใช้ตั้งความเร็วจาก ATR: TA1 (Fi/Di), TC1 (guard time), TC2 (WI), TA2, โปรโตคอลแรก"""
    atr = bytes(atr)
    info = {"ta1": None, "tc1": None, "ta2": None, "tc2": None, "protocol": 0}
    if len(atr) < 2:
        return info
    y = atr[1] >> 4
    i, level, first_td = 2, 1, True
    while i < len(atr):
        for bit, name in ((1, "ta"), (2, "tb"), (4, "tc")):
            if y & bit:
                if i >= len(atr):
                    return info
                key = f"{name}{level}"
                if key in info:
                    info[key] = atr[i]
                i += 1
        if not (y & 8) or i >= len(atr):
            break
        td = atr[i]
        i += 1
        if first_td:
            info["protocol"] = td & 0x0F
            first_td = False
        y = td >> 4
        level += 1
    return info


class CcidReader:
    """เปิดเครื่องอ่าน 1 เครื่องค้างไว้ (claim interface) — ใช้จากโปรเซส service เท่านั้น"""

    def __init__(self, device, label=None):
        self.device = device
        self.label = label or au.usb_device_label(device)
        found = find_ccid_interface(device)
        if found is None:
            raise CcidError(f"'{self.label}' ไม่ใช่เครื่องอ่านบัตรแบบ CCID")
        self.intf, self.ep_out, self.ep_in = found

        if not au.has_usb_permission(device):
            raise CcidPermissionError(f"ยังไม่ได้รับอนุญาตให้ใช้เครื่องอ่านบัตร '{self.label}'")

        self.conn = au.get_usb_manager().openDevice(device)
        if self.conn is None:
            raise CcidError(f"เปิดเครื่องอ่านบัตร '{self.label}' ไม่สำเร็จ")
        if not self.conn.claimInterface(self.intf, True):
            self.conn.close()
            raise CcidError(f"claim interface เครื่องอ่านบัตร '{self.label}' ไม่สำเร็จ")

        self.features = None
        try:
            self.features = parse_ccid_features(self.conn.getRawDescriptors())
        except Exception:
            pass

        packet = max(int(self.ep_in.getMaxPacketSize() or 64), 8)
        self._in_size = ((1024 + packet - 1) // packet) * packet
        self._seq = 0
        self._lock = threading.Lock()
        self.closed = False

    # ---------------- low level ----------------

    def _next_seq(self):
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _read_message(self, timeout_s):
        deadline = time.time() + timeout_s
        buf = bytearray()
        while time.time() < deadline:
            chunk = au.bulk_transfer_in(self.conn, self.ep_in, self._in_size, timeout_ms=1000)
            if chunk is None:
                continue  # timeout รอบย่อย — ลองอ่านต่อจนกว่าจะหมดเวลารวม
            buf += chunk
            if len(buf) >= _HEADER_LEN:
                total = _HEADER_LEN + struct.unpack_from("<I", buf, 1)[0]
                if len(buf) >= total:
                    return bytes(buf[:total])
        raise CcidError("เครื่องอ่านบัตรไม่ตอบสนอง (timeout)")

    def _exchange(self, msg_type, payload=b"", params=b"\x00\x00\x00", timeout_s=10):
        with self._lock:
            seq = self._next_seq()
            msg = build_message(msg_type, seq, payload, params)
            au.bulk_transfer_out(self.conn, self.ep_out, msg, timeout_ms=3000)
            deadline = time.time() + timeout_s
            while True:
                resp = parse_message(self._read_message(max(0.5, deadline - time.time())))
                if resp["seq"] != seq:
                    continue  # response เก่าที่ค้างอยู่ — ทิ้ง
                if resp["cmd_status"] == _CMD_TIME_EXTENSION:
                    if time.time() > deadline:
                        raise CcidError("บัตรตอบสนองช้าเกินไป (timeout)")
                    continue
                return resp

    @staticmethod
    def _raise_failed(resp, action):
        if resp["icc_status"] == ICC_NOT_PRESENT or resp["error"] == _ERR_ICC_MUTE:
            raise CcidNoCardError("ไม่มีบัตรเสียบอยู่ในเครื่อง")
        raise CcidError(f"{action} ไม่สำเร็จ (CCID error 0x{resp['error']:02X})")

    # ---------------- public ----------------

    def slot_status(self):
        resp = self._exchange(PC_TO_RDR_GET_SLOT_STATUS, timeout_s=3)
        return resp["icc_status"]

    def is_card_present(self):
        return self.slot_status() in (ICC_PRESENT_ACTIVE, ICC_PRESENT_INACTIVE)

    def power_on(self):
        """จ่ายไฟให้บัตร + คืน ATR — ถ้า auto voltage ไม่รองรับ จะลอง 5V / 3V / 1.8V"""
        for power_select in (0x00, 0x01, 0x02, 0x03):
            resp = self._exchange(PC_TO_RDR_ICC_POWER_ON, params=bytes([power_select, 0, 0]))
            if resp["cmd_status"] != _CMD_FAILED:
                return resp["data"]
            if resp["error"] == _ERR_BAD_POWER_SELECT:
                continue
            self._raise_failed(resp, "Power on บัตร")
        raise CcidError("เครื่องอ่านไม่รองรับระดับแรงดันไฟที่ลองทั้งหมด")

    def negotiate_speed(self, atr):
        """
        ✅ ทำแบบเดียวกับไดรเวอร์ CCID บน Windows: ถ้าบัตรบอกใน ATR (TA1) ว่ารับความเร็วสูงกว่าค่าเริ่มต้นได้
        และเครื่องอ่านไม่ได้ต่อรองให้เอง → ส่ง PPS แล้ว SetParameters เพื่อเร่งความเร็วการรับส่งข้อมูลกับบัตร
        คืน True ถ้าเปลี่ยนความเร็วแล้ว / False ถ้าไม่จำเป็นหรือทำไม่ได้ / ยก CcidError ถ้าล้มเหลวกลางทาง
        (ผู้เรียกต้อง power off/on บัตรใหม่เมื่อเกิด error)
        """
        feats = getattr(self, "features", None)
        if feats is None:
            return False  # ไม่รู้ความสามารถของเครื่องอ่าน — ไม่เสี่ยง
        info = parse_atr(atr)
        ta1 = info["ta1"]
        if ta1 is None or ta1 == 0x11 or info["protocol"] != 0 or info["ta2"] is not None:
            return False  # บัตรใช้ความเร็วเริ่มต้น / ไม่ใช่ T=0 / บัตรล็อกโหมดไว้แล้ว
        if feats & (_FEAT_AUTO_PARAM_ATR | _FEAT_AUTO_PPS_PROP):
            return False  # เครื่องอ่านจัดการความเร็วเองอยู่แล้ว
        auto_pps_cur = bool(feats & _FEAT_AUTO_PPS_CUR)
        if not auto_pps_cur and (feats & _FEAT_LEVEL_MASK) != _FEAT_LEVEL_TPDU:
            return False  # ส่ง PPS เองได้เฉพาะเครื่องอ่านแบบ TPDU

        if not auto_pps_cur:
            pps = bytes([0xFF, 0x10, ta1, 0xFF ^ 0x10 ^ ta1])
            resp = self._exchange(PC_TO_RDR_XFR_BLOCK, payload=pps, timeout_s=3)
            if resp["cmd_status"] == _CMD_FAILED:
                self._raise_failed(resp, "PPS")
            if resp["data"][:3] != pps[:3]:
                raise CcidError(f"บัตรไม่ยอมรับ PPS (ตอบ {resp['data'].hex()})")

        convention = 0x02 if bytes(atr)[:1] == b"\x3F" else 0x00
        params = bytes([ta1, convention, info["tc1"] or 0x00, info["tc2"] or 0x0A, 0x00])
        resp = self._exchange(PC_TO_RDR_SET_PARAMETERS, payload=params,
                              params=b"\x00\x00\x00", timeout_s=3)
        if resp["cmd_status"] == _CMD_FAILED:
            self._raise_failed(resp, "SetParameters")
        return True

    def power_off(self):
        try:
            self._exchange(PC_TO_RDR_ICC_POWER_OFF, timeout_s=3)
        except Exception:
            pass

    def transmit(self, apdu):
        """ส่ง APDU คืน (data_list, sw1, sw2) — รูปแบบเดียวกับ pyscard connection.transmit()"""
        resp = self._exchange(PC_TO_RDR_XFR_BLOCK, payload=bytes(apdu), params=b"\x00\x00\x00")
        if resp["cmd_status"] == _CMD_FAILED:
            self._raise_failed(resp, "ส่งคำสั่งไปยังบัตร")
        data = resp["data"]
        if len(data) < 2:
            raise CcidError("บัตรตอบกลับข้อมูลไม่ครบ (ไม่มี status word)")
        return list(data[:-2]), data[-2], data[-1]

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.releaseInterface(self.intf)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
