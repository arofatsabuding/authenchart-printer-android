# smartcard_reader.py
# ==============================================================
# Thai National ID Smart Card Reader — เวอร์ชัน Android (USB CCID ผ่าน usb_ccid.py)
# ==============================================================
# API ฝั่ง HTTP เหมือนเวอร์ชัน Windows ทุกอย่าง (path / รูปแบบ JSON / error envelope)
# ต่างกันแค่ชั้นล่าง: Windows ใช้ PC/SC (pyscard) ส่วน Android คุยกับเครื่องอ่านผ่าน USB Host โดยตรง
# และใช้การ poll สถานะช่องเสียบบัตรทุก 0.2 วินาทีแทน CardMonitor ของ pyscard

import base64
import os
import threading
import time
from datetime import datetime, timezone

import android_utils as au
import config_store
from usb_ccid import (CcidReader, CcidError, CcidNoCardError, CcidPermissionError,
                      list_ccid_devices)

# ✅ เครื่องอ่านบัตรรับได้ทีละ 1 คำสั่งเท่านั้น — lock นี้บังคับให้ทำทีละงาน (poll / อ่านบัตร)
_SMARTCARD_LOCK = threading.Lock()

# ✅ หน่วงเวลาสั้นๆ ระหว่างคำสั่ง APDU ตอนอ่านรูป (40 คำสั่งติดกัน)
_APDU_DELAY_SEC = 0.01

# ✅ ความถี่ในการเช็คว่ามีการเสียบ/ถอดบัตร (แทน CardMonitor ของ pyscard)
# GET_SLOT_STATUS เป็นคำสั่งเล็กมาก (ไปกลับ ~1-2ms) ถามถี่ได้ — ยิ่งถี่ หน้าเว็บยิ่งเห็นบัตรเร็ว
_POLL_INTERVAL_SEC = 0.2

# --------------------------------------------------------------
# APDU Commands (มาตรฐานสำหรับบัตรประชาชนไทย - Thai National ID Card)
# --------------------------------------------------------------
CMD_SELECT_APPLET = [0x00, 0xA4, 0x04, 0x00, 0x08,
                     0xA0, 0x00, 0x00, 0x00, 0x54, 0x48, 0x00, 0x01]

CMD_CID          = [0x80, 0xB0, 0x00, 0x04, 0x02, 0x00, 0x0D]
CMD_FULLNAME_TH  = [0x80, 0xB0, 0x00, 0x11, 0x02, 0x00, 0x64]
CMD_FULLNAME_EN  = [0x80, 0xB0, 0x00, 0x75, 0x02, 0x00, 0x64]
CMD_BIRTH        = [0x80, 0xB0, 0x00, 0xD9, 0x02, 0x00, 0x08]
CMD_SEX          = [0x80, 0xB0, 0x00, 0xE1, 0x02, 0x00, 0x01]
CMD_CARD_ISSUER  = [0x80, 0xB0, 0x00, 0xF6, 0x02, 0x00, 0x64]
CMD_ISSUE_DATE   = [0x80, 0xB0, 0x01, 0x67, 0x02, 0x00, 0x08]
CMD_EXPIRE_DATE  = [0x80, 0xB0, 0x01, 0x6F, 0x02, 0x00, 0x08]
CMD_ADDRESS      = [0x80, 0xB0, 0x15, 0x79, 0x02, 0x00, 0x64]

CMD_GET_RESPONSE = [0x00, 0xC0, 0x00, 0x00]

# รูปถ่ายแบ่งเป็น 20 บล็อค (มาตรฐานตำแหน่งของบัตรประชาชนไทย)
CMD_PHOTO_BLOCKS = [
    [0x80, 0xB0, 0x01, 0x7B, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x02, 0x7A, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x03, 0x79, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x04, 0x78, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x05, 0x77, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x06, 0x76, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x07, 0x75, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x08, 0x74, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x09, 0x73, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0A, 0x72, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0B, 0x71, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0C, 0x70, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0D, 0x6F, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0E, 0x6E, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x0F, 0x6D, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x10, 0x6C, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x11, 0x6B, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x12, 0x6A, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x13, 0x69, 0x02, 0x00, 0xFF],
    [0x80, 0xB0, 0x14, 0x68, 0x02, 0x00, 0xFF],
]


