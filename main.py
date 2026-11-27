# main.py
# ==============================================================
# AuthenAPI (Android) — หน้าตั้งค่า (Kivy) แทน System Tray + tkinter ของเวอร์ชัน Windows
# - เปิดแอปครั้งแรกจะสตาร์ท Foreground Service (service.py) ที่รัน Flask server พอร์ต 9333
# - ปิดหน้าแอปได้ เซิร์ฟเวอร์ยังทำงานต่อ (มีแจ้งเตือนค้างที่แถบด้านบน)
# ==============================================================

import base64
import io
import json
import os
import ssl
import threading
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ ฟอนต์ไทย — Kivy ใช้ Roboto เป็นค่าเริ่มต้นซึ่งไม่มีอักษรไทย จึงลงทะเบียน Sarabun ทับชื่อ "Roboto"
from kivy.core.text import LabelBase  # noqa: E402

_FONT_REGULAR = os.path.join(BASE_DIR, "fonts", "Sarabun-Regular.ttf")
_FONT_BOLD = os.path.join(BASE_DIR, "fonts", "Sarabun-Bold.ttf")
if os.path.exists(_FONT_REGULAR):
    LabelBase.register(
        name="Roboto",
        fn_regular=_FONT_REGULAR,
        fn_bold=_FONT_BOLD if os.path.exists(_FONT_BOLD) else _FONT_REGULAR,
    )

from kivy.app import App  # noqa: E402
from kivy.clock import Clock, mainthread  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.lang import Builder  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.properties import StringProperty  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivy.uix.gridlayout import GridLayout  # noqa: E402
from kivy.uix.image import Image as KivyImage  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.popup import Popup  # noqa: E402
from kivy.uix.scrollview import ScrollView  # noqa: E402

import android_utils as au  # noqa: E402
import config_store  # noqa: E402
import printer_transport as pt  # noqa: E402
from smartcard_reader import ThaiIDCardReader  # noqa: E402

APP_VERSION = "v2.0.7"
PORT = 9333
LOCAL_API = f"http://127.0.0.1:{PORT}"

# ✅ อัปเดตผ่าน GitHub Releases (repo เดียวกับเวอร์ชัน Windows) — แนบไฟล์ชื่อนี้ไปกับ release
GITHUB_REPO = "arofatsabuding/authenchart-printer-android"
UPDATE_ASSET_NAME = "AuthenAPI.apk"

CONNECTIONS = [
    (pt.CONNECTION_USB, "USB (สาย OTG)"),
    (pt.CONNECTION_BLUETOOTH, "Bluetooth"),
    (pt.CONNECTION_NETWORK, "LAN / Wi-Fi (IP)"),
]
NONE_TEXT = "(ไม่พบอุปกรณ์ — กดรีเฟรช)"

FIELD_LABELS = [
    ("pid", "เลขบัตรประชาชน"),
    ("titleName", "คำนำหน้า (ไทย)"),
    ("fname", "ชื่อ (ไทย)"),
    ("mname", "ชื่อกลาง (ไทย)"),
    ("lname", "นามสกุล (ไทย)"),
    ("titleNameEng", "คำนำหน้า (Eng)"),
    ("fnameEng", "ชื่อ (Eng)"),
    ("mnameEng", "ชื่อกลาง (Eng)"),
    ("lnameEng", "นามสกุล (Eng)"),
    ("birthDate", "วันเกิด (พ.ศ.)"),
    ("sex", "เพศ"),
    ("cardIssuer", "หน่วยงานที่ออกบัตร"),
    ("issueDate", "วันออกบัตร (พ.ศ.)"),
    ("expireDate", "วันหมดอายุ (พ.ศ.)"),
    ("address", "ที่อยู่ (เต็ม)"),
    ("houseNo", "บ้านเลขที่"),
    ("villageNo", "หมู่ที่"),
    ("laneRoad", "ตรอก/ซอย/ถนน"),
    ("subDistrict", "ตำบล/แขวง"),
    ("district", "อำเภอ/เขต"),
    ("province", "จังหวัด"),
]


# =========================
# HTTP helpers (urllib — ไม่ต้องพึ่ง requests บน Android)
# =========================
def http_json(method, url, payload=None, timeout=10, headers=None, context=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode("utf-8", "ignore")}


