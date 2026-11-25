[app]

# ชื่อแอปที่แสดงบนเครื่อง
title = AuthenAPI

# package = org.authenchart.authenapi (ต้องตรงกับ PACKAGE_NAME ใน android_utils.py)
package.name = authenapi
package.domain = org.authenchart

source.dir = .
source.include_exts = py,png,ttf,txt
source.exclude_dirs = bin,.buildozer,tests,__pycache__
source.exclude_patterns = config.json

version = 2.0.3

# ✅ ไม่ใช้ Flask/requests — HTTP server ใช้ stdlib (http_app.py) เพราะ p4a ติดตั้งได้เฉพาะแพ็กเกจที่มี wheel
# ✅ ล็อก Python 3.11 — Kivy 2.3.1 ยังคอมไพล์กับ Python 3.14 (ค่าเริ่มต้นของ p4a master) ไม่ได้
requirements = python3==3.11.9,hostpython3==3.11.9,kivy==2.3.1,pyjnius,android,pillow,certifi

icon.filename = %(source.dir)s/logo.png
presplash.filename = %(source.dir)s/logo.png

orientation = portrait
fullscreen = 0

# ✅ Foreground service รัน Flask server (พอร์ต 9333) ค้างไว้แม้ปิดหน้าแอป
#    p4a จะสร้างคลาส org.authenchart.authenapi.ServicePrintserver ให้
services = Printserver:service.py:foreground:sticky

android.permissions = INTERNET, ACCESS_NETWORK_STATE, WAKE_LOCK, FOREGROUND_SERVICE, POST_NOTIFICATIONS, BLUETOOTH, BLUETOOTH_ADMIN, BLUETOOTH_CONNECT

# ✅ target 33 (Android 13) — ถ้าจะขึ้น 34+ ต้องประกาศ foregroundServiceType ให้ service เพิ่ม
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = False
android.accept_sdk_license = True

p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 0