class SmartCardError(Exception):
    pass


class CardNotPresentError(SmartCardError):
    """ยกขึ้นเมื่อเครื่องอ่านเจอ แต่ไม่มีบัตรเสียบอยู่ในเครื่อง"""
    pass


class SmartCardBusyError(SmartCardError):
    """ยกขึ้นเมื่อมีการอ่านบัตรอีก request กำลังทำงานอยู่แล้ว (กันชนกัน)"""
    pass


class ThaiIDCardReader:
    """
    อ่านข้อมูลจากบัตรประชาชนไทยผ่านเครื่องอ่านบัตร USB (CCID)
    Config (ชื่อเครื่องอ่านที่เลือกใช้) เก็บไว้ใน config.json คีย์ "smartcard" เหมือนเวอร์ชัน Windows
    """

    def __init__(self):
        self._inflight_lock = threading.Lock()
        self._inflight = None

        self._cache_lock = threading.Lock()
        self._cached_data = None
        self._cached_error = None
        self._card_present = False
        self._watcher_started = False

        self._reader = None                 # CcidReader ที่เปิดค้างไว้ (เฉพาะโปรเซส service)
        self._permission_requested = set()  # ขออนุญาต USB ไปแล้ว ไม่ต้องเด้งซ้ำทุกวินาที
        self._no_speedup = set()            # เครื่องอ่านที่เร่งความเร็ว (PPS) แล้วมีปัญหา — ไม่ลองอีก
        self._cfg_cache = (None, None)      # (mtime, smartcard config) — กันอ่านไฟล์ทุกรอบ poll

    # ---------------- Config helpers ----------------

    def get_smartcard_config(self):
        try:
            mtime = os.stat(config_store.get_config_file()).st_mtime_ns
        except OSError:
            mtime = None
        cached_mtime, cached = self._cfg_cache
        if cached is not None and mtime is not None and mtime == cached_mtime:
            return dict(cached)
        cfg = dict(config_store.read_raw().get("smartcard") or {})
        cfg.setdefault("selected_reader", None)
        cfg["read_image"] = bool(cfg.get("read_image", True))  # ✅ ค่าเริ่มต้น: อ่านรูป
        self._cfg_cache = (mtime, dict(cfg))
        return cfg

    def is_read_image_enabled(self):
        return bool(self.get_smartcard_config().get("read_image", True))

    def set_smartcard_reader(self, reader_name=None, read_image=None):
        """บันทึกเครื่องอ่านที่เลือก และ/หรือ การเปิด-ปิดอ่านรูป (ค่าที่เป็น None = คงค่าเดิม)"""
        smartcard = dict(config_store.read_raw().get("smartcard") or {})
        if reader_name is not None:
            smartcard["selected_reader"] = reader_name
        if read_image is not None:
            smartcard["read_image"] = bool(read_image)
        smartcard.setdefault("selected_reader", None)
        smartcard.setdefault("read_image", True)
        config_store.save_config({"smartcard": smartcard})
        return smartcard

    # ---------------- Reader discovery ----------------

    @staticmethod
    def list_readers():
        try:
            return [label for label, _ in list_ccid_devices()]
        except Exception as e:
            raise SmartCardError(f"ไม่สามารถอ่านรายชื่อเครื่องอ่านบัตรได้: {e}")

    def _close_reader(self):
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def _open_selected_reader(self):
        """เปิด (หรือใช้ตัวที่เปิดค้างไว้) เครื่องอ่านที่เลือกใน config — ต้องถือ _SMARTCARD_LOCK อยู่"""
        selected = self.get_smartcard_config().get("selected_reader")

        if self._reader is not None and not self._reader.closed:
            if not selected or self._reader.label == selected:
                return self._reader
        self._close_reader()

        try:
            available = list_ccid_devices()
        except Exception as e:
            raise SmartCardError(f"ไม่สามารถอ่านรายชื่อเครื่องอ่านบัตรได้: {e}")
        if not available:
            raise SmartCardError("ไม่พบเครื่องอ่านบัตร (smart card reader) ที่เชื่อมต่ออยู่")

        target = None
        if selected:
            for label, dev in available:
                if label == selected:
                    target = (label, dev)
                    break
            if target is None:
                raise SmartCardError(
                    f"ไม่พบเครื่องอ่านที่ตั้งค่าไว้ ('{selected}'). "
                    f"เครื่องที่มีอยู่: {[label for label, _ in available]}"
                )
        else:
            target = available[0]  # ยังไม่ได้ตั้งค่า ใช้เครื่องแรกที่เจอ

        label, dev = target
        if not au.has_usb_permission(dev):
            if label not in self._permission_requested:
                self._permission_requested.add(label)
                try:
                    au.request_usb_permission(dev)
                except Exception as e:
                    print("⚠️ request USB permission failed:", e)
            raise SmartCardError(
                f"ยังไม่ได้รับอนุญาตให้ใช้เครื่องอ่านบัตร '{label}' — "
                f"เปิดแอป AuthenAPI แล้วกด 'ขออนุญาต USB'"
            )

        try:
            self._reader = CcidReader(dev, label)
        except CcidPermissionError as e:
            raise SmartCardError(str(e))
        except CcidError as e:
            raise SmartCardError(f"เชื่อมต่อเครื่องอ่าน '{label}' ไม่สำเร็จ: {e}")
        return self._reader

    def list_terminals_with_status(self):
        """[{"terminalName": "...", "isPresent": true/false/None}] — ใช้สถานะจาก watcher (ไม่แตะฮาร์ดแวร์)"""
        available = self.list_readers()
        selected = self.get_smartcard_config().get("selected_reader")
        _, _, is_present = self.get_cached_result()
        return [
            {"terminalName": name, "isPresent": is_present if (not selected or name == selected) else None}
            for name in available
        ]

    # ---------------- Background card watcher ----------------

    def start_background_watcher(self):
        """
        ✅ เฝ้าดูการเสียบ/ถอดบัตรในเบื้องหลัง (poll สถานะช่องเสียบทุก 0.2 วินาที)
        เมื่อเสียบบัตร จะอ่านข้อมูลครั้งเดียวแล้วเก็บไว้ในแคช — API endpoints อ่านจากแคชเท่านั้น
        (ครอบคลุมกรณีบัตรเสียบค้างไว้ก่อนเปิดแอปด้วย เพราะรอบแรกจะเห็นว่ามีบัตรทันที)
        """
        if self._watcher_started:
            return
        self._watcher_started = True
        threading.Thread(target=self._watch_loop, name="card-watcher", daemon=True).start()

    def _poll_card_present(self):
        if not _SMARTCARD_LOCK.acquire(blocking=False):
            return self._card_present  # กำลังอ่านบัตรอยู่ — ใช้สถานะล่าสุดไปก่อน
        try:
            try:
                reader = self._open_selected_reader()
                return reader.is_card_present()
            except Exception:
                self._close_reader()  # เครื่องอ่านอาจถูกถอด — เปิดใหม่รอบหน้า
                return False
        finally:
            _SMARTCARD_LOCK.release()

    def _watch_loop(self):
        last_present = False
        while True:
            present = self._poll_card_present()
            if present and not last_present:
                time.sleep(0.4)  # ✅ ให้บัตรตั้งตัว (contact settle) ก่อนเริ่มอ่านจริง
                self._read_and_cache()
            elif present and self._needs_image_refresh():
                # ✅ เพิ่งเปิดการอ่านรูปขณะบัตรเสียบค้างอยู่ — อ่านใหม่ให้มีรูป
                self._read_and_cache()
            elif not present and last_present:
                self._clear_cache_on_removal()
            last_present = present
            time.sleep(_POLL_INTERVAL_SEC)

    def _needs_image_refresh(self):
        with self._cache_lock:
            data = self._cached_data
        return data is not None and "image" not in data and self.is_read_image_enabled()

    def _read_and_cache(self):
        """อ่านบัตรจริง (ครั้งเดียว) แล้วเก็บผลไว้ในแคช — เรียกจาก watcher เท่านั้น"""
        started = time.time()
        try:
            # ✅ ปิดการอ่านรูปไว้ = ไม่อ่านรูปจากบัตรเลย (ประหยัดเวลาส่วนที่นานที่สุด)
            data = self.read_card(read_image=self.is_read_image_enabled())
            print(f"🪪 อ่านบัตรเสร็จใน {time.time() - started:.2f} วินาที")
            with self._cache_lock:
                self._cached_data = data
                self._cached_error = None
                self._card_present = True
        except Exception as e:
            print(f"⚠️ อ่านบัตรไม่สำเร็จ ({time.time() - started:.2f} วินาที): {e}")
            with self._cache_lock:
                self._cached_data = None
                self._cached_error = e
                self._card_present = True  # เสียบอยู่ แต่อ่านไม่สำเร็จ

    def _clear_cache_on_removal(self):
        with self._cache_lock:
            self._cached_data = None
            self._cached_error = None
            self._card_present = False

    def get_cached_result(self):
        """คืน (data, error, is_present) จากแคชปัจจุบัน — ไม่แตะฮาร์ดแวร์"""
        with self._cache_lock:
            return self._cached_data, self._cached_error, self._card_present

    def _get_connection(self):
        reader = self._open_selected_reader()
        reader.speed_negotiated = False
        try:
            atr = reader.power_on()
            if reader.label not in self._no_speedup:
                try:
                    if reader.negotiate_speed(atr):
                        reader.speed_negotiated = True
                        print(f"⚡ เร่งความเร็วบัตรแล้ว (ATR {bytes(atr).hex()})")
                except CcidNoCardError:
                    raise
                except Exception as e:
                    # ✅ เร่งไม่ได้ — จำไว้ไม่ลองอีก แล้วรีเซ็ตบัตรกลับไปใช้ความเร็วปกติ
                    print(f"⚠️ เร่งความเร็วบัตรไม่สำเร็จ ใช้ความเร็วปกติแทน: {e}")
                    self._no_speedup.add(reader.label)
                    reader.power_off()
                    reader.power_on()
        except CcidNoCardError as e:
            raise CardNotPresentError(f"ไม่มีบัตรเสียบอยู่ในเครื่อง '{reader.label}': {e}")
        except CcidError as e:
            raise SmartCardError(f"เชื่อมต่อเครื่องอ่าน '{reader.label}' ไม่สำเร็จ: {e}")
        return reader

    # ---------------- Low level APDU ----------------

    @staticmethod
    def _send(connection, command):
        try:
            return connection.transmit(command)
        except CcidNoCardError as e:
            raise CardNotPresentError(str(e))

    def _read_field(self, connection, command, delay=0.0):
        """
        ส่งคำสั่งอ่านฟิลด์ แล้ว GET RESPONSE ตามจำนวนไบต์ที่ชิปแจ้งกลับ (sw1=0x61, sw2=จำนวนไบต์)
        ✅ รองรับเครื่องอ่านแบบ APDU-level ที่ทำ GET RESPONSE ให้เองด้วย (ได้ data กลับมาเลย)
        """
        data, sw1, sw2 = self._send(connection, command)
        if delay:
            time.sleep(delay)
        if sw1 == 0x61:
            data, sw1, sw2 = self._send(connection, CMD_GET_RESPONSE + [sw2])
        elif not data:
            data, sw1, sw2 = self._send(connection, CMD_GET_RESPONSE + [command[-1]])
        if sw1 == 0x6C:  # ขอความยาวผิด — ชิปบอกความยาวที่ถูกต้องมาใน sw2
            data, sw1, sw2 = self._send(connection, CMD_GET_RESPONSE + [sw2])
        if delay:
            time.sleep(delay)
        return data

    @staticmethod
    def _decode_raw(raw_bytes):
        """decode แบบเก็บ '#' ไว้ก่อน — ใช้กับฟิลด์ที่ต้อง split (ชื่อ, ที่อยู่)"""
        try:
            text = bytes(raw_bytes).decode("tis-620", errors="ignore")
        except Exception:
            text = bytes(raw_bytes).decode("latin-1", errors="ignore")
        return text

    @classmethod
    def _decode_thai(cls, raw_bytes):
        """decode แบบลบ '#' ทิ้ง — ใช้กับฟิลด์เดี่ยวๆ ที่ไม่มีการแบ่งย่อย"""
        text = cls._decode_raw(raw_bytes)
        return text.replace("#", "").strip()

    # ---------------- Public read ----------------

    def read_card(self, read_image=False):
        """
        ✅ Request coalescing: ถ้ามีการอ่านบัตรแบบเดียวกันกำลังทำงานอยู่ request ที่มาทีหลัง
        จะรอผลลัพธ์เดียวกันแทนที่จะไปแย่งอ่านซ้ำ
        """
        with self._inflight_lock:
            current = self._inflight
            if current is not None and current["read_image"] == read_image:
                event = current["event"]
                is_leader = False
            else:
                event = threading.Event()
                self._inflight = {"read_image": read_image, "event": event, "result": None, "error": None}
                is_leader = True

        if not is_leader:
            finished = event.wait(timeout=20)
            with self._inflight_lock:
                snapshot = self._inflight if self._inflight is not None and self._inflight["event"] is event else None
            if not finished:
                raise SmartCardBusyError("เครื่องอ่านบัตรกำลังถูกใช้งานอยู่ กรุณารอสักครู่แล้วลองใหม่")
            if snapshot is not None and snapshot["error"] is not None:
                raise snapshot["error"]
            if snapshot is not None:
                return snapshot["result"]
            return self._do_actual_read(read_image)

        try:
            result = self._do_actual_read(read_image)
            with self._inflight_lock:
                self._inflight["result"] = result
            return result
        except Exception as e:
            with self._inflight_lock:
                self._inflight["error"] = e
            raise
        finally:
            with self._inflight_lock:
                event.set()
                if self._inflight is not None and self._inflight["event"] is event:
                    self._inflight = None

    def _do_actual_read(self, read_image):
        acquired = _SMARTCARD_LOCK.acquire(timeout=4)
        if not acquired:
            raise SmartCardBusyError("เครื่องอ่านบัตรกำลังถูกใช้งานอยู่ กรุณารอสักครู่แล้วลองใหม่")
        try:
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                connection = None
                try:
                    connection = self._get_connection()
                    self._send(connection, CMD_SELECT_APPLET)
                    return self._read_all_fields(connection, read_image)
                except CardNotPresentError:
                    raise  # ไม่มีบัตรจริงๆ — ไม่ retry
                except Exception as e:
                    if isinstance(e, SmartCardError) and "ยังไม่ได้รับอนุญาต" in str(e):
                        raise  # ไม่มีสิทธิ์ USB — retry ไปก็ไม่หาย
                    if connection is not None and getattr(connection, "speed_negotiated", False):
                        # อ่านพังหลังเร่งความเร็ว — รอบถัดไปใช้ความเร็วปกติ
                        print(f"⚠️ อ่านบัตรพลาดหลังเร่งความเร็ว — ปิดการเร่งความเร็ว: {e}")
                        self._no_speedup.add(connection.label)
                    if attempt < max_attempts:
                        # ✅ ปัญหาฮาร์ดแวร์/การสื่อสารชั่วขณะ — ปิดแล้วเปิดเครื่องอ่านใหม่ แล้ว retry
                        self._close_reader()
                        connection = None
                        time.sleep(0.4 * attempt)
                        continue
                    raise SmartCardError(
                        f"เกิดข้อผิดพลาดขณะอ่านข้อมูลจากบัตรซ้ำหลายครั้งติดต่อกัน "
                        f"(ลองแล้ว {max_attempts} ครั้ง): {e}"
                    )
                finally:
                    if connection is not None:
                        connection.power_off()
        finally:
            _SMARTCARD_LOCK.release()

    def _read_all_fields(self, connection, read_image):
        cid = self._decode_thai(self._read_field(connection, CMD_CID))

        # รูปแบบฟิลด์ชื่อบนชิป: title#firstname#middlename#lastname
        fullname_th_raw = self._decode_raw(self._read_field(connection, CMD_FULLNAME_TH))
        th_parts = fullname_th_raw.split("#")
        title_th = th_parts[0].strip() if len(th_parts) > 0 else ""
        fname_th = th_parts[1].strip() if len(th_parts) > 1 else ""
        mname_th = th_parts[2].strip() if len(th_parts) > 2 else ""
        lname_th = th_parts[3].strip() if len(th_parts) > 3 else ""

        fullname_en_raw = self._decode_raw(self._read_field(connection, CMD_FULLNAME_EN))
        en_parts = fullname_en_raw.split("#")
        title_en = en_parts[0].strip() if len(en_parts) > 0 else ""
        fname_en = en_parts[1].strip() if len(en_parts) > 1 else ""
        mname_en = en_parts[2].strip() if len(en_parts) > 2 else ""
        lname_en = en_parts[3].strip() if len(en_parts) > 3 else ""

        birth_raw = self._decode_thai(self._read_field(connection, CMD_BIRTH))  # YYYYMMDD (พ.ศ.)
        sex_raw = self._decode_thai(self._read_field(connection, CMD_SEX))
        card_issuer = self._decode_thai(self._read_field(connection, CMD_CARD_ISSUER))
        issue_date_raw = self._decode_thai(self._read_field(connection, CMD_ISSUE_DATE))
        expire_date_raw = self._decode_thai(self._read_field(connection, CMD_EXPIRE_DATE))

        # ที่อยู่บนชิปแบ่งด้วย # — จับคู่ด้วย "คำนำหน้า" แทนตำแหน่งคงที่ (เหมือนเวอร์ชัน Windows)
        address_raw = self._decode_raw(self._read_field(connection, CMD_ADDRESS))
        addr_non_empty = [p.strip() for p in address_raw.split("#") if p.strip() != ""]
        address = " ".join(addr_non_empty)

        village_no = ""
        sub_district = ""
        district = ""
        province = ""
        remaining = []

        prefix_map = [
            (("หมู่ที่", "หมู่บ้าน"), "village_no"),
            (("ตำบล", "แขวง"), "sub_district"),
            (("อำเภอ", "เขต"), "district"),
            (("จังหวัด",), "province"),
        ]

        for part in addr_non_empty:
            matched_field = None
            matched_prefix = ""
            for prefixes, field in prefix_map:
                for pre in prefixes:
                    if part.startswith(pre):
                        matched_field = field
                        matched_prefix = pre
                        break
                if matched_field:
                    break

            if matched_field:
                value = part[len(matched_prefix):].strip()
                if matched_field == "village_no":
                    village_no = value
                elif matched_field == "sub_district":
                    sub_district = value
                elif matched_field == "district":
                    district = value
                elif matched_field == "province":
                    province = value
            else:
                remaining.append(part)

        house_no = remaining[0] if len(remaining) > 0 else ""
        lane = remaining[1] if len(remaining) > 1 else ""
        road = remaining[2] if len(remaining) > 2 else ""
        lane_road = " ".join([p for p in remaining[1:] if p != ""])

        result = {
            "pid": cid,
            "titleName": title_th,
            "fname": fname_th,
            "mname": mname_th,
            "lname": lname_th,
            "titleNameEng": title_en,
            "fnameEng": fname_en,
            "mnameEng": mname_en,
            "lnameEng": lname_en,
            "birthDate": birth_raw,
            "sex": sex_raw,
            "cardIssuer": card_issuer,
            "issueDate": issue_date_raw,
            "expireDate": expire_date_raw,
            "address": address,
            "houseNo": house_no,
            "villageNo": village_no,
            "lane": lane,
            "road": road,
            "laneRoad": lane_road,
            "subDistrict": sub_district,
            "district": district,
            "province": province,
        }

        if read_image:
            result["image"] = self._read_photo(connection)

        return result

    def _read_photo(self, connection):
        photo_bytes = bytearray()
        for cmd in CMD_PHOTO_BLOCKS:
            photo_bytes.extend(self._read_field(connection, cmd, delay=_APDU_DELAY_SEC))
        return base64.b64encode(bytes(photo_bytes)).decode("ascii")


