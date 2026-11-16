# AuthenAPI — เวอร์ชัน Android

พอร์ตจาก `py-print - windows` มาเป็นแอป Android (Python + Kivy + python-for-android)
**API ยังเหมือนเดิมทุกอย่าง** — เว็บที่เคยเรียก `http://127.0.0.1:9333` บน Windows เปิดในเบราว์เซอร์ (Chrome) บนแท็บเล็ต/มือถือเครื่องเดียวกันแล้วใช้ได้ทันที

| Endpoint | หน้าที่ |
|---|---|
| `GET /ping` | เช็คว่าเซิร์ฟเวอร์ทำงาน |
| `POST /print` `{"imageBase64": "..."}` | พิมพ์รูปลงเครื่องพิมพ์ใบเสร็จ ESC/POS + ตัดกระดาษ |
| `GET /printers` | รายชื่อเครื่องพิมพ์ |
| `GET /api/smartcard/readers` · `/terminals` · `/config` · `POST /config` | เครื่องอ่านบัตร |
| `GET /api/smartcard/read-card-only?readImageFlag=true` | ข้อมูลบัตรประชาชน (+รูป) |

## เปรียบเทียบกับเวอร์ชัน Windows

| Windows | Android |
|---|---|
| `win32print` (ส่ง RAW ผ่าน driver) | ส่ง ESC/POS ตรงทาง **USB OTG**, **Bluetooth (SPP)** หรือ **LAN/Wi-Fi (port 9100)** — `printer_transport.py` |
| `pyscard` (PC/SC) + `CardMonitor` | ไดรเวอร์ **USB CCID** เขียนเอง (`usb_ccid.py`) + poll การเสียบ/ถอดบัตรทุก 0.2 วินาที + ต่อรองความเร็วบัตร (PPS) เหมือนไดรเวอร์ Windows |
| System tray + tkinter | หน้าตั้งค่า Kivy (`main.py`) |
| Flask thread ในโปรแกรม | **Foreground Service** (`service.py`) — ปิดหน้าแอปแล้ว API ยังทำงาน (มีแจ้งเตือนค้างไว้) |
| Flask + flask-cors | HTTP server จาก stdlib (`http_app.py`) — p4a ติดตั้ง Flask ไม่ได้ (markupsafe ไม่มี wheel Android) |
| ติดตั้งลง Desktop + shortcut + Startup | ติดตั้ง APK ตามปกติ |
| อัปเดตด้วย .bat แทนที่ .exe | เช็ค GitHub Releases แล้วเปิดลิงก์ดาวน์โหลด `AuthenAPI.apk` ให้ผู้ใช้กดติดตั้งทับ |
| `config.json` ข้างไฟล์ .exe | `config.json` ในพื้นที่ส่วนตัวของแอป (ไม่หายตอนอัปเดต) |

config เพิ่มคีย์ `printer_connection` (`usb` / `bluetooth` / `network`) ส่วน `printer_name` เก็บเป็น
`ชื่อ [VID:PID]` (USB), `ชื่อ [MAC]` (Bluetooth) หรือ `ip:port` (LAN)

## โครงสร้างไฟล์

```
main.py               หน้าตั้งค่า (แทน tray_runner.py)
service.py            foreground service ที่รันเซิร์ฟเวอร์
server.py             /ping /print /printers (แทน main.py เดิม)
http_app.py           HTTP server + CORS (แทน Flask)
smartcard_reader.py   อ่านบัตรประชาชน (logic/รูปแบบ JSON เดิม)
usb_ccid.py           ไดรเวอร์เครื่องอ่านบัตร USB CCID
escpos.py             แปลงรูป -> คำสั่ง ESC/POS (GS v 0)
printer_transport.py  ส่งข้อมูลไปเครื่องพิมพ์ USB / Bluetooth / LAN
android_utils.py      ตัวช่วยเรียก Android API (pyjnius)
config_store.py       อ่าน/เขียน config.json
fonts/                ฟอนต์ Sarabun (OFL) — Kivy ไม่มีฟอนต์ไทยในตัว
tests/test_offline.py เทสต์ที่รันบน PC ได้ (ไม่ต้องมีมือถือ)
```

## Build APK

buildozer รันบน Windows ตรงๆ ไม่ได้

**ทาง 0 — Docker Desktop (ที่ใช้ build จริงบนเครื่องนี้)**
```bash
docker run --rm -v "C:/Users/User/Desktop/py-print/py-print - android:/src"   -v "C:/Users/User/Desktop/py-print/android-keystore:/root/.android"   -v authenapi-work:/work -v authenapi-home:/root/.buildozer   --entrypoint bash kivy/buildozer:latest /src/.build_docker.sh
```
ได้ไฟล์ใน `bin/` (รอบแรก ~30–40 นาที รอบถัดไปเร็วขึ้นเพราะ cache อยู่ใน Docker volume)
สคริปต์ `.build_docker.sh` มี workaround บั๊ก venv ของ python-for-android ไว้แล้ว

