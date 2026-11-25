# ทดสอบส่วนที่ไม่ต้องใช้มือถือ (รันบน PC ได้):  python -m pytest tests  หรือ  python tests/test_offline.py
import base64
import io
import os
import socket
import struct
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

import android_utils as au
import config_store
import usb_ccid
from escpos import build_escpos_image
from smartcard_reader import ThaiIDCardReader

config_store._config_file = os.path.join(tempfile.mkdtemp(), "config.json")


# ---------------------------------------------------------------- ESC/POS

def test_escpos_raster():
    img = Image.new("RGBA", (288, 10), (0, 0, 0, 0))   # โปร่งใสทั้งภาพ -> ต้องเป็นพื้นขาว
    for x in range(0, 16):
        img.putpixel((x, 0), (0, 0, 0, 255))             # จุดดำ 16 จุดแรกของแถวแรก
    data = build_escpos_image(img, "80", 48)
    assert data[:2] == b"\x1B\x40"
    assert data[2:6] == b"\x1D\x76\x30\x00"
    xL, xH, yL, yH = data[6:10]
    assert xL + xH * 256 == 72 and yL + yH * 256 == 20   # 576/8, สูง 10*2
    raster = data[10:10 + 72 * 20]
    assert raster[0:4] == b"\xFF\xFF\xFF\xFF"            # 32 จุดดำหลังขยาย 2 เท่า
    assert raster[4] == 0 and set(raster[72:]) <= {0, 0xFF}
    assert data.endswith(b"\x1B\x64\x02\x1D\x56\x00")     # feed 48px = 2 lines + cut


def test_escpos_small_bands():
    """รูปยาวต้องถูกแบ่งเป็นหลาย GS v 0 ก้อนละ <= 4KB และรวมกันได้ครบทุกแถว"""
    img = Image.new("L", (576, 1000), 0)
    data = build_escpos_image(img, "80", 0)
    pos, total_rows, bands = 2, 0, 0
    while data[pos:pos + 4] == b"\x1D\x76\x30\x00":
        xL, xH, yL, yH = data[pos + 4:pos + 8]
        w, h = xL + xH * 256, yL + yH * 256
        assert w == 72 and w * h <= 4096
        assert set(data[pos + 8:pos + 8 + w * h]) == {0xFF}
        pos += 8 + w * h
        total_rows += h
        bands += 1
    assert total_rows == 1000 and bands > 1
    assert data[pos:] == b"\x1B\x64\x01\x1D\x56\x00"


def test_paced_write():
    import time
    import printer_transport as pt
    pt._RATE["test"] = (100, 1000)       # ก้อนละ 100 ไบต์, 1000 ไบต์/วินาที
    got = []
    t0 = time.monotonic()
    pt._paced_write(got.append, b"x" * 350, "test")
    elapsed = time.monotonic() - t0
    assert [len(c) for c in got] == [100, 100, 100, 50]
    assert 0.3 <= elapsed < 0.6


def test_wait_between_jobs():
    """งานที่ 2 ต้องรอจนเครื่องพิมพ์งานแรก + ตัดกระดาษเสร็จก่อน"""
    import time
    import printer_transport as pt
    calls = []
    orig, orig_cut = pt._send_network, pt._CUT_SEC
    pt._send_network = lambda name, data: calls.append(time.monotonic())
    pt._CUT_SEC = 0.2
    try:
        pt._busy_until = 0.0
        pt.send_to_printer("network", "x", b"a", print_seconds=0.3)
        pt.send_to_printer("network", "x", b"b", print_seconds=0.3)
        assert len(calls) == 2 and calls[1] - calls[0] >= 0.5
    finally:
        pt._send_network, pt._CUT_SEC, pt._busy_until = orig, orig_cut, 0.0


# ---------------------------------------------------------------- Fake CCID reader + Thai ID card

def tis(s):
    return s.encode("tis-620")


