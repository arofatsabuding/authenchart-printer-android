# server.py
# ==============================================================
# HTTP backend (พอร์ต 9333) — endpoint เดียวกับ main.py ของเวอร์ชัน Windows
#   GET  /ping
#   POST /print                     {"imageBase64": "data:image/png;base64,..."}  (เข้าคิว ตอบทันที)
#                                   {"imageBase64": ..., "wait": true}           (รอพิมพ์เสร็จแล้วค่อยตอบ)
#   GET  /printers
#   /api/smartcard/...              (ดู smartcard_reader.py)
# ==============================================================

import base64
import io
import queue
import threading

from PIL import Image

import config_store
from http_app import App
from escpos import build_escpos_image, estimate_print_seconds
from printer_transport import send_to_printer, list_all_printers, DEFAULT_JOB_GAP_SEC
from smartcard_reader import register_smartcard_routes

PORT = 9333


def print_image(image: Image.Image):
    cfg = config_store.load_config()
    if not cfg.get("printer_enabled", True):
        raise RuntimeError("เครื่องพิมพ์ถูกปิดใช้งานอยู่ กรุณาเปิดใช้งานที่หน้าตั้งค่าก่อนพิมพ์")
    connection = cfg.get("printer_connection", "usb")
    pname = cfg.get("printer_name") or ""
    psize = cfg.get("paper_size", "80")
    feed_px = int(cfg.get("feed_px", 20))
    print(f"🖨️ Printing ESC/POS via {connection} to {pname or '(auto)'} | Paper {psize}mm | Feed {feed_px}px")
    data = build_escpos_image(image, psize, feed_px)
    try:
        gap = float(cfg.get("job_gap_sec", DEFAULT_JOB_GAP_SEC))
    except (TypeError, ValueError):
        gap = DEFAULT_JOB_GAP_SEC
    send_to_printer(connection, pname, data, gap, estimate_print_seconds(data, psize),
                    wait_done=bool(cfg.get("wait_print_done", True)))


# ✅ คิวงานพิมพ์: /print รับรูปแล้วตอบหน้าเว็บทันที แล้วค่อยพิมพ์ทีละใบตามลำดับเบื้องหลัง
# หน้าเว็บ (printImage) ตั้ง timeout ไว้ 2 วินาที แต่การส่งรูปเข้าเครื่องพิมพ์ + รอพิมพ์เสร็จใช้นานกว่านั้น
# เดิมหน้าเว็บจึงหมดเวลา -> ถือว่าพิมพ์ไม่ได้ -> ส่งรูปเดิมซ้ำไปที่ fallback (8189) แล้วส่งใบถัดไปทันที
# ระหว่างที่ใบแรกยังส่งไม่เสร็จ — แบบเดียวกับ Windows ที่ spooler รับงานแล้วตอบทันที
_jobs = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def _print_worker():
    while True:
        image = _jobs.get()
        try:
            print_image(image)
        except Exception as e:
            print("❌ Print job failed:", e)
        finally:
            _jobs.task_done()


def enqueue_print(image: Image.Image) -> int:
    """เข้าคิวพิมพ์ คืนจำนวนงานที่รออยู่ (รวมงานนี้)"""
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_print_worker, name="print-worker", daemon=True).start()
            _worker_started = True
    _jobs.put(image)
    return _jobs.qsize()


def create_app():
    app = App()  # CORS + Private Network Access จัดการใน http_app.py แล้ว

    register_smartcard_routes(app)

    @app.route("/ping", methods=["GET"])
    def ping(req):
        return {"status": "ok"}, 200

    @app.route("/print", methods=["POST"])
    def print_route(req):
        try:
            data = req.json or {}
            base64_image = data.get("imageBase64")
            if not base64_image:
                return {"error": "Missing base64 image"}, 400

            base64_image = base64_image.split(",")[-1]
            image = Image.open(io.BytesIO(base64.b64decode(base64_image)))
            image.load()  # ถอดรหัสรูปตอนนี้เลย รูปเสียจะได้แจ้ง error กลับไปทันที
            if data.get("wait"):
                # รอพิมพ์จริงแล้วค่อยตอบ (ปุ่มทดสอบพิมพ์ในแอปใช้ — อยากรู้ผลว่าพิมพ์ได้ไหม)
                _jobs.join()
                print_image(image)
                return {"message": "✅ พิมพ์สำเร็จ"}, 200
            if not config_store.load_config().get("printer_enabled", True):
                raise RuntimeError("เครื่องพิมพ์ถูกปิดใช้งานอยู่ กรุณาเปิดใช้งานที่หน้าตั้งค่าก่อนพิมพ์")
            pending = enqueue_print(image)
            return {"message": "✅ พิมพ์สำเร็จ", "queued": pending}, 200
        except Exception as e:
            print("❌ Error:", e)
            return {"error": str(e)}, 500

    @app.route("/printers", methods=["GET"])
    def get_printers(req):
        try:
            cfg = config_store.load_config()
            return {
                "default_printer": cfg.get("printer_name") or "",
                "connection": cfg.get("printer_connection", "usb"),
                "available_printers": list_all_printers(),
            }, 200
        except Exception as e:
            return {"error": str(e)}, 500

    return app


def run_server():
    create_app().run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    run_server()
