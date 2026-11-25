# escpos.py
# ==============================================================
# แปลงรูปเป็นคำสั่ง ESC/POS Raster (GS v 0) — logic เดียวกับ escpos_print_image()
# ของเวอร์ชัน Windows แต่ไม่ผูกกับ win32print (คืนค่าเป็น bytes ไปส่งต่อทาง USB/BT/LAN)
# ==============================================================

from PIL import Image

# ✅ แบ่งภาพเป็นแถบ (band) ละไม่เกิน ~4KB — เครื่องพิมพ์ราคาถูกหลายรุ่นมีบัฟเฟอร์รับข้อมูลเล็ก
# (บางรุ่น 4-8KB) ถ้าส่ง GS v 0 ก้อนใหญ่ บัฟเฟอร์ล้นแล้วไบต์หาย -> เครื่องอ่านคำสั่งเพี้ยน
# เอาข้อมูลรูปที่เหลือไปพิมพ์เป็นตัวอักษร (ข้อความขยะ/ภาษาต่างดาวหลังรูป)
_BAND_MAX_BYTES = 4096


# ความเร็วพิมพ์ของเครื่องรุ่นช้า (~50 มม./วินาที ที่ 8 จุด/มม.) — ใช้ประมาณเวลาพิมพ์
SLOW_PRINT_ROWS_PER_SEC = 400


def estimate_print_seconds(data: bytes, paper_size="80") -> float:
    """เวลาที่เครื่องพิมพ์รุ่นช้าใช้พิมพ์ข้อมูลรูปนี้โดยประมาณ"""
    rows = len(data) / (paper_width_px(paper_size) // 8)
    return rows / SLOW_PRINT_ROWS_PER_SEC


def paper_width_px(paper_size):
    return 384 if str(paper_size) == "58" else 576


def prepare_image(img: Image.Image, paper_size="80") -> Image.Image:
    """ย่อ/ขยายให้เต็มความกว้างกระดาษ แล้วแปลงเป็นขาวดำ (mode '1' — bit 1 = จุดดำ)"""
    # ✅ PNG โปร่งใสจะกลายเป็นพื้นดำถ้าแปลงตรงๆ — วางบนพื้นขาวก่อน
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg

    gray = img.convert("L")
    width = paper_width_px(paper_size)
    if gray.width != width:
        height = max(1, int(gray.height * width / gray.width))
        gray = gray.resize((width, height))

    # จุดเข้ม (< 128) -> 255 (bit = 1 = พิมพ์จุดดำ), จุดสว่าง -> 0
    return gray.point(lambda x: 255 if x < 128 else 0, mode="1")


def build_escpos_image(img: Image.Image, paper_size="80", feed_px=20) -> bytes:
    bw = prepare_image(img, paper_size)
    width_bytes = bw.width // 8
    raster = bw.tobytes()  # mode '1' แพ็ก 8 จุดต่อไบต์ MSB ก่อน ตรงกับรูปแบบ GS v 0

    data = bytearray(b"\x1B\x40")  # Initialize printer

    band_rows = max(8, _BAND_MAX_BYTES // width_bytes)
    for y0 in range(0, bw.height, band_rows):
        rows = min(band_rows, bw.height - y0)
        data += b"\x1D\x76\x30\x00" + bytes([
            width_bytes % 256, width_bytes // 256, rows % 256, rows // 256
        ])
        data += raster[y0 * width_bytes:(y0 + rows) * width_bytes]

    # ✅ Feed ก่อนตัด (สูตรเดียวกับเวอร์ชัน Windows)
    lines = max(1, int(int(feed_px) / 24))
    data += b"\x1B\x64" + bytes([min(lines, 255)])

    # ✅ Full cut
    data += b"\x1D\x56\x00"
    return bytes(data)