def _https_context():
    # Python บน Android ไม่มีชุด CA ของระบบ — ใช้ certifi แทน
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# =========================
# AUTO-UPDATE (ผ่าน GitHub Releases)
# =========================
def _parse_version(v):
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update():
    """คืนค่า: (has_update, latest_version, download_url, error_message)"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        status, data = http_json("GET", url, timeout=10, context=_https_context(),
                                 headers={"User-Agent": "AuthenAPI-Android",
                                          "Accept": "application/vnd.github+json"})
        if status != 200:
            return False, None, None, f"เช็คอัปเดตไม่สำเร็จ (HTTP {status})"
        latest_tag = data.get("tag_name", "")
        if not latest_tag:
            return False, None, None, "ไม่พบข้อมูลเวอร์ชันจาก GitHub"
        download_url = None
        for asset in data.get("assets", []):
            if asset.get("name") == UPDATE_ASSET_NAME:
                download_url = asset.get("browser_download_url")
                break
        if not download_url:
            return False, latest_tag, None, f"ไม่พบไฟล์ {UPDATE_ASSET_NAME} แนบใน release ล่าสุด"
        return _parse_version(latest_tag) > _parse_version(APP_VERSION), latest_tag, download_url, None
    except Exception as e:
        return False, None, None, f"เช็คอัปเดตไม่สำเร็จ: {e}"


# =========================
# SERVICE CONTROL
# =========================
_desktop_server_started = False


def start_server_service():
    global _desktop_server_started
    if au.is_android():
        from jnius import autoclass
        service = autoclass(au.SERVICE_CLASS)
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        service.start(activity, "")
    elif not _desktop_server_started:
        # ✅ รันบน PC (ไว้ทดสอบ UI/พิมพ์ผ่าน LAN) — ใช้ thread แทน service
        _desktop_server_started = True
        from server import run_server
        threading.Thread(target=run_server, daemon=True).start()


def stop_server_service():
    if au.is_android():
        from jnius import autoclass
        service = autoclass(au.SERVICE_CLASS)
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        service.stop(activity)


def request_runtime_permissions(on_done):
    """Bluetooth (Android 12+) และการแจ้งเตือน (Android 13+) ต้องขอตอนใช้งานจริง"""
    if not au.is_android():
        on_done()
        return
    try:
        from android.permissions import request_permissions
        perms = []
        sdk = au.sdk_int()
        if sdk >= 31:
            perms.append("android.permission.BLUETOOTH_CONNECT")
        if sdk >= 33:
            perms.append("android.permission.POST_NOTIFICATIONS")
        if not perms:
            on_done()
            return
        request_permissions(perms, lambda permissions, grants: on_done())
    except Exception as e:
        print("⚠️ request permissions failed:", e)
        on_done()


# =========================
# POPUPS
# =========================
def _wrapped_label(text, **kwargs):
    lbl = Label(text=text, size_hint_y=None, halign="left", valign="top", **kwargs)
    lbl.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
    lbl.bind(texture_size=lambda w, v: setattr(w, "height", v[1] + dp(6)))
    return lbl


@mainthread
def show_message(title, text, buttons=None):
    """buttons: [(ข้อความ, callback)] — ไม่ใส่ = มีปุ่ม 'ตกลง' ปุ่มเดียว"""
    content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
    scroll = ScrollView()
    scroll.add_widget(_wrapped_label(text))
    content.add_widget(scroll)

    row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
    popup = Popup(title=title, content=content, size_hint=(0.92, 0.5), auto_dismiss=True)
    for caption, callback in (buttons or [("ตกลง", None)]):
        btn = Button(text=caption)

        def _on_press(_btn, cb=callback):
            popup.dismiss()
            if cb:
                cb()
        btn.bind(on_press=_on_press)
        row.add_widget(btn)
    content.add_widget(row)
    popup.open()


@mainthread
def show_card_data(data):
    content = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
    scroll = ScrollView()
    grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(4), padding=dp(4))
    grid.bind(minimum_height=grid.setter("height"))

    if data.get("image"):
        try:
            from kivy.core.image import Image as CoreImage
            img_bytes = base64.b64decode(data["image"])
            texture = CoreImage(io.BytesIO(img_bytes), ext="jpg").texture
            grid.add_widget(KivyImage(texture=texture, size_hint_y=None, height=dp(200)))
        except Exception as e:
            grid.add_widget(_wrapped_label(f"(โหลดรูปไม่สำเร็จ: {e})", color=(1, 0.4, 0.4, 1)))

    for key, label in FIELD_LABELS:
        grid.add_widget(_wrapped_label(f"[b]{label}:[/b] {data.get(key) or '-'}", markup=True))

    scroll.add_widget(grid)
    content.add_widget(scroll)
    popup = Popup(title="ผลการทดสอบอ่านบัตรประชาชน", content=content, size_hint=(0.95, 0.9))
    close_btn = Button(text="ปิด", size_hint_y=None, height=dp(48))
    close_btn.bind(on_press=lambda *_: popup.dismiss())
    content.add_widget(close_btn)
    popup.open()


# =========================
# UI
# =========================
KV = """
#:import dp kivy.metrics.dp

