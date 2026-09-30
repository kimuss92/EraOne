from enum import StrEnum


class EraState(StrEnum):
    OFFLINE = "offline"
    ONLINE_IDLE = "online_idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    STANDBY = "standby"
    RECONNECTING = "reconnecting"
