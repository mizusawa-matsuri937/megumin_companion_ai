"""Parent-side MediaWorker contracts.

Keep the native worker implementation out of this package initializer: importing
``app.media`` occurs in the backend process during normal application startup,
whereas importing ``sounddevice`` is deliberately confined to
``app.media.worker`` inside the private helper process.
"""

from app.media.client import MediaWorkerAudioPlayer, create_media_worker_audio_player
from app.media.types import AudioOutputDevice, MouthEnvelopeSample, OutputDeviceList
from app.media.voice import (
    MediaWorkerVoiceInput,
    VoiceCaptureError,
    VoiceCaptureState,
    VoiceTranscription,
    create_media_worker_voice_input,
)

__all__ = [
    "AudioOutputDevice",
    "MediaWorkerAudioPlayer",
    "MediaWorkerVoiceInput",
    "MouthEnvelopeSample",
    "OutputDeviceList",
    "VoiceCaptureError",
    "VoiceCaptureState",
    "VoiceTranscription",
    "create_media_worker_audio_player",
    "create_media_worker_voice_input",
]
