# printer_transport.py
# ==============================================================
# ส่งข้อมูล ESC/POS ไปยังเครื่องพิมพ์ — แทน win32print ของเวอร์ชัน Windows
# รองรับ 3 แบบ: USB (OTG), Bluetooth (SPP) และ Network (LAN/Wi-Fi, port 9100)
# ==============================================================

import re
import socket
import threading
import time

import android_utils as au

CONNECTION_USB = "usb"
CONNECTION_BLUETOOTH = "bluetooth"
CONNECTION_NETWORK = "network"

_PRINT_LOCK = threading.Lock()  # ส่งงานพิมพ์ทีละงาน กันข้อมูลสองงานปนกัน

_USB_KEY_RE = re.compile(r"\[([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\]\s*$")
_BT_MAC_RE = re.compile(r"\[([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\]\s*$")


class PrinterError(Exception):
    pass


# ✅ จำกัดความเร็วการส่ง (ไบต์/วินาที) ให้ไม่เร็วกว่าที่เครื่องพิมพ์พิมพ์ทัน
# เครื่องพิมพ์ที่ไม่มี flow control (Bluetooth ราคาถูก / USB ที่ข้างในเป็นชิป USB-Serial)
# จะทิ้งไบต์ที่ล้นบัฟเฟอร์ -> คำสั่งรูปเพี้ยน -> ข้อมูลรูปที่เหลือถูกพิมพ์เป็นข้อความขยะ
# (เวอร์ชัน Windows ไม่เจอเพราะส่งผ่านไดรเวอร์ของเครื่องพิมพ์ซึ่งคุมจังหวะให้)
_RATE = {
    # (ขนาดก้อน, ไบต์/วินาที)
    "usb": (4096, 48 * 1024),
    "bluetooth": (512, 16 * 1024),
    "network": (4096, 96 * 1024),
}


def _paced_write(write, data, connection_type):
    """เรียก write(chunk) ทีละก้อน แล้วหน่วงเวลาให้ความเร็วเฉลี่ยไม่เกินที่กำหนด"""
    chunk_size, rate = _RATE[connection_type]
    data = bytes(data)
    start = time.monotonic()
    for offset in range(0, len(data), chunk_size):
        chunk = data[offset:offset + chunk_size]
        write(chunk)
        sent = offset + len(chunk)
        wait = start + sent / rate - time.monotonic()
        if wait > 0:
            time.sleep(wait)


# ✅ ถามเครื่องพิมพ์ว่าพิมพ์ + ตัดกระดาษเสร็จหรือยัง ด้วย GS r 1 (ขอสถานะเซ็นเซอร์กระดาษ)
# GS r ไม่ใช่คำสั่ง real-time — เครื่องจะตอบหลังทำคำสั่งก่อนหน้า (รูป + ตัดกระดาษ) เสร็จหมดแล้ว
# ได้คำตอบเมื่อไหร่ = เครื่องว่าง ส่งงานถัดไปได้ทันที ไม่ต้องเดาเวลา
_STATUS_QUERY = b"\x1D\x72\x01"
_IDLE_EXTRA_SEC = 3.0       # เผื่อจากเวลาพิมพ์โดยประมาณ ก่อนถือว่าเครื่องไม่ตอบ
_status_supported = {}      # (connection, printer_name) -> True/False ว่าเครื่องนี้ตอบ GS r ไหม


def _wait_printer_idle(write, read_some, timeout_sec):
    """
    write(bytes) / read_some(timeout_ms) -> bytes (ว่าง = ไม่มีข้อมูล)
    คืน True ถ้าเครื่องตอบภายในเวลา (= พิมพ์งานก่อนหน้าเสร็จแล้ว)
    """
    for _ in range(20):                     # ทิ้งข้อมูลเก่าที่ค้างอยู่ (เช่นสถานะที่เครื่องส่งเอง)
        if not read_some(30):
            break
    write(_STATUS_QUERY)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if read_some(200):
            return True
    return False


# ---------------------------------------------------------------- USB

def _find_usb_printer_endpoint(device):
    """หา interface + bulk OUT endpoint — เลือก class Printer (7) ก่อน ถ้าไม่มีค่อยใช้ตัวอื่น"""
    candidates = []
    for intf in au.iter_interfaces(device):
        cls = intf.getInterfaceClass()
        if cls in (au.USB_CLASS_CSCID, 0x03, 0x09):  # ข้าม smart card / HID / hub
            continue
        for ep in au.iter_endpoints(intf):
            if ep.getType() == au.USB_ENDPOINT_XFER_BULK and ep.getDirection() == au.USB_DIR_OUT:
                candidates.append((0 if cls == au.USB_CLASS_PRINTER else 1, intf, ep))
                break
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1], candidates[0][2]


def list_usb_printers():
    """คืน [(label, device)] ของอุปกรณ์ USB ที่น่าจะเป็นเครื่องพิมพ์ (มี bulk OUT)"""
    result = []
    for dev in au.list_usb_devices():
        intf, ep = _find_usb_printer_endpoint(dev)
        if ep is not None:
            result.append((au.usb_device_label(dev), dev))
    return result


