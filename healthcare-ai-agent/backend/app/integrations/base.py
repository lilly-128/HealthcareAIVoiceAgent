"""Connector interface.

Swap MockEHRConnector for a real one (Epic/Cerner/…) without touching the AI,
the capability layer, or the appointment service.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


class IntegrationError(Exception):
    def __init__(self, error_class: str, message: str = ""):
        # TIMEOUT | UNKNOWN | OUTAGE | AUTH | RATE_LIMITED | VALIDATION | CONFLICT | MAPPING
        self.error_class = error_class
        super().__init__(message or error_class)


@dataclass
class ExternalAppointment:
    external_id: str
    status: str
    raw: dict


class EHRConnector(ABC):
    name: str = "base"

    @abstractmethod
    def upsert_patient(self, patient: dict) -> str: ...

    @abstractmethod
    def find_provider(self, provider_hint: dict) -> str: ...

    @abstractmethod
    def create_appointment(self, payload: dict, idempotency_key: str) -> ExternalAppointment: ...

    @abstractmethod
    def get_appointment(self, external_id: str) -> ExternalAppointment | None: ...

    @abstractmethod
    def find_by_idempotency_key(self, idempotency_key: str) -> ExternalAppointment | None: ...

    @abstractmethod
    def cancel_appointment(self, external_id: str) -> ExternalAppointment: ...

    @abstractmethod
    def reschedule_appointment(self, external_id: str, start_at: str) -> ExternalAppointment: ...
