# escpos.py
# ==============================================================
# แปลงรูปเป็นคำสั่ง ESC/POS Raster (GS v 0) — logic เดียวกับ escpos_print_image()
# ของเวอร์ชัน Windows แต่ไม่ผูกกับ win32print (คืนค่าเป็น bytes ไปส่งต่อทาง USB/BT/LAN)
# ==============================================================

from PIL import Image

_BAND_ROWS = 256  # แบ่งภาพเป็นแถบละ 256 แถว กันบัฟเฟอร์เครื่องพิมพ์ล้น (โดยเฉพาะ Bluetooth)


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

    for y0 in range(0, bw.height, _BAND_ROWS):
        rows = min(_BAND_ROWS, bw.height - y0)
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
