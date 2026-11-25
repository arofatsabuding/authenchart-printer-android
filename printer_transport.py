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


def _send_usb(printer_name, data):
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
            _paced_write(lambda chunk: au.bulk_transfer_out(conn, ep, chunk, timeout_ms=10000),
                         data, CONNECTION_USB)
        finally:
            conn.releaseInterface(intf)
    finally:
        conn.close()


# ---------------------------------------------------------------- Bluetooth

def list_bluetooth_printers():
    return au.list_bonded_bluetooth_devices()


def _send_bluetooth(printer_name, data):
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
        time.sleep(0.5)  # ✅ รอให้ข้อมูลส่งออกหมดก่อนปิด socket (บางรุ่นตัดข้อมูลท้ายทิ้ง)
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


def _send_network(printer_name, data):
    host, port = parse_network_address(printer_name)
    try:
        with socket.create_connection((host, port), timeout=10) as s:
            _paced_write(s.sendall, data, CONNECTION_NETWORK)
    except OSError as e:
        raise PrinterError(f"เชื่อมต่อเครื่องพิมพ์ {host}:{port} ไม่สำเร็จ: {e}")


# ---------------------------------------------------------------- Public

def send_to_printer(connection_type, printer_name, data: bytes):
    with _PRINT_LOCK:
        if connection_type == CONNECTION_BLUETOOTH:
            _send_bluetooth(printer_name, data)
        elif connection_type == CONNECTION_NETWORK:
            _send_network(printer_name, data)
        else:
            _send_usb(printer_name, data)


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