<Caption@Label>:
    size_hint_y: None
    height: max(dp(30), self.texture_size[1] + dp(6))
    text_size: self.width, None
    halign: 'left'
    valign: 'middle'
    color: 0.15, 0.15, 0.15, 1

<Heading@Caption>:
    height: dp(44)
    font_size: '19sp'
    bold: True
    color: 0.05, 0.45, 0.3, 1

<Btn@Button>:
    size_hint_y: None
    height: dp(50)
    halign: 'center'
    text_size: self.width - dp(8), None

<Field@TextInput>:
    size_hint_y: None
    height: dp(46)
    multiline: False
    font_size: '16sp'
    padding: [dp(10), dp(12)]

<Pick@Spinner>:
    size_hint_y: None
    height: dp(46)
    text_size: self.width - dp(16), None
    shorten: True
    shorten_from: 'right'

<Settings>:
    canvas.before:
        Color:
            rgba: 1, 1, 1, 1
        Rectangle:
            pos: self.pos
            size: self.size
    ScrollView:
        do_scroll_x: False
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(6)

            Caption:
                text: 'ตั้งค่า AuthenChart Printer (' + root.app_version + ')'
                font_size: '21sp'
                bold: True
                height: dp(50)
            Caption:
                id: server_status
                text: 'เซิร์ฟเวอร์: กำลังตรวจสอบ...'
            GridLayout:
                cols: 2
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(8)
                Btn:
                    text: 'รีสตาร์ทเซิร์ฟเวอร์'
                    on_press: root.restart_server()
                Btn:
                    text: 'ขออนุญาต USB'
                    on_press: root.request_all_usb_permissions()

            Heading:
                text: 'เครื่องพิมพ์'
            BoxLayout:
                size_hint_y: None
                height: dp(46)
                Caption:
                    text: 'เปิดใช้งานเครื่องพิมพ์ (ถ้าปิด API /print จะไม่รับงานพิมพ์)'
                Switch:
                    id: printer_enabled
                    size_hint_x: None
                    width: dp(90)
                    on_active: root.on_toggle_printer(self.active)
            Caption:
                text: 'ประเภท: ESC/POS Thermal (RAW Mode)'
                color: 0.1, 0.55, 0.2, 1
            Caption:
                text: 'การเชื่อมต่อ:'
            Pick:
                id: conn_spinner
                on_text: root.on_connection_changed(self.text)
            Caption:
                id: printer_caption
                text: 'เครื่องพิมพ์:'
            Pick:
                id: printer_spinner
            Field:
                id: network_input
                hint_text: 'IP เครื่องพิมพ์ เช่น 192.168.1.100:9100'
            Caption:
                text: 'ขนาดกระดาษ (mm):'
            Pick:
                id: paper_spinner
                values: ['58', '80']
            Caption:
                text: 'ระยะ feed ก่อนตัด (px):'
            Field:
                id: feed_input
                input_filter: 'int'
            BoxLayout:
                size_hint_y: None
                height: dp(58)
                Caption:
                    text: 'รอเครื่องพิมพ์พิมพ์เสร็จก่อนส่งใบถัดไป (เปิดถ้าใบที่ 2 มีข้อความขยะ / ปิด = พิมพ์เร็ว)'
                Switch:
                    id: wait_done_enabled
                    size_hint_x: None
                    width: dp(90)
                    on_active: root.on_toggle_wait_done(self.active)
            Caption:
                text: 'เวลารอเพิ่มระหว่างใบ (วินาที) — ใช้ตอนเปิดสวิตช์ด้านบน:'
            Field:
                id: gap_input
                input_filter: 'float'
            GridLayout:
                cols: 3
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(8)
                Btn:
                    text: 'รีเฟรช'
                    on_press: root.refresh_printers()
                Btn:
                    text: 'บันทึก'
                    on_press: root.save_printer()
                Btn:
                    id: test_print_btn
                    text: 'ทดสอบพิมพ์'
                    on_press: root.test_print()

            Heading:
                text: 'เครื่องอ่านบัตรประชาชน'
            Caption:
                text: 'เครื่องอ่านบัตร (USB CCID):'
            Pick:
                id: reader_spinner
            BoxLayout:
                size_hint_y: None
                height: dp(46)
                Caption:
                    text: 'อ่านรูปประจำตัว (ถ้าปิด จะส่งรูปเป็นค่าว่าง และอ่านบัตรเร็วขึ้น)'
                Switch:
                    id: read_image_enabled
                    size_hint_x: None
                    width: dp(90)
            GridLayout:
                cols: 3
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(8)
                Btn:
                    text: 'รีเฟรช'
                    on_press: root.refresh_readers()
                Btn:
                    text: 'บันทึก'
                    on_press: root.save_smartcard()
                Btn:
                    text: 'ทดสอบอ่านบัตร'
                    on_press: root.test_read_card()

            Heading:
                text: 'อื่นๆ'
            Btn:
                text: 'ตรวจสอบอัปเดต'
                on_press: root.check_update()
            Caption:
                text: 'API: http://127.0.0.1:9333  •  ' + root.app_version
                color: 0.5, 0.5, 0.5, 1
                font_size: '13sp'