# ==============================================================
# Route registration (endpoint เดียวกับเวอร์ชัน Windows)
# ==============================================================

def register_smartcard_routes(app):
    card_reader = ThaiIDCardReader()
    card_reader.start_background_watcher()

    @app.route("/api/smartcard/readers", methods=["GET"])
    def list_smartcard_readers(req):
        try:
            reader_list = card_reader.list_readers()
            current_cfg = card_reader.get_smartcard_config()
            return {
                "available_readers": reader_list,
                "selected_reader": current_cfg.get("selected_reader")
            }, 200
        except SmartCardError as e:
            return {"error": str(e)}, 500

    @app.route("/api/smartcard/terminals", methods=["GET"])
    def smartcard_terminals(req):
        try:
            return card_reader.list_terminals_with_status(), 200
        except SmartCardError as e:
            return {"error": str(e)}, 500

    @app.route("/api/smartcard/config", methods=["GET"])
    def get_smartcard_config(req):
        return card_reader.get_smartcard_config(), 200

    @app.route("/api/smartcard/config", methods=["POST"])
    def set_smartcard_config(req):
        data = req.json or {}
        reader_name = data.get("reader_name")
        read_image = data.get("read_image")
        if not reader_name and read_image is None:
            return {"error": "Missing 'reader_name'"}, 400
        updated = card_reader.set_smartcard_reader(reader_name or None,
                                                   None if read_image is None else bool(read_image))
        return updated, 200

    def _build_error_envelope(message, status=500, error_label="Internal Server Error"):
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}+00:00"
        return {
            "timestamp": timestamp,
            "status": status,
            "error": error_label,
            "message": message,
        }

    def _get_cached_response():
        data, error, is_present = card_reader.get_cached_result()

        if data is not None:
            return data, 200

        if error is not None:
            if isinstance(error, SmartCardBusyError):
                return _build_error_envelope(str(error), status=409, error_label="Conflict"), 409
            if isinstance(error, SmartCardError):
                return _build_error_envelope(str(error)), 500
            return _build_error_envelope(f"Unexpected error: {error}"), 500

        # ไม่มี error แต่ก็ไม่มีข้อมูล แปลว่ายังไม่มีบัตรเสียบ (ข้อความเดียวกับเวอร์ชัน Windows)
        return _build_error_envelope(
            "jnasmartcardio.Smartcardio$JnaCardNotPresentException: Card not present."
        ), 500

    @app.route("/api/smartcard/read-card-only", methods=["GET"])
    def read_card_only(req):
        read_image_flag = req.args.get("readImageFlag", "false").lower() == "true"
        data, status = _get_cached_response()

        if status != 200:
            return data, status

        result = dict(data)
        if not read_image_flag:
            result.pop("image", None)
        elif not card_reader.is_read_image_enabled() or "image" not in result:
            # ✅ ปิดการอ่านรูปไว้ (หรือรูปยังอ่านไม่เสร็จ) — ส่งรูปเป็นค่าว่างแทน
            result["image"] = ""

        return result, 200

    return card_reader
