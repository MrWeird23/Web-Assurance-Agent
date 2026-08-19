from enum import StrEnum


class PublicationDecision(StrEnum):
    PUBLISH = "publish"
    DUPLICATE = "duplicate"
    STALE = "stale"
