import httpx

from ..config import settings
from .base import EHRConnector, ExternalAppointment, IntegrationError


class MockEHRConnector(EHRConnector):
    name = "MOCK_EHR"

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or settings.EHR_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.EHR_TIMEOUT_SECONDS

    # -------------------------------------------------------------- helpers
    def _call(self, method: str, path: str, **kw) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            # The request may or may not have been applied → unknown outcome.
            raise IntegrationError("TIMEOUT", str(e))
        except httpx.HTTPError as e:
            raise IntegrationError("NETWORK", str(e))

        if r.status_code == 401:
            raise IntegrationError("AUTH", r.text)
        if r.status_code == 409:
            raise IntegrationError("CONFLICT", r.text)
        if r.status_code == 422:
            raise IntegrationError("VALIDATION", r.text)
        if r.status_code == 429:
            raise IntegrationError("RATE_LIMITED", r.text)
        if r.status_code >= 500:
            raise IntegrationError("OUTAGE", r.text)
        if r.status_code == 404:
            return {}
        return r.json()

    @staticmethod
    def _to_appointment(data: dict) -> ExternalAppointment | None:
        if not data or "external_appointment_id" not in data:
            return None
        return ExternalAppointment(
            external_id=data["external_appointment_id"],
            status=data.get("status", "UNKNOWN"),
            raw=data,
        )

    # ------------------------------------------------------------ operations
    def upsert_patient(self, patient: dict) -> str:
        return self._call("POST", "/ehr/patients", json=patient)["external_patient_id"]

    def find_provider(self, provider_hint: dict) -> str:
        return self._call("POST", "/ehr/providers/search", json=provider_hint)["external_provider_id"]

    def create_appointment(self, payload: dict, idempotency_key: str) -> ExternalAppointment:
        data = self._call(
            "POST", "/ehr/appointments", json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        appt = self._to_appointment(data)
        if appt is None:
            raise IntegrationError("MAPPING", "EHR returned no appointment id")
        return appt

    def get_appointment(self, external_id: str) -> ExternalAppointment | None:
        return self._to_appointment(self._call("GET", f"/ehr/appointments/{external_id}"))

    def find_by_idempotency_key(self, idempotency_key: str) -> ExternalAppointment | None:
        return self._to_appointment(
            self._call("GET", "/ehr/appointments", params={"idempotency_key": idempotency_key})
        )

    def cancel_appointment(self, external_id: str) -> ExternalAppointment:
        return self._to_appointment(self._call("DELETE", f"/ehr/appointments/{external_id}"))

    def reschedule_appointment(self, external_id: str, start_at: str) -> ExternalAppointment:
        return self._to_appointment(
            self._call("PUT", f"/ehr/appointments/{external_id}", json={"start_at": start_at})
        )


def get_connector(hospital=None) -> EHRConnector:
    """Connector registry. Real hospitals would map to their own connector."""
    return MockEHRConnector()
