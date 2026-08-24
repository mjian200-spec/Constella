from __future__ import annotations

import base64
import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


class ImageAdapterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class PreparedImage:
    source_path: str
    mime_type: str
    data_url: str


class ImageAdapter:
    """Prepare one image for a multimodal OpenAI-compatible request.

    There is intentionally no image-size policy in the first implementation.
    """

    def prepare(self, path_value: str | None) -> PreparedImage:
        if not path_value:
            raise ImageAdapterError("image_path_missing", "Image asset has no resolved path")
        path = Path(path_value)
        if not path.is_file():
            raise ImageAdapterError("image_file_missing", f"Image file does not exist: {path}")
        try:
            raw = path.read_bytes()
            with Image.open(io.BytesIO(raw)) as image:
                image = ImageOps.exif_transpose(image)
                image.load()
                detected = Image.MIME.get(image.format or "", "")
                if detected in {"image/jpeg", "image/png", "image/webp"}:
                    output = io.BytesIO()
                    fmt = "JPEG" if detected == "image/jpeg" else image.format
                    if fmt == "JPEG" and image.mode not in {"RGB", "L"}:
                        image = image.convert("RGB")
                    image.save(output, format=fmt)
                    raw, mime_type = output.getvalue(), detected
                else:
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="PNG")
                    raw, mime_type = output.getvalue(), "image/png"
        except ImageAdapterError:
            raise
        except Exception as error:
            raise ImageAdapterError("image_decode_failed", f"Cannot decode image {path}: {error}") from error
        encoded = base64.b64encode(raw).decode("ascii")
        return PreparedImage(str(path), mime_type, f"data:{mime_type};base64,{encoded}")
