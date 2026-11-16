set -e
# ✅ ต้องเซ็นด้วยกุญแจเดิมทุกครั้ง ไม่งั้น Android จะติดตั้งทับ (อัปเดต) ไม่ได้
# mount โฟลเดอร์กุญแจเข้ามาที่ /root/.android (ดู README หัวข้อ Build APK)
if [ ! -f /root/.android/debug.keystore ]; then
  echo "ERROR: ไม่พบ /root/.android/debug.keystore — ต้อง mount โฟลเดอร์ android-keystore เข้ามา" >&2
  echo '  เช่น -v "C:/Users/User/Desktop/py-print/android-keystore:/root/.android"' >&2
  exit 1
fi
rm -rf /work/app && mkdir -p /work/app
cd /src && tar --exclude=./bin --exclude=./.buildozer --exclude=./tests --exclude=./__pycache__ -cf - . | tar -xf - -C /work/app
cd /work/app
ln -sfn /work/dot-buildozer .buildozer
mkdir -p /work/dot-buildozer
# workaround p4a: venv ถูกสร้างทับของเดิม ทำให้ pip ปนเวอร์ชันพัง -> ใช้ --clear
P4A=/work/dot-buildozer/android/platform/python-for-android/pythonforandroid/build.py
[ -f "$P4A" ] && sed -i "s/shprint(host_python, '-m', 'venv', 'venv')/shprint(host_python, '-m', 'venv', '--clear', 'venv')/" "$P4A"
yes | buildozer -v android debug
mkdir -p /src/bin && cp bin/*.apk /src/bin/ && ls -la /src/bin
