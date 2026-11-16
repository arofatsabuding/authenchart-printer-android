set -e
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