def _find_usb_device(printer_name):
    m = _USB_KEY_RE.search(printer_name or "")
    printers = list_usb_printers()
    if m:
        vid, pid = int(m.group(1), 16), int(m.group(2), 16)
        for label, dev in printers:
            if dev.getVendorId() == vid and dev.getProductId() == pid:
                return dev
        raise PrinterError(f"ไม่พบเครื่องพิมพ์ USB '{printer_name}' (ตรวจสอบสาย OTG / เสียบใหม่)")
    if printers:
        return printers[0][1]  # ยังไม่ได้เลือก ใช้เครื่องแรกที่เจอ
    raise PrinterError("ไม่พบเครื่องพิมพ์ USB ที่เชื่อมต่ออยู่")


def _find_bulk_in(intf):
    for ep in au.iter_endpoints(intf):
        if ep.getType() == au.USB_ENDPOINT_XFER_BULK and ep.getDirection() == au.USB_DIR_IN:
            return ep
    return None


def _send_usb(printer_name, data, idle_timeout=None):
    device = _find_usb_device(printer_name)
    if not au.has_usb_permission(device):
        au.request_usb_permission(device)
        raise PrinterError("ยังไม่ได้รับอนุญาตให้ใช้เครื่องพิมพ์ USB — กด 'อนุญาต' ในหน้าต่างที่เด้งขึ้น แล้วสั่งพิมพ์ใหม่")

    intf, ep = _find_usb_printer_endpoint(device)
    conn = au.get_usb_manager().openDevice(device)
    if conn is None:
        raise PrinterError("เปิดการเชื่อมต่อเครื่องพิมพ์ USB ไม่สำเร็จ")
    try:
        if not conn.claimInterface(intf, True):
            raise PrinterError("claim interface เครื่องพิมพ์ USB ไม่สำเร็จ (อาจมีแอปอื่นใช้อยู่)")
        try:
            def write(chunk):
                au.bulk_transfer_out(conn, ep, chunk, timeout_ms=10000)

            _paced_write(write, data, CONNECTION_USB)
            ep_in = _find_bulk_in(intf)
            if idle_timeout is None or ep_in is None:
                return False
            is_ftdi = device.getVendorId() == 0x0403  # ชิป FTDI ส่งสถานะ 2 ไบต์นำหน้าทุก packet

            def read_some(timeout_ms):
                got = au.bulk_transfer_in(conn, ep_in, 64, timeout_ms=timeout_ms) or b""
                return got[2:] if is_ftdi else got

            return _wait_printer_idle(write, read_some, idle_timeout)
        finally:
            conn.releaseInterface(intf)
    finally:
        conn.close()


# ---------------------------------------------------------------- Bluetooth

def list_bluetooth_printers():
    return au.list_bonded_bluetooth_devices()


def _send_bluetooth(printer_name, data, idle_timeout=None):
    from jnius import autoclass
    m = _BT_MAC_RE.search(printer_name or "")
    if not m:
        raise PrinterError("ยังไม่ได้เลือกเครื่องพิมพ์ Bluetooth (ต้อง pair ในตั้งค่า Bluetooth ของเครื่องก่อน)")
    mac = m.group(1).upper()

    adapter = au.get_bluetooth_adapter()
    if adapter is None:
        raise PrinterError("อุปกรณ์นี้ไม่มี Bluetooth")
    if not adapter.isEnabled():
        raise PrinterError("Bluetooth ปิดอยู่ กรุณาเปิด Bluetooth ก่อน")
    try:
        adapter.cancelDiscovery()  # การค้นหาอุปกรณ์ที่ค้างอยู่ทำให้ connect ช้า/ล้มเหลว
    except Exception:
        pass

    UUID = autoclass("java.util.UUID")
    device = adapter.getRemoteDevice(mac)
    uuid = UUID.fromString(au.SPP_UUID)

    sock = None
    last_err = None
    for factory in ("createRfcommSocketToServiceRecord", "createInsecureRfcommSocketToServiceRecord"):
        try:
            sock = getattr(device, factory)(uuid)
            sock.connect()
            break
        except Exception as e:
            last_err = e
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            sock = None
    if sock is None:
        raise PrinterError(f"เชื่อมต่อเครื่องพิมพ์ Bluetooth ไม่สำเร็จ: {last_err}")

    try:
        out = sock.getOutputStream()

        def _write(chunk):
            out.write(chunk)
            out.flush()

        _paced_write(_write, data, CONNECTION_BLUETOOTH)
        if idle_timeout is not None:
            inp = sock.getInputStream()

            def read_some(timeout_ms):
                end = time.monotonic() + timeout_ms / 1000.0
                while True:
                    n = inp.available()
                    if n > 0:
                        return bytes((inp.read() & 0xFF) for _ in range(n))
                    if time.monotonic() >= end:
                        return b""
                    time.sleep(0.02)

            try:
                if _wait_printer_idle(_write, read_some, idle_timeout):
                    return True
            except Exception as e:
                print("⚠️ ถามสถานะเครื่องพิมพ์ Bluetooth ไม่สำเร็จ:", e)
        time.sleep(0.5)  # ✅ รอให้ข้อมูลส่งออกหมดก่อนปิด socket (บางรุ่นตัดข้อมูลท้ายทิ้ง)
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Network

