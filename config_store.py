# config_store.py
# ==============================================================
# จัดการ config.json (ใช้ร่วมกันระหว่างโปรเซส UI กับโปรเซส service)
# บน Android เก็บไว้ในโฟลเดอร์ข้อมูลส่วนตัวของแอป (getFilesDir) — ไม่หายตอนอัปเดตแอป
# ==============================================================

import json
import os

from android_utils import is_android, get_files_dir

DEFAULTS = {
    "printer_type": "ESC/POS Thermal",
    "printer_connection": "usb",   # "usb" | "bluetooth" | "network"
    "printer_name": "",            # usb: "ชื่อ [VID:PID]" | bluetooth: "ชื่อ [MAC]" | network: "ip:port"
    "paper_size": "80",            # "58" | "80"
    "feed_px": 20,
    "job_gap_sec": 0.3,            # เวลารอเพิ่มหลังเครื่องพิมพ์เสร็จ (วินาที) — เพิ่มถ้าใบที่ 2 มีข้อความขยะ
    "printer_enabled": True,
}

_config_file = None


def get_config_file():
    global _config_file
    if _config_file is None:
        if is_android():
            base = get_files_dir()
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        _config_file = os.path.join(base, "config.json")
    return _config_file


def read_raw():
    try:
        with open(get_config_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_raw(data):
    """เขียนแบบ atomic (ไฟล์ชั่วคราว + replace) กันอีกโปรเซสอ่านเจอไฟล์ครึ่งๆ กลางๆ"""
    path = get_config_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = DEFAULTS.copy()
    saved = read_raw()
    cfg.update({k: v for k, v in saved.items() if k in cfg})
    return cfg


def save_config(cfg):
    """Merge กับค่าเดิมในไฟล์ (เช่น key "smartcard") แทนการเขียนทับทั้งไฟล์ — เหมือนเวอร์ชัน Windows"""
    existing = read_raw()
    existing.update(cfg)
    write_raw(existing)