CARD_FIELDS = {
    (0x00, 0x04): b"1234567890123",
    (0x00, 0x11): tis("นาย#สมชาย##ใจดี").ljust(100, b" "),
    (0x00, 0x75): b"Mr.#Somchai##Jaidee".ljust(100, b" "),
    (0x00, 0xD9): b"25300115",
    (0x00, 0xE1): b"1",
    (0x00, 0xF6): tis("อำเภอเมือง/เชียงใหม่").ljust(100, b" "),
    (0x01, 0x67): b"25650101",
    (0x01, 0x6F): b"25740114",
    (0x15, 0x79): tis("99/1#หมู่ที่ 3##ถนนห้วยแก้ว#ตำบลสุเทพ#อำเภอเมืองเชียงใหม่#จังหวัดเชียงใหม่").ljust(100, b" "),
}
PHOTO = bytes(range(256)) * 20


class FakeCard:
    """จำลองบัตร T=0 + เครื่องอ่าน TPDU-level: READ BINARY ตอบ 61xx แล้วต้อง GET RESPONSE"""

    def __init__(self):
        self.present = True
        self.pending = b""

    def apdu(self, apdu):
        cla, ins, p1, p2 = apdu[:4]
        if ins == 0xA4:
            return b"\x61\x0A"
        if ins == 0xB0:
            le = apdu[6]
            if (p1, p2) in CARD_FIELDS:
                self.pending = CARD_FIELDS[(p1, p2)][:le]
            else:  # บล็อกรูป
                block = p1 - 1
                self.pending = PHOTO[block * 255:(block + 1) * 255][:le]
            return bytes([0x61, len(self.pending) & 0xFF])
        if ins == 0xC0:
            out, self.pending = self.pending[:apdu[4] or 256], b""
            return out + b"\x90\x00"
        return b"\x6D\x00"


class FakeUsb:
    def __init__(self, card, atr="3B6800000073C84012009000", accept_pps=True):
        self.card = card
        self.queue = []
        self.atr = bytes.fromhex(atr)
        self.accept_pps = accept_pps
        self.set_params = []

    def out(self, conn, ep, data, timeout_ms=0, chunk_size=0):
        msg_type, length, slot, seq = struct.unpack_from("<BIBB", data, 0)
        payload = data[10:10 + length]
        status, resp_type, body = 0, 0x80, b""
        if msg_type == 0x65:
            resp_type = 0x81
            status = 0 if self.card.present else 2
        elif msg_type == 0x62:
            if self.card.present:
                body = self.atr
            else:
                status, err = 0x42, 0xFE
        elif msg_type == 0x63:
            resp_type = 0x81
        elif msg_type == 0x61:
            resp_type = 0x82
            self.set_params.append(bytes(payload))
        elif msg_type == 0x6F and payload[:1] == bytes([0xFF]):   # PPS request
            body = payload if self.accept_pps else bytes([0xFF, 0x00, 0xFF])
        elif msg_type == 0x6F:
            body = self.card.apdu(payload)
        err = 0xFE if status == 0x42 else 0
        msg = struct.pack("<BIBBBBB", resp_type, len(body), 0, seq, status, err, 0) + body
        # ส่งกลับเป็น packet ละ 64 ไบต์เหมือน USB จริง
        self.queue.extend(msg[i:i + 64] for i in range(0, len(msg), 64))
        return len(data)

    def inp(self, conn, ep, size, timeout_ms=0):
        return self.queue.pop(0) if self.queue else None


class FakeReader(usb_ccid.CcidReader):
    def __init__(self):
        self.label = "Fake Reader [058F:9540]"
        self.conn = self.ep_in = self.ep_out = self.intf = None
        self._in_size = 1024
        self.features = None
        self._seq = 0
        self._lock = threading.Lock()
        self.closed = False

    def close(self):
        self.closed = True


def _install_fake(card, **kw):
    usb = FakeUsb(card, **kw)
    usb_ccid.au.bulk_transfer_out = usb.out
    usb_ccid.au.bulk_transfer_in = usb.inp
    return usb


def test_ccid_and_thai_id_read():
    card = FakeCard()
    _install_fake(card)
    reader = ThaiIDCardReader()
    fake = FakeReader()
    reader._open_selected_reader = lambda: fake

    assert fake.is_card_present()
    data = reader._do_actual_read(read_image=True)
    assert data["pid"] == "1234567890123"
    assert (data["titleName"], data["fname"], data["mname"], data["lname"]) == ("นาย", "สมชาย", "", "ใจดี")
    assert data["fnameEng"] == "Somchai" and data["lnameEng"] == "Jaidee"
    assert data["birthDate"] == "25300115" and data["sex"] == "1"
    assert data["houseNo"] == "99/1" and data["villageNo"] == "3"
    assert data["laneRoad"] == "ถนนห้วยแก้ว"
    assert (data["subDistrict"], data["district"], data["province"]) == ("สุเทพ", "เมืองเชียงใหม่", "เชียงใหม่")
    assert base64.b64decode(data["image"]) == PHOTO[:255 * 20]

    card.present = False
    assert not fake.is_card_present()
    try:
        reader._do_actual_read(read_image=False)
        raise AssertionError("ควร error เพราะไม่มีบัตร")
    except Exception as e:
        assert type(e).__name__ == "CardNotPresentError", e