> ⚠️ **กุญแจเซ็นแอป** `android-keystore/debug.keystore` (อยู่นอก repo — ห้าม commit) ต้องใช้ดอกเดิมทุกครั้งที่ build
> ไม่งั้นเครื่องลูกค้าจะติดตั้งทับ/อัปเดตไม่ได้ ("ไม่ได้ติดตั้งแอป") ถ้ากุญแจหาย ลูกค้าทุกเครื่องต้องถอนแอปแล้วติดตั้งใหม่
> **สำรองไฟล์นี้ไว้หลายที่** (alias `androiddebugkey`, รหัส `android`)

**ทาง A — GitHub Actions (ง่ายสุด)**
ใช้ `.github/workflows/build-android.yml`
→ กด Run workflow → ดาวน์โหลด `AuthenAPI.apk` จากหน้า Artifacts (รอบแรกใช้เวลาประมาณ 20–40 นาที)

**ทาง B — WSL2 (Ubuntu) บนเครื่องนี้**
```bash
wsl --install -d Ubuntu          # ใน PowerShell (ครั้งแรกครั้งเดียว)
# ใน Ubuntu:
sudo apt update && sudo apt install -y python3-pip python3-venv git zip unzip openjdk-17-jdk \
  autoconf automake libtool pkg-config zlib1g-dev libncurses5-dev libtinfo5 cmake libffi-dev libssl-dev
python3 -m venv ~/bz && source ~/bz/bin/activate
pip install buildozer==1.5.0 cython==0.29.36
cp -r "/mnt/c/Users/User/Desktop/py-print/py-print - android" ~/authenapi && cd ~/authenapi
buildozer -v android debug        # ได้ไฟล์ที่ bin/*.apk
```

> ⚠️ **ระบบอัปเดต**: Android ติดตั้งทับได้เฉพาะ APK ที่เซ็นด้วยคีย์เดียวกัน — APK แบบ `debug`
> ที่ build จากคนละเครื่อง/คนละรอบ CI จะเซ็นด้วยคีย์ต่างกัน ถ้าจะแจก `AuthenAPI.apk` ผ่าน GitHub Releases
> ให้สร้าง keystore ของตัวเองแล้ว build แบบ `buildozer android release` (ตั้งค่า `P4A_RELEASE_KEYSTORE` ฯลฯ)
> และอัปโหลดไฟล์ชื่อ **`AuthenAPI.apk`** แนบไปกับ release (คนละไฟล์กับ `AuthenAPI.exe`)

## ใช้งานบนเครื่อง

1. ติดตั้ง APK แล้วเปิดแอป **AuthenAPI** — เซิร์ฟเวอร์จะเริ่มอัตโนมัติ (ขึ้น "เซิร์ฟเวอร์: ทำงานอยู่")
2. **เครื่องพิมพ์**
   - USB: เสียบผ่านสาย OTG → รีเฟรช → เลือก → บันทึก → กด "อนุญาต" ในหน้าต่างของระบบ
   - Bluetooth: pair เครื่องพิมพ์ในตั้งค่า Bluetooth ของ Android ก่อน แล้วค่อยเลือกในแอป
   - LAN/Wi-Fi: กรอก `IP:9100`
   - กด "ทดสอบพิมพ์"
3. **เครื่องอ่านบัตร**: เสียบผ่าน OTG → รีเฟรช → เลือก → บันทึก → อนุญาต USB → เสียบบัตร → "ทดสอบอ่านบัตร"
4. ปิดหน้าแอปได้ (อย่ากด "บังคับหยุด") เว็บเรียก `http://127.0.0.1:9333` ได้ตามเดิม
5. แนะนำ: ตั้งค่า Android > แอป > AuthenAPI > แบตเตอรี่ → **ไม่จำกัด** กันระบบปิด service ตอนจอดับ

## ข้อจำกัดที่ควรรู้

- **ยังไม่ได้ทดสอบบนเครื่อง Android จริง** — เทสต์ใน `tests/` จำลองเครื่องอ่าน/บัตร และเครื่องพิมพ์ LAN ได้ผ่านหมด แต่ USB/Bluetooth จริงต้องลองกับฮาร์ดแวร์
- เครื่องอ่านบัตรต้องเป็น **USB CCID มาตรฐาน** (เกือบทุกรุ่นที่ใช้ในไทย) และรองรับบัตรแบบ T=0 (บัตรประชาชนไทย)
- เครื่องพิมพ์ USB ต้องเป็นแบบที่รับ ESC/POS ตรงๆ ทาง bulk endpoint (เครื่องพิมพ์ใบเสร็จทั่วไปได้)
- แท็บเล็ตต้องรองรับ USB Host/OTG และถ้าเสียบทั้งเครื่องพิมพ์และเครื่องอ่านบัตรพร้อมกันต้องใช้ OTG hub
- หลังรีสตาร์ทเครื่อง ต้องเปิดแอปหนึ่งครั้งเพื่อเริ่มเซิร์ฟเวอร์ (ยังไม่มี auto-start on boot)
- target Android API 33 — ถ้าจะขึ้น Play Store (ต้อง 34+) ต้องเพิ่ม `foregroundServiceType` ให้ service

## ทดสอบบน PC

```bash
python tests/test_offline.py     # ต้องมีแค่ pillow
python main.py                   # ต้องมี kivy — เปิดหน้า UI + เซิร์ฟเวอร์ (พิมพ์ได้เฉพาะแบบ LAN)
```
