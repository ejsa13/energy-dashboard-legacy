"""Minimal, credential-safe Octopus REST client for historical import."""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, TypeAlias
from urllib import error, parse, request

API_BASE_URL = "https://api.octopus.energy/v1"


class OctopusApiError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_api_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise OctopusApiError("Octopus response contained an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OctopusApiError("Octopus response contained an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise OctopusApiError("Octopus response contained a naive timestamp")
    return parsed.astimezone(timezone.utc)


def extract_product_code(tariff_code: str) -> str:
    parts = tariff_code.split("-")
    if len(parts) < 4 or not tariff_code.startswith(("E-", "G-")):
        raise OctopusApiError("Octopus response contained an invalid tariff code")
    return "-".join(parts[2:-1])


@dataclass(frozen=True, slots=True)
class OctopusContext:
    mpan: str
    meter_serials: tuple[str, ...]
    earliest_available_at: datetime
    meter_point: dict[str, Any]
    tariff_agreements: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class OctopusGasContext:
    mprn: str
    meter_serials: tuple[str, ...]
    earliest_available_at: datetime
    tariff_agreements: tuple[dict[str, Any], ...]


Transport: TypeAlias = Callable[[request.Request, float], bytes]


class OctopusClient:
    def __init__(self, api_key: str, *, transport: Transport | None = None, timeout_seconds: float = 20):
        if not api_key:
            raise OctopusApiError("Octopus API key is missing")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._urlopen

    @staticmethod
    def _urlopen(req: request.Request, timeout: float) -> bytes:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()

    def _get_json(
        self,
        path_or_url: str,
        *,
        params: dict[str, str] | None = None,
        authenticated: bool,
    ) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{API_BASE_URL}/{path_or_url.lstrip('/')}"
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{parse.urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": "energy-dashboard/0.1"}
        if authenticated:
            token = base64.b64encode(f"{self.api_key}:".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        try:
            payload = self.transport(request.Request(url, headers=headers), self.timeout_seconds)
        except error.HTTPError as exc:
            # Do not include provider response bodies, URLs, account ids, or credentials in errors.
            raise OctopusApiError(
                f"Octopus API returned HTTP {exc.code}",
                transient=exc.code == 429 or 500 <= exc.code < 600,
            ) from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            raise OctopusApiError("Octopus API is temporarily unavailable", transient=True) from exc
        try:
            decoded = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OctopusApiError("Octopus API returned malformed JSON") from exc
        if not isinstance(decoded, dict):
            raise OctopusApiError("Octopus API returned an invalid payload")
        return decoded

    def _paginated(self, path: str, *, params: dict[str, str], authenticated: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = path
        next_params: dict[str, str] | None = params
        while next_url:
            page = self._get_json(next_url, params=next_params, authenticated=authenticated)
            results = page.get("results")
            if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
                raise OctopusApiError("Octopus API returned malformed paginated results")
            rows.extend(results)
            next_value = page.get("next")
            if next_value is not None and not isinstance(next_value, str):
                raise OctopusApiError("Octopus API returned an invalid pagination link")
            next_url, next_params = next_value, None
        return rows

    def products(self) -> list[dict[str, Any]]:
        """Public Octopus product catalogue; no account data is sent."""
        return self._paginated("products/", params={}, authenticated=False)

    def product(self, product_code: str) -> dict[str, Any]:
        return self._get_json(f"products/{product_code}/", authenticated=False)

    def resolve_import_context(self, *, account_number: str) -> OctopusContext:
        account = self._get_json(f"accounts/{account_number}/", authenticated=True)
        candidates: list[dict[str, Any]] = []
        for property_data in account.get("properties", []):
            if not isinstance(property_data, dict):
                continue
            for candidate in property_data.get("electricity_meter_points", []):
                if not isinstance(candidate, dict) or candidate.get("is_export") is True:
                    continue
                agreements = candidate.get("agreements", [])
                if not isinstance(agreements, list):
                    continue
                tariff_codes = [str(item.get("tariff_code", "")).upper() for item in agreements if isinstance(item, dict)]
                if tariff_codes and all("OUTGOING" in code for code in tariff_codes):
                    continue
                if candidate.get("mpan") and candidate.get("meters"):
                    candidates.append(candidate)
        if len(candidates) != 1:
            raise OctopusApiError("Could not uniquely discover an import electricity meter point from the Octopus account")
        meter_point = candidates[0]
        settlement_serials = {
            str(meter.get("serial_number"))
            for meter in meter_point.get("meters", [])
            if isinstance(meter, dict)
            and meter.get("serial_number")
            and any(register.get("is_settlement_register") for register in meter.get("registers", []) if isinstance(register, dict))
        }
        all_serials = {
            str(meter.get("serial_number"))
            for meter in meter_point.get("meters", [])
            if isinstance(meter, dict) and meter.get("serial_number")
        }
        serials = settlement_serials or all_serials
        if not serials:
            raise OctopusApiError("Could not discover a settlement meter serial from the Octopus account")
        agreements = meter_point.get("agreements", [])
        if not isinstance(agreements, list) or not all(isinstance(item, dict) for item in agreements):
            raise OctopusApiError("Octopus account response contained invalid tariff agreements")
        agreement_starts = []
        for agreement in agreements:
            try:
                agreement_starts.append(parse_api_datetime(agreement.get("valid_from")))
            except OctopusApiError:
                continue
        if not agreement_starts:
            raise OctopusApiError("Could not determine the earliest import history date from the Octopus account")
        return OctopusContext(
            mpan=str(meter_point["mpan"]),
            meter_serials=tuple(sorted(serials)),
            earliest_available_at=min(agreement_starts),
            meter_point=meter_point,
            tariff_agreements=tuple(agreements),
        )

    def resolve_export_context(self, *, account_number: str) -> OctopusContext:
        """Discover the account's one outgoing electricity meter point."""
        account = self._get_json(f"accounts/{account_number}/", authenticated=True)
        candidates: list[dict[str, Any]] = []
        for property_data in account.get("properties", []):
            if not isinstance(property_data, dict):
                continue
            for point in property_data.get("electricity_meter_points", []):
                if not isinstance(point, dict) or not point.get("mpan") or not point.get("meters"):
                    continue
                agreements = point.get("agreements", [])
                if not isinstance(agreements, list):
                    continue
                codes = [str(item.get("tariff_code", "")).upper() for item in agreements if isinstance(item, dict)]
                if point.get("is_export") is True or any("OUTGOING" in code for code in codes):
                    candidates.append(point)
        if len(candidates) != 1:
            raise OctopusApiError("Could not uniquely discover an export electricity meter point from the Octopus account")
        point = candidates[0]
        serials = tuple(sorted({str(meter.get("serial_number")) for meter in point["meters"] if isinstance(meter, dict) and meter.get("serial_number")}))
        agreements = point.get("agreements", [])
        starts = []
        for agreement in agreements:
            try: starts.append(parse_api_datetime(agreement.get("valid_from")))
            except OctopusApiError: pass
        if not serials or not starts:
            raise OctopusApiError("Octopus export meter details are incomplete")
        return OctopusContext(str(point["mpan"]), serials, min(starts), point, tuple(agreements))

    def resolve_gas_context(self, *, account_number: str) -> OctopusGasContext:
        account = self._get_json(f"accounts/{account_number}/", authenticated=True)
        candidates = [point for property_data in account.get("properties", []) if isinstance(property_data, dict) for point in property_data.get("gas_meter_points", []) if isinstance(point, dict) and point.get("mprn") and point.get("meters")]
        if len(candidates) != 1:
            raise OctopusApiError("Could not uniquely discover a gas meter point from the Octopus account")
        point = candidates[0]
        serials = tuple(sorted({str(meter["serial_number"]) for meter in point["meters"] if isinstance(meter, dict) and meter.get("serial_number")}))
        agreements = point.get("agreements")
        if not serials or not isinstance(agreements, list) or not all(isinstance(item, dict) for item in agreements):
            raise OctopusApiError("Octopus account response contained invalid gas meter details")
        starts = []
        for agreement in agreements:
            try:
                starts.append(parse_api_datetime(agreement.get("valid_from")))
            except OctopusApiError:
                continue
        if not starts:
            raise OctopusApiError("Could not determine the earliest gas history date from the Octopus account")
        return OctopusGasContext(str(point["mprn"]), serials, min(starts), tuple(agreements))

    def consumption(self, *, mpan: str, meter_serial: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._paginated(
            f"electricity-meter-points/{mpan}/meters/{meter_serial}/consumption/",
            params={"page_size": "250", "period_from": format_utc(start), "period_to": format_utc(end), "order_by": "period"},
            authenticated=True,
        )

    def gas_consumption(self, *, mprn: str, meter_serial: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._paginated(f"gas-meter-points/{mprn}/meters/{meter_serial}/consumption/", params={"page_size": "250", "period_from": format_utc(start), "period_to": format_utc(end), "order_by": "period"}, authenticated=True)

    def gas_unit_rates(self, *, tariff_code: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._paginated(f"products/{extract_product_code(tariff_code)}/gas-tariffs/{tariff_code}/standard-unit-rates/", params={"page_size":"250", "period_from":format_utc(start), "period_to":format_utc(end)}, authenticated=False)

    def gas_standing_charges(self, *, tariff_code: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._paginated(f"products/{extract_product_code(tariff_code)}/gas-tariffs/{tariff_code}/standing-charges/", params={"page_size":"250", "period_from":format_utc(start), "period_to":format_utc(end)}, authenticated=False)

    def unit_rates(self, *, tariff_code: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        product_code = extract_product_code(tariff_code)
        return self._paginated(
            f"products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/",
            params={"page_size": "250", "period_from": format_utc(start), "period_to": format_utc(end)},
            authenticated=False,
        )

    def standing_charges(self, *, tariff_code: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        product_code = extract_product_code(tariff_code)
        return self._paginated(
            f"products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/",
            params={"page_size": "250", "period_from": format_utc(start), "period_to": format_utc(end)},
            authenticated=False,
        )

    def _post_graphql(self, query: str, variables: dict[str, Any], *, retry_token: bool = True) -> dict[str, Any]:
        if not hasattr(self, "_graphql_token"): self._graphql_token = None
        if self._graphql_token is None:
            mutation = "mutation($input: ObtainJSONWebTokenInput!) { obtainKrakenToken(input: $input) { token } }"
            body = json.dumps({"query": mutation, "variables": {"input": {"APIKey": self.api_key}}}).encode()
            try: raw = self.transport(request.Request(f"{API_BASE_URL}/graphql/", data=body, headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":"energy-dashboard/0.1"}), self.timeout_seconds)
            except error.HTTPError as exc: raise OctopusApiError(f"Octopus GraphQL returned HTTP {exc.code}", transient=(exc.code == 429 or 500 <= exc.code < 600)) from exc
            except (error.URLError, TimeoutError, socket.timeout) as exc: raise OctopusApiError("Octopus GraphQL is temporarily unavailable", transient=True) from exc
            try: token = json.loads(raw.decode()).get("data",{}).get("obtainKrakenToken",{}).get("token")
            except Exception as exc: raise OctopusApiError("Octopus GraphQL returned malformed JSON") from exc
            if not isinstance(token,str) or not token: raise OctopusApiError("Octopus GraphQL authentication failed")
            self._graphql_token = token
        body=json.dumps({"query":query,"variables":variables}).encode()
        try: raw=self.transport(request.Request(f"{API_BASE_URL}/graphql/",data=body,headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":"energy-dashboard/0.1","Authorization":self._graphql_token}),self.timeout_seconds)
        except error.HTTPError as exc: raise OctopusApiError(f"Octopus GraphQL returned HTTP {exc.code}", transient=(exc.code == 429 or 500 <= exc.code < 600)) from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc: raise OctopusApiError("Octopus GraphQL is temporarily unavailable", transient=True) from exc
        try: payload=json.loads(raw.decode(),parse_float=Decimal)
        except Exception as exc: raise OctopusApiError("Octopus GraphQL returned malformed JSON") from exc
        errors=payload.get("errors") or []
        if errors:
            message=" ".join(str(e.get("message","")) for e in errors if isinstance(e,dict)).lower()
            if retry_token and any(marker in message for marker in ("unauthor", "token", "jwt", "signature of the jwt has expired")): self._graphql_token=None; return self._post_graphql(query,variables,retry_token=False)
            safe_message = " ".join(message.split())[:160] or "unknown GraphQL error"
            raise OctopusApiError(f"Octopus Mini telemetry request failed: {safe_message}", transient=("network" in message and "too many" not in message and "rate limit" not in message))
        data=payload.get("data")
        if not isinstance(data,dict): raise OctopusApiError("Octopus GraphQL returned invalid data")
        return data

    def discover_mini_device(self, *, account_number: str) -> str:
        query='''query($accountNumber: String!){ account(accountNumber:$accountNumber){ properties { smartDeviceNetworks { smartDevices { deviceId } } } } }'''
        data=self._post_graphql(query,{"accountNumber":account_number})
        devices=[]
        for prop in (data.get("account") or {}).get("properties") or []:
            for network in (prop or {}).get("smartDeviceNetworks") or []:
                for device in (network or {}).get("smartDevices") or []:
                    if isinstance(device.get("deviceId"),str): devices.append(device["deviceId"])
        # Octopus now exposes generic smart-device IDs without a device type.
        # Probe read-only telemetry and select the only device with recent Mini data.
        end = datetime.now(timezone.utc); start = end - timedelta(hours=24)
        active=[]
        for device_id in dict.fromkeys(devices):
            try:
                rows=self.mini_telemetry(device_id=device_id,start=start,end=end)
            except OctopusApiError:
                continue
            if rows:
                latest = max(str(row.get("readAt", "")) for row in rows)
                active.append((latest, device_id))
        if not active: raise OctopusApiError("Could not discover an electricity Mini device with telemetry")
        # A property can expose archived or secondary meter devices. Prefer
        # the device with the newest actual reading.
        return max(active)[1]

    def mini_telemetry(self, *, device_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        q='''query($deviceId:String!,$start:DateTime!,$end:DateTime!){ smartMeterTelemetry(deviceId:$deviceId,start:$start,end:$end,grouping:TEN_SECONDS){ readAt consumption export demand consumptionDelta costDelta costDeltaWithTax } }'''
        rows=self._post_graphql(q,{"deviceId":device_id,"start":format_utc(start),"end":format_utc(end)}).get("smartMeterTelemetry")
        if not isinstance(rows,list) or not all(isinstance(x,dict) for x in rows): raise OctopusApiError("Octopus Mini returned invalid telemetry")
        return rows
