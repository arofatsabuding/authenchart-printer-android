# android_utils.py
# ==============================================================
# ตัวช่วยเรียก Android API ผ่าน pyjnius (ใช้ได้ทั้งในโปรเซส UI และโปรเซส service)
# ทุกฟังก์ชัน import jnius แบบ lazy — ทำให้ไฟล์นี้ import บน PC ได้ (ไว้ทดสอบ/เช็ค syntax)
# ==============================================================

import os
import sys

PACKAGE_NAME = "org.authenchart.authenapi"          # ต้องตรงกับ package.domain + package.name ใน buildozer.spec
SERVICE_CLASS = PACKAGE_NAME + ".ServicePrintserver"  # p4a สร้างคลาสนี้จาก services = Printserver:service.py
USB_PERMISSION_ACTION = PACKAGE_NAME + ".USB_PERMISSION"

USB_CLASS_PRINTER = 0x07
USB_CLASS_CSCID = 0x0B          # Smart Card (CCID)
USB_ENDPOINT_XFER_BULK = 2
USB_DIR_OUT = 0x00
USB_DIR_IN = 0x80


def is_android():
    return hasattr(sys, "getandroidapilevel") or "ANDROID_ARGUMENT" in os.environ


class NotAndroidError(RuntimeError):
    pass


def _require_android():
    if not is_android():
        raise NotAndroidError("ฟีเจอร์นี้ใช้ได้เฉพาะบน Android เท่านั้น")


def sdk_int():
    _require_android()
    from jnius import autoclass
    return autoclass("android.os.Build$VERSION").SDK_INT


def get_context():
    """คืน Context ที่ใช้ได้ — Activity (โปรเซส UI) หรือ Service (โปรเซส service)"""
    _require_android()
    from jnius import autoclass
    try:
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        if activity is not None:
            return activity
    except Exception:
        pass
    service = autoclass("org.kivy.android.PythonService").mService
    if service is None:
        raise RuntimeError("ไม่พบ Android Context (ทั้ง Activity และ Service)")
    return service


def get_files_dir():
    """โฟลเดอร์ข้อมูลส่วนตัวของแอป — ใช้ร่วมกันได้ทั้งโปรเซส UI และ service"""
    return get_context().getFilesDir().getAbsolutePath()


# ---------------------------------------------------------------- USB

def get_usb_manager():
    _require_android()
    from jnius import autoclass, cast
    Context = autoclass("android.content.Context")
    return cast("android.hardware.usb.UsbManager",
                get_context().getSystemService(Context.USB_SERVICE))


def list_usb_devices():
    _require_android()
    from jnius import cast
    devices = get_usb_manager().getDeviceList().values().toArray()
    return [cast("android.hardware.usb.UsbDevice", d) for d in devices]


def usb_device_label(device):
    """ชื่อที่แสดง/บันทึกใน config เช่น 'USB Smart Card Reader [058F:9540]'"""
    name = None
    try:
        name = device.getProductName()
    except Exception:
        pass
    if not name:
        name = "USB Device"
    return f"{str(name).strip()} [{device.getVendorId():04X}:{device.getProductId():04X}]"


def has_usb_permission(device):
    return bool(get_usb_manager().hasPermission(device))


def request_usb_permission(device):
    """เปิดหน้าต่างขออนุญาตใช้ USB ของระบบ (ผลอนุญาตผูกกับแอป ใช้ได้ทั้ง UI และ service)"""
    from jnius import autoclass
    Intent = autoclass("android.content.Intent")
    PendingIntent = autoclass("android.app.PendingIntent")
    ctx = get_context()
    intent = Intent(USB_PERMISSION_ACTION)
    intent.setPackage(ctx.getPackageName())
    flags = 0x02000000 if sdk_int() >= 31 else 0  # FLAG_MUTABLE (ระบบต้องเติม extra ลงไป)
    pending = PendingIntent.getBroadcast(ctx, 0, intent, flags)
    get_usb_manager().requestPermission(device, pending)


def iter_endpoints(interface):
    for i in range(interface.getEndpointCount()):
        yield interface.getEndpoint(i)


def iter_interfaces(device):
    for i in range(device.getInterfaceCount()):
        yield device.getInterface(i)


def bulk_transfer_out(connection, endpoint, data, timeout_ms=5000, chunk_size=16384):
    """ส่ง bytes ออกทาง bulk OUT (แบ่ง chunk — Android รุ่นเก่ารับได้ไม่เกิน 16KB ต่อครั้ง)"""
    data = bytes(data)
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        sent = connection.bulkTransfer(endpoint, chunk, len(chunk), timeout_ms)
        if sent is None or sent < 0:
            raise IOError("ส่งข้อมูลทาง USB ไม่สำเร็จ (bulkTransfer ล้มเหลว/หมดเวลา)")
        if sent == 0:
            raise IOError("ส่งข้อมูลทาง USB ไม่สำเร็จ (ส่งได้ 0 ไบต์)")
        offset += sent
    return offset


def bulk_transfer_in(connection, endpoint, size, timeout_ms=5000):
    """
    อ่าน bytes จาก bulk IN — ส่ง Python list เข้าไปเป็น byte[] แล้ว pyjnius จะคัดลอก
    ค่ากลับมาใส่ list ให้หลังเรียกเสร็จ (pass-by-reference) ค่าที่ได้เป็น signed byte จึงต้อง & 0xFF
    """
    buf = [0] * size
    n = connection.bulkTransfer(endpoint, buf, size, timeout_ms)
    if n is None or n < 0:
        return None
    return bytes((b & 0xFF) for b in buf[:n])


# ---------------------------------------------------------------- Bluetooth

SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"


def get_bluetooth_adapter():
    _require_android()
    from jnius import autoclass
    return autoclass("android.bluetooth.BluetoothAdapter").getDefaultAdapter()


def list_bonded_bluetooth_devices():
    """คืน [(label, mac)] ของอุปกรณ์ Bluetooth ที่จับคู่ (pair) ไว้แล้วในเครื่อง"""
    adapter = get_bluetooth_adapter()
    from jnius import cast
    if adapter is None or not adapter.isEnabled():
        return []
    result = []
    for d in adapter.getBondedDevices().toArray():
        dev = cast("android.bluetooth.BluetoothDevice", d)
        name = None
        try:
            name = dev.getName()
        except Exception:
            pass
        mac = dev.getAddress()
        result.append((f"{name or 'Bluetooth'} [{mac}]", mac))
    return result


# ---------------------------------------------------------------- อื่นๆ

def open_url(url):
    """เปิดลิงก์ด้วยเบราว์เซอร์ของเครื่อง (ใช้ตอนดาวน์โหลด APK เวอร์ชันใหม่)"""
    from jnius import autoclass
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    get_context().startActivity(intent)