def test_parse_atr():
    info = usb_ccid.parse_atr(bytes.fromhex("3B6800000073C84012009000"))
    assert info["ta1"] is None and info["protocol"] == 0
    info = usb_ccid.parse_atr(bytes.fromhex("3BF81300008131FE454A434F5076323431B7"))
    assert info["ta1"] == 0x13 and info["protocol"] == 1
    info = usb_ccid.parse_atr(bytes.fromhex("3B7918000000544820"))
    assert info["ta1"] == 0x18 and info["tc1"] == 0x00 and info["protocol"] == 0


FAST_ATR = "3B7918000000544820"   # TA1=0x18 (เร็วกว่าค่าเริ่มต้น 12 เท่า)


def test_speed_negotiation_tpdu_reader():
    card = FakeCard()
    usb = _install_fake(card, atr=FAST_ATR)
    reader = ThaiIDCardReader()
    fake = FakeReader()
    fake.features = 0x00010030   # TPDU, ไม่มี auto PPS
    reader._open_selected_reader = lambda: fake
    data = reader._do_actual_read(read_image=True)
    assert data["pid"] == "1234567890123"
    assert usb.set_params == [bytes([0x18, 0x00, 0x00, 0x0A, 0x00])]

    # เครื่องอ่านที่ต่อรองเองได้ — ต้องไม่ส่งอะไรเพิ่ม
    usb = _install_fake(card, atr=FAST_ATR)
    fake.features = 0x000100FE
    reader._do_actual_read(read_image=False)
    assert usb.set_params == []


def test_speed_negotiation_fallback():
    card = FakeCard()
    usb = _install_fake(card, atr=FAST_ATR, accept_pps=False)
    reader = ThaiIDCardReader()
    fake = FakeReader()
    fake.features = 0x00010030
    reader._open_selected_reader = lambda: fake
    data = reader._do_actual_read(read_image=False)
    assert data["pid"] == "1234567890123"
    assert usb.set_params == [] and fake.label in reader._no_speedup


def test_read_image_toggle():
    from http_app import App, Request
    from smartcard_reader import register_smartcard_routes
    card = FakeCard()
    _install_fake(card)
    app = App()
    orig_start = ThaiIDCardReader.start_background_watcher
    ThaiIDCardReader.start_background_watcher = lambda self: None  # ไม่ให้ watcher จริงมายุ่ง
    try:
        reader = register_smartcard_routes(app)
    finally:
        ThaiIDCardReader.start_background_watcher = orig_start
    fake = FakeReader()

    def get_card(flag):
        req = Request("GET", "/api/smartcard/read-card-only", {"readImageFlag": flag}, {}, b"")
        return app.dispatch(req)
    reader._open_selected_reader = lambda: fake

    # ค่าเริ่มต้นต้องเป็น "อ่านรูป"
    assert reader.get_smartcard_config()["read_image"] is True

    # ปิดอ่านรูป -> อ่านบัตรโดยไม่อ่านรูป, ค่าเครื่องอ่านเดิมต้องไม่หาย
    reader.set_smartcard_reader("Fake Reader [058F:9540]")
    reader.set_smartcard_reader(read_image=False)
    cfg = reader.get_smartcard_config()
    assert cfg == {"selected_reader": "Fake Reader [058F:9540]", "read_image": False}
    reader._read_and_cache()
    data, err, present = reader.get_cached_result()
    assert err is None and "image" not in data and data["pid"] == "1234567890123"
    assert not reader._needs_image_refresh()
    body, status = get_card("true")
    assert status == 200 and body["image"] == "" and body["pid"] == "1234567890123"
    body, status = get_card("false")
    assert status == 200 and "image" not in body

    # มีรูปในแคช แต่ปิดการอ่านรูปภายหลัง -> ต้องส่งค่าว่าง
    reader.set_smartcard_reader(read_image=True)
    reader._read_and_cache()
    reader.set_smartcard_reader(read_image=False)
    body, _ = get_card("true")
    assert body["image"] == ""
    reader._read_and_cache()

    # POST config เปลี่ยนเฉพาะ read_image ได้
    req = Request("POST", "/api/smartcard/config", {}, {}, b'{"read_image": true}')
    body, status = app.dispatch(req)
    assert status == 200 and body["read_image"] is True and body["selected_reader"] == "Fake Reader [058F:9540]"
    reader.set_smartcard_reader(read_image=False)

    # เปิดกลับ -> watcher ต้องรู้ว่าต้องอ่านใหม่เพื่อเติมรูป
    reader.set_smartcard_reader(read_image=True)
    assert reader._needs_image_refresh()
    reader._read_and_cache()
    data, _, _ = reader.get_cached_result()
    assert base64.b64decode(data["image"]) == PHOTO[:255 * 20]
    body, _ = get_card("true")
    assert base64.b64decode(body["image"]) == PHOTO[:255 * 20]