"""


class Settings(BoxLayout):
    app_version = StringProperty(APP_VERSION)

    def on_kv_post(self, base_widget):
        self.card_reader = ThaiIDCardReader()  # ใช้แค่อ่าน/เขียน config + list เครื่องอ่าน
        self.load_from_config()

    # ---------------- helpers ----------------

    @staticmethod
    def _set_visible(widget, visible, height):
        widget.opacity = 1 if visible else 0
        widget.disabled = not visible
        widget.height = height if visible else 0

    def _connection_key(self):
        for key, label in CONNECTIONS:
            if label == self.ids.conn_spinner.text:
                return key
        return pt.CONNECTION_USB

    @staticmethod
    def _spinner_value(spinner):
        text = (spinner.text or "").strip()
        return "" if text == NONE_TEXT else text

    @staticmethod
    def _fill_spinner(spinner, values, selected=None):
        spinner.values = values
        if selected and selected in values:
            spinner.text = selected
        elif selected and not values:
            spinner.text = selected  # ค่าที่บันทึกไว้ (เครื่องอาจยังไม่ได้เสียบ)
        elif values:
            spinner.text = values[0]
        else:
            spinner.text = NONE_TEXT

    # ---------------- load / refresh ----------------

    def load_from_config(self):
        cfg = config_store.load_config()
        self.ids.conn_spinner.values = [label for _, label in CONNECTIONS]
        conn = cfg.get("printer_connection", pt.CONNECTION_USB)
        self.ids.conn_spinner.text = dict(CONNECTIONS).get(conn, CONNECTIONS[0][1])
        self.ids.paper_spinner.text = str(cfg.get("paper_size", "80"))
        self.ids.feed_input.text = str(cfg.get("feed_px", 20))
        self.ids.gap_input.text = str(cfg.get("job_gap_sec", pt.DEFAULT_JOB_GAP_SEC))
        self.ids.wait_done_enabled.active = bool(cfg.get("wait_print_done", True))
        self.ids.printer_enabled.active = bool(cfg.get("printer_enabled", True))
        if conn == pt.CONNECTION_NETWORK:
            self.ids.network_input.text = cfg.get("printer_name") or ""
        self.on_connection_changed(self.ids.conn_spinner.text, selected=cfg.get("printer_name"))
        self.refresh_readers()
        self.on_toggle_printer(self.ids.printer_enabled.active)

    def refresh_all(self):
        self.refresh_printers()
        self.refresh_readers()

    def on_connection_changed(self, _text, selected=None):
        conn = self._connection_key()
        is_network = conn == pt.CONNECTION_NETWORK
        self._set_visible(self.ids.printer_spinner, not is_network, dp(46))
        self._set_visible(self.ids.network_input, is_network, dp(46))
        self.ids.printer_caption.text = "IP:Port เครื่องพิมพ์:" if is_network else "เครื่องพิมพ์:"
        if conn == pt.CONNECTION_BLUETOOTH:
            self.ids.printer_caption.text = "เครื่องพิมพ์ (ต้อง pair ในตั้งค่า Bluetooth ก่อน):"
        if not is_network:
            if selected is None:
                selected = config_store.load_config().get("printer_name")
            self.refresh_printers(selected=selected)
        self.on_toggle_printer(self.ids.printer_enabled.active)

    def refresh_printers(self, selected=None):
        conn = self._connection_key()
        if conn == pt.CONNECTION_NETWORK:
            return
        if selected is None:
            selected = self._spinner_value(self.ids.printer_spinner)
        self._fill_spinner(self.ids.printer_spinner, pt.list_printers(conn), selected)

    def refresh_readers(self):
        try:
            readers = self.card_reader.list_readers()
        except Exception as e:
            readers = []
            print("⚠️ list readers failed:", e)
        sc_cfg = self.card_reader.get_smartcard_config()
        self._fill_spinner(self.ids.reader_spinner, readers, sc_cfg.get("selected_reader"))
        self.ids.read_image_enabled.active = bool(sc_cfg.get("read_image", True))

    def on_toggle_printer(self, active):
        for wid in ("conn_spinner", "printer_spinner", "network_input", "paper_spinner", "feed_input", "gap_input",
                    "wait_done_enabled"):
            w = self.ids[wid]
            if w.opacity:  # ช่องที่ถูกซ่อนอยู่ให้คง disabled ไว้
                w.disabled = not active
        self.on_toggle_wait_done(self.ids.wait_done_enabled.active)
        # ✅ disable แค่ปุ่ม "ทดสอบพิมพ์" — ปุ่ม "บันทึก" ต้องกดได้เสมอ ไม่งั้นกลับมาเปิดไม่ได้
        self.ids.test_print_btn.disabled = not active

    def on_toggle_wait_done(self, active):
        # ช่องเวลารอใช้เฉพาะตอนเปิด "รอเครื่องพิมพ์พิมพ์เสร็จ"
        self.ids.gap_input.disabled = not (active and self.ids.printer_enabled.active)

    # ---------------- USB permission ----------------

    def _usb_devices_by_label(self):
        devices = {}
        try:
            from usb_ccid import list_ccid_devices
            for label, dev in pt.list_usb_printers() + list_ccid_devices():
                devices[label] = dev
        except Exception as e:
            print("⚠️ list usb failed:", e)
        return devices

    def _ensure_usb_permission(self, label):
        dev = self._usb_devices_by_label().get(label)
        if dev is not None and not au.has_usb_permission(dev):
            au.request_usb_permission(dev)

    def request_all_usb_permissions(self):
        if not au.is_android():
            show_message("ขออนุญาต USB", "ใช้ได้เฉพาะบน Android")
            return
        pending = [(label, dev) for label, dev in self._usb_devices_by_label().items()
                   if not au.has_usb_permission(dev)]
        if not pending:
            show_message("ขออนุญาต USB", "อุปกรณ์ USB ทั้งหมดได้รับอนุญาตแล้ว (หรือยังไม่ได้เสียบอุปกรณ์)")
            return
        # ✅ ระบบแสดงได้ทีละหน้าต่าง — ขอเครื่องแรกก่อน กดปุ่มนี้ซ้ำเพื่อขอเครื่องถัดไป
        label, dev = pending[0]
        au.request_usb_permission(dev)
        more = f"\n\nยังเหลืออีก {len(pending) - 1} เครื่อง — กดปุ่มนี้อีกครั้งหลังอนุญาตเครื่องนี้" if len(pending) > 1 else ""
        show_message("ขออนุญาต USB", f"กรุณากด 'อนุญาต' ให้กับ:\n{label}{more}")

    # ---------------- printer actions ----------------

    def _collect_printer_config(self):
        conn = self._connection_key()
        if conn == pt.CONNECTION_NETWORK:
            name = self.ids.network_input.text.strip()
        else:
            name = self._spinner_value(self.ids.printer_spinner)
        try:
            feed = int(self.ids.feed_input.text or 20)
        except ValueError:
            feed = 20
        try:
            gap = max(0.0, min(float(self.ids.gap_input.text or pt.DEFAULT_JOB_GAP_SEC), 10.0))
        except ValueError:
            gap = pt.DEFAULT_JOB_GAP_SEC
        return {
            "printer_type": "ESC/POS Thermal",
            "printer_connection": conn,
            "printer_name": name,
            "paper_size": self.ids.paper_spinner.text,
            "feed_px": feed,
            "job_gap_sec": gap,
            "wait_print_done": self.ids.wait_done_enabled.active,
            "printer_enabled": self.ids.printer_enabled.active,
        }

    def save_printer(self, silent=False):
        cfg_new = self._collect_printer_config()
        config_store.save_config(cfg_new)
        if cfg_new["printer_connection"] == pt.CONNECTION_USB and cfg_new["printer_name"]:
            try:
                self._ensure_usb_permission(cfg_new["printer_name"])
            except Exception as e:
                print("⚠️ usb permission:", e)
        if not silent:
            show_message("บันทึกแล้ว", "บันทึกการตั้งค่าเครื่องพิมพ์เรียบร้อย")

    def test_print(self):
        self.save_printer(silent=True)  # ✅ ทดสอบด้วยค่าที่เห็นบนหน้าจอ (บันทึกให้ก่อน)
        paper = self.ids.paper_spinner.text
        feed = self.ids.feed_input.text
        threading.Thread(target=self._test_print_worker, args=(paper, feed), daemon=True).start()

    def _test_print_worker(self, paper, feed):
        from PIL import Image, ImageDraw, ImageFont
        width = 384 if paper == "58" else 576
        img = Image.new("RGB", (width, 200), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(_FONT_REGULAR, 30)
        except Exception:
            font = ImageFont.load_default()
        draw.text((30, 40), f"ESC/POS Test Print (Android)\nทดสอบพิมพ์  Feed={feed}px", fill="black", font=font)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        payload = {"imageBase64": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                   "wait": True}  # รอผลพิมพ์จริง จะได้บอกได้ว่าสำเร็จไหม
        try:
            status, data = http_json("POST", f"{LOCAL_API}/print", payload, timeout=30)
            if status == 200:
                show_message("สำเร็จ", "พิมพ์ทดสอบและตัดกระดาษสำเร็จ!")
            else:
                show_message("ผิดพลาด", f"พิมพ์ไม่สำเร็จ ({status}): {data.get('error', '')}")
        except Exception as e:
            show_message("ผิดพลาด", f"เรียกเซิร์ฟเวอร์ไม่สำเร็จ: {e}")

    # ---------------- smartcard actions ----------------

    def save_smartcard(self):
        selected = self._spinner_value(self.ids.reader_spinner)
        if not selected:
            show_message("แจ้งเตือน", "กรุณาเลือกเครื่องอ่านบัตรก่อนบันทึก")
            return
        try:
            self.card_reader.set_smartcard_reader(selected, read_image=self.ids.read_image_enabled.active)
            self._ensure_usb_permission(selected)
            show_message("บันทึกแล้ว", "บันทึกการตั้งค่าเครื่องอ่านบัตรเรียบร้อย")
        except Exception as e:
            show_message("ผิดพลาด", f"บันทึกเครื่องอ่านบัตรไม่สำเร็จ: {e}")

    def test_read_card(self):
        threading.Thread(target=self._test_read_worker, daemon=True).start()

    def _test_read_worker(self):
        try:
            status, data = http_json("GET", f"{LOCAL_API}/api/smartcard/read-card-only?readImageFlag=true",
                                     timeout=15)
        except Exception as e:
            show_message("ผิดพลาด", f"เรียก API ไม่สำเร็จ: {e}")
            return
        if status != 200:
            msg = data.get("message") or data.get("error") or f"HTTP {status}"
            if "Card not present" in msg:
                msg = "ไม่มีบัตรเสียบอยู่ในเครื่องอ่าน (หรือยังไม่ได้เลือก/อนุญาตเครื่องอ่านบัตร)"
            show_message("อ่านบัตรไม่สำเร็จ", msg)
            return
        show_card_data(data)

    # ---------------- server ----------------

    def check_server_async(self, *_):
        threading.Thread(target=self._check_server, daemon=True).start()

    def _check_server(self):
        try:
            status, _ = http_json("GET", f"{LOCAL_API}/ping", timeout=2)
            ok = status == 200
        except Exception:
            ok = False
        self._set_server_status(ok)

    @mainthread
    def _set_server_status(self, ok):
        lbl = self.ids.server_status
        if ok:
            lbl.text = f"เซิร์ฟเวอร์: ทำงานอยู่ (พอร์ต {PORT})"
            lbl.color = (0.1, 0.55, 0.2, 1)
        else:
            lbl.text = "เซิร์ฟเวอร์: ไม่ทำงาน — กด 'รีสตาร์ทเซิร์ฟเวอร์'"
            lbl.color = (0.8, 0.15, 0.15, 1)

    def restart_server(self):
        self.ids.server_status.text = "เซิร์ฟเวอร์: กำลังรีสตาร์ท..."
        try:
            stop_server_service()
        except Exception as e:
            print("⚠️ stop service:", e)
        Clock.schedule_once(lambda dt: start_server_service(), 1.5)

    # ---------------- update ----------------

    def check_update(self, silent_if_no_update=False):
        threading.Thread(target=self._update_worker, args=(silent_if_no_update,), daemon=True).start()

    def _update_worker(self, silent_if_no_update):
        has_update, latest, url, err = check_for_update()
        if err:
            if not silent_if_no_update:
                show_message("เช็คอัปเดตไม่สำเร็จ", err)
            return
        if not has_update:
            if not silent_if_no_update:
                show_message("ตรวจสอบอัปเดต", f"ใช้เวอร์ชันล่าสุดอยู่แล้ว ({APP_VERSION})")
            return

        def _download():
            try:
                au.open_url(url)
            except Exception as e:
                show_message("ผิดพลาด", f"เปิดลิงก์ดาวน์โหลดไม่สำเร็จ: {e}\n\n{url}")

        show_message(
            "พบเวอร์ชันใหม่",
            f"พบเวอร์ชันใหม่ {latest} (ปัจจุบัน {APP_VERSION})\n\n"
            f"กด 'ดาวน์โหลด' เพื่อดาวน์โหลดไฟล์ APK แล้วเปิดไฟล์เพื่อติดตั้งทับ "
            f"(การตั้งค่าเดิมจะยังอยู่)",
            buttons=[("ดาวน์โหลด", _download), ("ภายหลัง", None)],
        )


class AuthenAPIApp(App):
    title = f"AuthenAPI ({APP_VERSION})"
    icon = "app.png"

    def build(self):
        Window.clearcolor = (1, 1, 1, 1)
        Builder.load_string(KV)
        return Settings()

    def on_start(self):
        try:
            start_server_service()
        except Exception as e:
            show_message("ผิดพลาด", f"สตาร์ทเซิร์ฟเวอร์ไม่สำเร็จ: {e}")
        request_runtime_permissions(self._after_permissions)
        Clock.schedule_interval(self.root.check_server_async, 3)
        Clock.schedule_once(self.root.check_server_async, 1)
        Clock.schedule_once(lambda dt: self.root.check_update(silent_if_no_update=True), 4)

    @mainthread
    def _after_permissions(self):
        self.root.refresh_all()

    def on_pause(self):
        return True  # ✅ สลับไปแอปอื่นได้โดยไม่ปิดแอป (เซิร์ฟเวอร์อยู่ใน service แยกอยู่แล้ว)

    def on_resume(self):
        self.root.refresh_all()


if __name__ == "__main__":
    AuthenAPIApp().run()
