"""EWS error codes and their mapping. Pure module: no I/O, no asyncio, no mutable globals."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping


class Code(StrEnum):
    """Only names from the EWS enumeration ResponseCodeType (checked against tests/data/response_codes.txt)."""

    NoError = "NoError"
    ErrorAccessDenied = "ErrorAccessDenied"
    ErrorCalendarEndDateIsEarlierThanStartDate = "ErrorCalendarEndDateIsEarlierThanStartDate"
    ErrorChangeKeyRequiredForWriteOperations = "ErrorChangeKeyRequiredForWriteOperations"
    ErrorExceededConnectionCount = "ErrorExceededConnectionCount"
    ErrorExceededSubscriptionCount = "ErrorExceededSubscriptionCount"
    ErrorExpiredSubscription = "ErrorExpiredSubscription"
    ErrorFolderNotFound = "ErrorFolderNotFound"
    ErrorImpersonationDenied = "ErrorImpersonationDenied"
    ErrorInternalServerError = "ErrorInternalServerError"
    ErrorInvalidChangeKey = "ErrorInvalidChangeKey"
    ErrorInvalidIdMalformed = "ErrorInvalidIdMalformed"
    ErrorInvalidOperation = "ErrorInvalidOperation"
    ErrorInvalidPropertySet = "ErrorInvalidPropertySet"
    ErrorInvalidRequest = "ErrorInvalidRequest"
    ErrorInvalidSubscription = "ErrorInvalidSubscription"
    ErrorInvalidSyncStateData = "ErrorInvalidSyncStateData"
    ErrorInvalidWatermark = "ErrorInvalidWatermark"
    ErrorIrresolvableConflict = "ErrorIrresolvableConflict"
    ErrorItemNotFound = "ErrorItemNotFound"
    ErrorItemSave = "ErrorItemSave"
    ErrorNonExistentMailbox = "ErrorNonExistentMailbox"
    ErrorQuotaExceeded = "ErrorQuotaExceeded"
    ErrorSchemaValidation = "ErrorSchemaValidation"
    ErrorServerBusy = "ErrorServerBusy"
    ErrorSubscriptionNotFound = "ErrorSubscriptionNotFound"
    ErrorTimeZone = "ErrorTimeZone"
    ErrorUnsupportedPathForSet = "ErrorUnsupportedPathForSet"


# Fixed texts per code, never with user data.
MESSAGE_TEXT: Final[Mapping[Code, str]] = MappingProxyType(
    {
        Code.NoError: "",
        Code.ErrorAccessDenied: "Access is denied.",
        Code.ErrorCalendarEndDateIsEarlierThanStartDate: "The end date is earlier than the start date.",
        Code.ErrorChangeKeyRequiredForWriteOperations: "A change key is required for this operation.",
        Code.ErrorExceededConnectionCount: "The maximum number of connections was exceeded.",
        Code.ErrorExceededSubscriptionCount: "The maximum number of subscriptions was exceeded.",
        Code.ErrorExpiredSubscription: "The subscription has expired.",
        Code.ErrorFolderNotFound: "The specified folder could not be found in the store.",
        Code.ErrorImpersonationDenied: "Impersonation is not permitted.",
        Code.ErrorInternalServerError: "The server could not complete the request.",
        Code.ErrorInvalidChangeKey: "The change key is invalid.",
        Code.ErrorInvalidIdMalformed: "The specified identifier is malformed or not valid.",
        Code.ErrorInvalidOperation: "The operation is not supported in this configuration.",
        Code.ErrorInvalidPropertySet: "A property value could not be interpreted.",
        Code.ErrorInvalidRequest: "The request is not supported.",
        Code.ErrorInvalidSubscription: "The subscription is not valid for this operation.",
        Code.ErrorInvalidSyncStateData: "The synchronization state is not valid.",
        Code.ErrorInvalidWatermark: "The watermark is not valid.",
        Code.ErrorIrresolvableConflict: "The item was changed by someone else; reload it and try again.",
        Code.ErrorItemNotFound: "The specified object was not found in the store.",
        Code.ErrorItemSave: "The item could not be saved.",
        Code.ErrorNonExistentMailbox: "The mailbox does not exist.",
        Code.ErrorQuotaExceeded: "The storage quota was exceeded.",
        Code.ErrorSchemaValidation: "The request failed schema validation.",
        Code.ErrorServerBusy: "The server is busy; try again later.",
        Code.ErrorSubscriptionNotFound: "The subscription was not found.",
        Code.ErrorTimeZone: "The time zone is not supported.",
        Code.ErrorUnsupportedPathForSet: "A property in the request cannot be changed.",
    }
)

BACKOFF_DEFAULT_MS: Final = 30000


def text_for(code: Code) -> str:
    return MESSAGE_TEXT[code]


class EwsError(Exception):
    """A domain-level EWS error. Carries only a code and a backoff, never user data."""

    def __init__(self, code: Code, *, backoff_ms: int | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.backoff_ms = backoff_ms

    def __str__(self) -> str:
        return self.code.value


# Key = CaldavKind.value (caldav.py). Kept as strings so that this module imports neither caldav nor httpx.
# "auth" is handled in main before the mapping (HTTP 401 + login.forget); the entry is only the safety net.
_MAPPING: Final[Mapping[str, tuple[Code, int | None]]] = MappingProxyType(
    {
        "auth": (Code.ErrorAccessDenied, None),
        "forbidden": (Code.ErrorAccessDenied, None),
        "not_found": (Code.ErrorItemNotFound, None),
        "conflict": (Code.ErrorIrresolvableConflict, None),
        "token_invalid": (Code.ErrorInvalidSyncStateData, None),
        "uid_conflict": (Code.ErrorItemSave, None),
        "quota": (Code.ErrorQuotaExceeded, None),
        "busy": (Code.ErrorServerBusy, BACKOFF_DEFAULT_MS),
        "network": (Code.ErrorServerBusy, BACKOFF_DEFAULT_MS),
        "unexpected": (Code.ErrorInternalServerError, None),
    }
)
KINDS: Final = frozenset(_MAPPING)


def from_caldav(e: BaseException, *, writing: bool) -> EwsError:
    """CaldavError -> EwsError. Duck typing on `e.kind` (CaldavKind, StrEnum). Unknown => ErrorInternalServerError.

    When reading, a CalDAV 409/412 never becomes a conflict; when writing the table in docs/architecture.md applies.
    """
    kind = getattr(e, "kind", None)
    key = getattr(kind, "value", kind)
    code, backoff = _MAPPING.get(key if isinstance(key, str) else "", (Code.ErrorInternalServerError, None))
    if not writing and code in (Code.ErrorIrresolvableConflict, Code.ErrorItemSave):
        code, backoff = Code.ErrorInternalServerError, None
    return EwsError(code, backoff_ms=backoff)
