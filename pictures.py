import base64
import hashlib
import io
import os
import shutil
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError


EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tiff"}
IMAGE_MIME = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


def archive_picture(source, directory):
    try:
        with Image.open(source) as image:
            extension = EXTENSIONS.get(image.format)
            image.verify()
        if not extension:
            raise ValueError("无法识别此图片格式。")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("微信暂未保存可读取的图片，或图片文件已损坏。") from error

    with source.open("rb") as file:
        name = hashlib.file_digest(file, "sha256").hexdigest() + "." + extension
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
            staged = directory / os.path.basename(temp.name)
            try:
                with source.open("rb") as original:
                    shutil.copyfileobj(original, temp)
                temp.close()
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
    return name


def image_data_url(path):
    mime = IMAGE_MIME.get(path.suffix.lower())
    if mime and path.stat().st_size <= 8 * 1024 * 1024:
        data = path.read_bytes()
    else:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((2400, 2400))
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            buffer = io.BytesIO()
            background.save(buffer, "JPEG", quality=91)
        mime = "image/jpeg"
        data = buffer.getvalue()
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