def test_ccid_features_parse():
    intf = bytes([9, 4, 0, 0, 3, 0x0B, 0, 0, 0])
    cls = bytearray(54)
    cls[0], cls[1] = 54, 0x21
    struct.pack_into("<I", cls, 40, 0x000204BA)
    assert usb_ccid.parse_ccid_features(list(intf + bytes(cls))) == 0x000204BA


# ---------------------------------------------------------------- HTTP server + network printer

import json
import urllib.request
import urllib.error


class _Res:
    def __init__(self, status, body, headers):
        self.status_code, self._body, self.headers = status, body, headers

    def get_json(self):
        return json.loads(self._body)


def _call(base, method, path, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=dict(headers or {}))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return _Res(r.status, r.read(), r.headers)
    except urllib.error.HTTPError as e:
        return _Res(e.code, e.read(), e.headers)


class _Client:
    def __init__(self, base):
        self.base = base

    def get(self, path, headers=None):
        return _Call(self.base, "GET", path, headers=headers)

    def post(self, path, json=None):
        return _call(self.base, "POST", path, json)

    def options(self, path, headers=None):
        return _call(self.base, "OPTIONS", path, headers=headers)


def _Call(base, method, path, headers=None):
    return _call(base, method, path, headers=headers)


def test_http_print_to_network_printer():
    received = bytearray()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept():
        conn, _ = srv.accept()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        conn.close()

    t = threading.Thread(target=accept, daemon=True)
    t.start()

    config_store.save_config({"printer_connection": "network", "printer_name": f"127.0.0.1:{port}",
                              "paper_size": "58", "printer_enabled": True})
    from server import create_app
    httpd = create_app().make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = _Client(f"http://127.0.0.1:{httpd.server_address[1]}")

    ping = client.get("/ping", headers={"Origin": "https://example.com"})
    assert ping.get_json() == {"status": "ok"}
    assert ping.headers.get("Access-Control-Allow-Origin") == "https://example.com"
    assert client.get("/nope").status_code == 404

    buf = io.BytesIO()
    Image.new("RGB", (100, 50), "white").save(buf, format="PNG")
    b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    res = client.post("/print", json={"imageBase64": b64})
    assert res.status_code == 200, res.get_json()
    t.join(5)
    assert received.startswith(b"\x1B\x40\x1D\x76\x30\x00") and received.endswith(b"\x1D\x56\x00")
    assert received[6] == 48  # 58mm = 384px = 48 bytes

    config_store.save_config({"printer_enabled": False})
    res = client.post("/print", json={"imageBase64": b64})
    assert res.status_code == 500 and "ปิดใช้งาน" in res.get_json()["error"]

    res = client.get("/api/smartcard/read-card-only")
    assert res.status_code == 500 and "Card not present" in res.get_json()["message"]

    res = client.options("/ping", headers={"Origin": "https://example.com",
                                           "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Private-Network": "true"})
    assert res.headers.get("Access-Control-Allow-Private-Network") == "true"
    assert "POST" in res.headers.get("Access-Control-Allow-Methods")
    httpd.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
