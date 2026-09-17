"""Pure publication identity precedence: provider ID, then canonical URL."""

from dataclasses import dataclass
from enum import StrEnum

from .urls import InvalidUrl, canonicalize_url


class ExternalKeyKind(StrEnum):
    EXTERNAL_ID = "EXTERNAL_ID"
    CANONICAL_URL = "CANONICAL_URL"


@dataclass(frozen=True)
class ExternalKey:
    kind: ExternalKeyKind
    value: str


@dataclass(frozen=True)
class MissingIdentity:
    """Neither a nonblank provider ID nor a usable URL was available."""


def external_key(external_id: str | None, url: str | None) -> ExternalKey | MissingIdentity:
    """Derive explicit identity without using title, date, body, or content hash."""

    if external_id and external_id.strip():
        return ExternalKey(ExternalKeyKind.EXTERNAL_ID, external_id)
    try:
        return ExternalKey(ExternalKeyKind.CANONICAL_URL, canonicalize_url(url))
    except InvalidUrl:
        return MissingIdentity()
