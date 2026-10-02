"""Utilities for handling file attachments exchanged through the bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any

from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.types import (
    Animation,
    Audio,
    Document,
    Message,
    PhotoSize,
    Sticker,
    Video,
    VideoNote,
    Voice,
)

ATTACHMENT_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB


@dataclass(slots=True)
class AttachmentData:
    """Normalized representation of any Telegram attachment."""

    content_type: str
    file_id: str
    file_unique_id: Optional[str]
    file_name: Optional[str]
    file_size: Optional[int]
    mime_type: Optional[str]

    def short_description(self) -> str:
        """Return a human-friendly description of the attachment."""

        name = self.file_name or self.content_type.capitalize()
        size = f" ({format_file_size(self.file_size)})" if self.file_size else ""
        return f"{name}{size}"


def format_file_size(size: Optional[int]) -> str:
    """Convert raw byte sizes to a human-readable string."""

    if not size:
        return "0 B"

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def _build_attachment(
    *,
    content_type: str,
    file_id: str,
    file_unique_id: Optional[str],
    file_name: Optional[str],
    file_size: Optional[int],
    mime_type: Optional[str],
) -> AttachmentData:
    return AttachmentData(
        content_type=content_type,
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
    )


def extract_attachment(message: Message) -> Optional[AttachmentData]:
    """Pull the attachment (if any) from a message and normalize it."""

    if message.document:
        return _from_document(message.document)
    if message.photo:
        return _from_photo(message.photo[-1])
    if message.video:
        return _from_video(message.video)
    if message.audio:
        return _from_audio(message.audio)
    if message.animation:
        return _from_animation(message.animation)
    if message.voice:
        return _from_voice(message.voice)
    if message.video_note:
        return _from_video_note(message.video_note)
    if message.sticker:
        return _from_sticker(message.sticker)

    return None


def _from_document(document: Document) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.DOCUMENT,
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_name=document.file_name,
        file_size=document.file_size,
        mime_type=document.mime_type,
    )


def _from_photo(photo: PhotoSize) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.PHOTO,
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        file_name=None,
        file_size=photo.file_size,
        mime_type="image/jpeg",
    )


def _from_video(video: Video) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.VIDEO,
        file_id=video.file_id,
        file_unique_id=video.file_unique_id,
        file_name=video.file_name,
        file_size=video.file_size,
        mime_type=video.mime_type,
    )


def _from_audio(audio: Audio) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.AUDIO,
        file_id=audio.file_id,
        file_unique_id=audio.file_unique_id,
        file_name=audio.file_name,
        file_size=audio.file_size,
        mime_type=audio.mime_type,
    )


def _from_animation(animation: Animation) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.ANIMATION,
        file_id=animation.file_id,
        file_unique_id=animation.file_unique_id,
        file_name=animation.file_name,
        file_size=animation.file_size,
        mime_type=animation.mime_type,
    )


def _from_voice(voice: Voice) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.VOICE,
        file_id=voice.file_id,
        file_unique_id=voice.file_unique_id,
        file_name=None,
        file_size=voice.file_size,
        mime_type=voice.mime_type,
    )


def _from_video_note(video_note: VideoNote) -> AttachmentData:
    return _build_attachment(
        content_type=ContentType.VIDEO_NOTE,
        file_id=video_note.file_id,
        file_unique_id=video_note.file_unique_id,
        file_name=None,
        file_size=video_note.file_size,
        mime_type=None,
    )


def _from_sticker(sticker: Sticker) -> AttachmentData:
    # Bot API Sticker has is_animated/is_video, not mime_type. Its opaque
    # file_unique_id does not contain an extension either.
    extension, mime = ("webm", "video/webm") if sticker.is_video else (
        ("tgs", "application/x-tgsticker") if sticker.is_animated
        else ("webp", "image/webp")
    )
    return _build_attachment(
        content_type=ContentType.STICKER,
        file_id=sticker.file_id,
        file_unique_id=sticker.file_unique_id,
        file_name=f"sticker.{extension}",
        file_size=sticker.file_size,
        mime_type=mime,
    )


def attachment_filename(attachment: AttachmentData) -> str:
    """A portable filename when Telegram's media object has no name."""
    if attachment.file_name:
        name = attachment.file_name.replace("\\", "/").rpartition("/")[2]
        name = "".join(c for c in name if c.isprintable() and c != '"')
        if name:
            return name[:120]
    return {
        "photo": "photo.jpg", "voice": "voice.ogg", "audio": "audio.mp3",
        "video": "video.mp4", "video_note": "video-note.mp4",
        "animation": "animation.mp4", "sticker": "sticker.webp",
    }.get(attachment.content_type, "attachment.bin")


async def send_attachment(
    bot: Bot,
    chat_id: int,
    attachment: AttachmentData,
    *,
    caption: Optional[str] = None,
    message_thread_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    reply_markup: Any = None,
) -> Any:
    """Send an attachment back to a user/admin with the appropriate API call.

    Returns the sent Message object for verification.

    NOTE: In Forum groups, BOTH message_thread_id AND reply_to_message_id can be used together
    to ensure messages appear in the thread AND show as replies to the header message.
    This allows admins to see the full conversation when clicking "View all replies".
    """

    kwargs = {"chat_id": chat_id}
    if caption:
        kwargs["caption"] = caption
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup

    # CRITICAL: In Forum groups, we can use BOTH message_thread_id AND reply_to_message_id
    # This ensures messages appear in the thread AND show as replies to the header
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
        # Also add reply_to_message_id if provided (for Forum groups)
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
    elif reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    if attachment.content_type == ContentType.DOCUMENT:
        return await bot.send_document(document=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.PHOTO:
        return await bot.send_photo(photo=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.VIDEO:
        return await bot.send_video(video=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.AUDIO:
        return await bot.send_audio(audio=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.ANIMATION:
        return await bot.send_animation(animation=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.VOICE:
        return await bot.send_voice(voice=attachment.file_id, **kwargs)
    elif attachment.content_type == ContentType.VIDEO_NOTE:
        video_note_kwargs = {"chat_id": chat_id, "video_note": attachment.file_id}
        if message_thread_id is not None:
            video_note_kwargs["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None:
                video_note_kwargs["reply_to_message_id"] = reply_to_message_id
        elif reply_to_message_id is not None:
            video_note_kwargs["reply_to_message_id"] = reply_to_message_id
        return await bot.send_video_note(**video_note_kwargs)
    elif attachment.content_type == ContentType.STICKER:
        sticker_kwargs = {"chat_id": chat_id, "sticker": attachment.file_id}
        if message_thread_id is not None:
            sticker_kwargs["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None:
                sticker_kwargs["reply_to_message_id"] = reply_to_message_id
        elif reply_to_message_id is not None:
            sticker_kwargs["reply_to_message_id"] = reply_to_message_id
        return await bot.send_sticker(**sticker_kwargs)
    else:
        # Fallback: treat as a generic document.
        return await bot.send_document(document=attachment.file_id, **kwargs)