def parse_network_address(printer_name):
    text = (printer_name or "").strip()
    if not text:
        raise PrinterError("ยังไม่ได้กรอก IP ของเครื่องพิมพ์ (เช่น 192.168.1.100:9100)")
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        host, port = text, "9100"
    return host, int(port)


def _send_network(printer_name, data, idle_timeout=None):
    host, port = parse_network_address(printer_name)
    try:
        with socket.create_connection((host, port), timeout=10) as s:
            _paced_write(s.sendall, data, CONNECTION_NETWORK)
            if idle_timeout is None:
                return False

            def read_some(timeout_ms):
                s.settimeout(timeout_ms / 1000.0)
                try:
                    return s.recv(64)
                except socket.timeout:
                    return b""

            return _wait_printer_idle(s.sendall, read_some, idle_timeout)
    except OSError as e:
        raise PrinterError(f"เชื่อมต่อเครื่องพิมพ์ {host}:{port} ไม่สำเร็จ: {e}")


# ---------------------------------------------------------------- Public

# ✅ ถ้าส่งงานถัดไปเข้าไปตอนเครื่องยังพิมพ์ค้าง/กำลังตัดกระดาษ หลายรุ่นจะทิ้งไบต์ช่วงต้นของงานใหม่
# -> รูปที่ 2 มีข้อความขยะทับอยู่ข้างบน จึงต้องรอให้เครื่องว่างก่อนส่งงานถัดไป:
#   1) เครื่องที่ตอบ GS r ได้  -> รอจนเครื่องตอบ (= เสร็จจริง) + gap_sec
#   2) เครื่องที่ไม่ตอบ        -> รอตามเวลาพิมพ์โดยประมาณ (print_seconds) + gap_sec
_busy_until = 0.0       # time.monotonic() ที่งานถัดไปส่งได้
DEFAULT_JOB_GAP_SEC = 0.3
_MAX_GAP_SEC = 10.0
_MAX_IDLE_WAIT_SEC = 20.0
_FALLBACK_CUT_SEC = 0.7  # เวลาตัดกระดาษโดยประมาณ (ใช้กับเครื่องที่ไม่ตอบสถานะ)


def send_to_printer(connection_type, printer_name, data: bytes,
                    gap_sec=DEFAULT_JOB_GAP_SEC, print_seconds=0.0):
    """
    gap_sec       = เวลารอเพิ่มหลังเครื่องพิมพ์ทำงานเสร็จ ก่อนส่งงานถัดไป
    print_seconds = เวลาพิมพ์งานนี้โดยประมาณ (ใช้กับเครื่องที่ไม่ตอบสถานะ)
    """
    global _busy_until
    try:
        gap = max(0.0, min(float(gap_sec), _MAX_GAP_SEC))
    except (TypeError, ValueError):
        gap = DEFAULT_JOB_GAP_SEC
    key = (connection_type, printer_name)
    with _PRINT_LOCK:
        wait = _busy_until - time.monotonic()
        if wait > 0:
            print(f"⏳ รอเครื่องพิมพ์ทำงานก่อนหน้าเสร็จ {wait:.1f} วินาที")
            time.sleep(wait)
        start = time.monotonic()
        # เครื่องที่เคยไม่ตอบ ไม่ต้องถามซ้ำ (ไม่ต้องเสียเวลารอ timeout ทุกใบ)
        if _status_supported.get(key) is False:
            idle_timeout = None
        else:
            idle_timeout = min(print_seconds + _IDLE_EXTRA_SEC, _MAX_IDLE_WAIT_SEC)
        idle = False
        try:
            if connection_type == CONNECTION_BLUETOOTH:
                idle = _send_bluetooth(printer_name, data, idle_timeout)
            elif connection_type == CONNECTION_NETWORK:
                idle = _send_network(printer_name, data, idle_timeout)
            else:
                idle = _send_usb(printer_name, data, idle_timeout)
        finally:
            if idle_timeout is not None:
                _status_supported[key] = bool(idle)
            if idle:
                print(f"✅ เครื่องพิมพ์ทำงานเสร็จใน {time.monotonic() - start:.1f} วินาที")
                _busy_until = time.monotonic() + gap
            else:
                _busy_until = max(start + print_seconds, time.monotonic()) + _FALLBACK_CUT_SEC + gap


def list_printers(connection_type):
    """รายชื่อเครื่องพิมพ์ (label) ของประเภทการเชื่อมต่อที่ระบุ"""
    try:
        if connection_type == CONNECTION_BLUETOOTH:
            return [label for label, _ in list_bluetooth_printers()]
        if connection_type == CONNECTION_USB:
            return [label for label, _ in list_usb_printers()]
    except Exception as e:
        print("⚠️ list printers failed:", e)
    return []


def list_all_printers():
    return list_printers(CONNECTION_USB) + list_printers(CONNECTION_BLUETOOTH)
