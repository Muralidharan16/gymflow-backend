import qrcode
import io
from PIL import Image

def generate_qr_png(data: str) -> bytes:
    """Generate QR code PNG bytes for a member's qr_token."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
