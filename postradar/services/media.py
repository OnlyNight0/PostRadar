"""Local media path generation and Telethon download handling.

Grouped Telegram albums are not assembled; each message is handled individually.
"""

import re
from pathlib import Path
from typing import Any


def _safe_filename(message: Any, media_type: str) -> str:
    file_info = getattr(message, "file", None)
    original_name = getattr(file_info, "name", None)
    extension = getattr(file_info, "ext", None)
    safe_extension = (
        extension
        if isinstance(extension, str) and re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension)
        else ""
    )

    if original_name:
        basename = str(original_name).replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(basename).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ""
        if not suffix:
            suffix = safe_extension
        stem = basename[: -len(Path(basename).suffix)] if Path(basename).suffix else basename
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
        if safe_stem:
            return f"{safe_stem}{suffix}"

    if not safe_extension:
        safe_extension = {"photo": ".jpg", "video": ".mp4"}.get(media_type, ".bin")
    return f"{media_type}{safe_extension}"


def media_target_path(media_dir: str | Path, source_id: int, message: Any) -> Path | None:
    """Build a deterministic safe target path, or return None for text-only posts."""
    if getattr(message, "media", None) is None:
        return None

    media_type = "other"
    if getattr(message, "photo", None) is not None:
        media_type = "photo"
    elif getattr(message, "video", None) is not None:
        media_type = "video"
    elif getattr(message, "document", None) is not None:
        mime_type = (getattr(message.document, "mime_type", "") or "").lower()
        media_type = "video" if mime_type.startswith("video/") else "document"

    filename = _safe_filename(message, media_type)
    return Path(media_dir) / str(source_id) / f"{message.id}_{filename}"


async def download_message_media(client: Any, message: Any, target_path: Path) -> str | None:
    """Download using Telethon's API and return the saved path when successful."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_path = await client.download_media(message, file=str(target_path))
    if downloaded_path is None:
        return None
    return str(downloaded_path)


def delete_media_file(media_dir: str | Path, media_path: str | Path) -> bool:
    """Delete a local media file only when its resolved path is inside MEDIA_DIR.

    Returns False when the database path is malformed or escapes the configured
    directory. Filesystem errors are raised so the caller can log them.
    """
    if not isinstance(media_path, (str, Path)) or not str(media_path).strip():
        return False

    root = Path(media_dir).expanduser().resolve()
    candidate = Path(media_path).expanduser()
    if candidate.is_symlink():
        return False
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return False
    resolved.unlink(missing_ok=True)
    return True
