# service.py
# ==============================================================
# Android Foreground Service (python-for-android) — รัน Flask server ค้างไว้เบื้องหลัง
# ทำหน้าที่แทน "tray + Flask thread" ของเวอร์ชัน Windows: ปิดหน้าแอปแล้ว API ยังทำงานต่อ
# ประกาศไว้ใน buildozer.spec: services = Printserver:service.py:foreground:sticky
# ==============================================================

import traceback


def _keep_cpu_awake():
    """ถือ partial wake lock ไว้ กัน CPU หลับตอนจอดับ (ไม่งั้นเว็บยิง API มาแล้วไม่ตอบ)"""
    try:
        from jnius import autoclass, cast
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        service = autoclass("org.kivy.android.PythonService").mService
        try:
            service.setAutoRestartService(True)  # ถ้าระบบฆ่า service ให้เปิดใหม่อัตโนมัติ
        except Exception:
            pass
        pm = cast("android.os.PowerManager", service.getSystemService(Context.POWER_SERVICE))
        lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AuthenAPI::PrintServer")
        lock.setReferenceCounted(False)
        lock.acquire()
        return lock
    except Exception as e:
        print("⚠️ wake lock failed:", e)
        return None


if __name__ == "__main__":
    _wake_lock = _keep_cpu_awake()
    try:
        from server import run_server
        print("🚀 AuthenAPI service starting on port 9333")
        run_server()
    except Exception:
        traceback.print_exc()
        raise
