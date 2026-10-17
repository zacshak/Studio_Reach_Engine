"""Browser-only derivatives. Original images remain the AI source of truth."""
import hashlib
import io

from PIL import Image, ImageOps


def _encode(image, max_width, quality, label):
    width = min(image.width, max_width)
    height = max(1, round(image.height * width / image.width))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    resized.save(output, format="WEBP", quality=quality)
    data = output.getvalue()
    name = f"{label}-{hashlib.sha256(data).hexdigest()[:32]}.webp"
    return name, data, width, height


def upload_previews(client, bucket, prefix, images):
    """Upload immutable derivatives and return metadata keyed by original name."""
    previews = {}
    for name, data in images:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGBA" if "A" in image.getbands()
                                  or "transparency" in image.info else "RGB")
            main = _encode(image, 860, 78, "preview")
            thumb = _encode(image, 160, 70, "thumb")
        for filename, body, _, _ in (main, thumb):
            client.put_object(
                Bucket=bucket, Key=f"{prefix}/{filename}", Body=body,
                ContentType="image/webp",
                CacheControl="public, max-age=31536000, immutable")
        previews[name] = dict(src=main[0], thumb=thumb[0], width=main[2],
                              height=main[3], thumb_width=thumb[2], thumb_height=thumb[3])
    return previews
