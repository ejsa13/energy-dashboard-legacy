"""Collector-owned, listen-only EcoFlow PowerOcean Enhanced Mode telemetry.

The app login/certification flow is adapted from the MIT-licensed
ecoflow-energy-ha project. Provider payloads are kept in memory only; an
opt-in, redacted decoded summary can be written to the collector log.
"""
from __future__ import annotations

import asyncio, base64, hashlib, json, logging, os, ssl, struct, threading, time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import aiohttp
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.database import get_engine
from app.ecoflow_clientid import generate_client_id
from app.ecoflow_history import EcoFlowAppHistoryClient, EcoFlowHistoryError
from app.repositories import CatalogRepository, LiveProviderHealthRepository, TelemetryRepository, TelemetrySampleInput

logger = logging.getLogger("collector.powerocean")
_SENSITIVE_KEY_PARTS = ("account", "auth", "certificate", "device", "email", "key", "password", "secret", "serial", "token", "user")
_SENSITIVE_IDENTIFIERS = {"id", "clientid", "devicesn", "sn"}


def _log_payloads_enabled() -> bool:
    return os.environ.get("COLLECTOR_LOG_RAW_PAYLOADS", "true").strip().lower() in {"1", "true", "yes", "on"}


def _sanitized_payload(values: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded decoded payload summary without credentials or IDs."""
    safe: dict[str, Any] = {}
    for key, value in values.items():
        normalized = key.lower()
        if normalized in _SENSITIVE_IDENTIFIERS or normalized.endswith("_id") or any(part in normalized for part in _SENSITIVE_KEY_PARTS):
            safe[key] = "[redacted]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value if not isinstance(value, str) else value[:128]
        else:
            safe[key] = f"[{type(value).__name__}]"
    return safe

API_BASES = ("https://api-e.ecoflow.com", "https://api.ecoflow.com")
STALE_SECONDS = 35
SNAPSHOT_SECONDS = 10
CORE = {"mpptPwr": ("solar_power", "Solar power", "power", "W"), "sysLoadPwr": ("home_power", "Home power", "power", "W"), "sysGridPwr": ("grid_power", "Grid power", "power", "W"), "bpPwr": ("battery_power", "Battery power", "power", "W"), "bpSoc": ("battery_soc", "Battery state of charge", "ratio", "%"), "bpOnlineSum": ("ecoflow_battery_pack_count", "Online battery packs", "diagnostic", "count"), "bp_online_sum": ("ecoflow_battery_pack_count", "Online battery packs", "diagnostic", "count"), "emsWorkMode": ("ecoflow_operating_mode", "Operating mode", "status", None), "pcsRunSta": ("ecoflow_inverter_status", "Inverter status", "status", None), "sysGridSta": ("ecoflow_grid_connection", "Grid connection", "status", None), "solar": ("solar_power", "Solar power", "power", "W"), "home_direct": ("home_power", "Home power", "power", "W"), "grid_raw_f2": ("grid_power", "Grid power", "power", "W"), "batt_pb": ("battery_power", "Battery power", "power", "W"), "soc": ("battery_soc", "Battery state of charge", "ratio", "%"), "ems_word_mode": ("ecoflow_operating_mode", "Operating mode", "status", None), "pcs_run_sta": ("ecoflow_inverter_status", "Inverter status", "status", None), "sys_grid_sta": ("ecoflow_grid_connection", "Grid connection", "status", None), "toPowerPulse": ("ecoflow_to_power_pulse", "To Power Pulse", "energy", "kWh"), "to_power_pulse": ("ecoflow_to_power_pulse", "To Power Pulse", "energy", "kWh"), "powerPulseEnergy": ("ecoflow_to_power_pulse", "To Power Pulse", "energy", "kWh")}
CORE["bpFullCap"] = ("ecoflow_battery_capacity_wh", "Battery capacity", "energy", "Wh")
CORE["battery_capacity_wh"] = ("ecoflow_battery_capacity_wh", "Battery capacity", "energy", "Wh")

def _battery_capacity_wh(values: dict[str, Any]) -> Decimal | None:
    """Read EcoFlow's explicit full-capacity field without guessing pack size."""
    def walk(value: Any):
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = "".join(character.lower() for character in str(key) if character.isalnum())
                # `latestQuotas` replies keep this as a dotted quota-map key
                # (`ems_change_report.poAiSchedule.bpFullCap`), whereas live
                # change reports nest it. Both forms describe the same full
                # battery capacity in Wh.
                if normalized in {"bpfullcap", "emstotalbatterycapacitywh"} or normalized.endswith(("bpfullcap", "emstotalbatterycapacitywh")):
                    try:
                        capacity = Decimal(str(nested))
                    except (InvalidOperation, ValueError):
                        continue
                    if Decimal("0") < capacity <= Decimal("1000000"):
                        return capacity
                found = walk(nested)
                if found is not None:
                    return found
        return None
    return walk(values)

def _pack_capacity_wh(pack: dict[str, Any]) -> Decimal | None:
    """Estimate one pack's full energy from its reported remaining Wh and SoC."""
    try:
        remaining = Decimal(str(pack.get("remain_wh")))
        soc = Decimal(str(pack.get("soc")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    capacity = remaining * Decimal("100") / soc if soc > 0 else Decimal("0")
    return capacity if Decimal("100") <= capacity <= Decimal("100000") else None

def _varint(field: int, value: int) -> bytes:
    encoded = bytearray([(field << 3) | 0])
    while value > 127:
        encoded.append((value & 127) | 128); value >>= 7
    encoded.append(value)
    return bytes(encoded)

def _bytes(field: int, value: bytes) -> bytes:
    return bytes([(field << 3) | 2]) + _varint(0, len(value))[1:] + value

def device_get_all_payload() -> bytes:
    """Request a complete PowerOcean state frame (MIT-adapted from HA)."""
    seq = int(time.time() * 1000) & 0x7FFFFFFF
    header = b"".join((_varint(2, 32), _varint(3, 32), _varint(14, seq), _bytes(23, b"app")))
    return _bytes(1, header)

def energy_stream_activate_payload() -> bytes:
    """Portal-compatible PowerOcean EnergyStreamSwitch request (MIT-adapted)."""
    seq = int(time.time() * 1000) & 0x7FFFFFFF
    switch = _varint(1, 1)
    header = b"".join((_bytes(1, switch), _varint(2, 32), _varint(3, 96), _varint(4, 1), _varint(5, 1), _varint(8, 96), _varint(9, 97), _varint(10, len(switch)), _varint(11, 1), _varint(14, seq), _varint(16, 3), _varint(17, 1)))
    return _bytes(1, header)

def device_get_all_payload() -> bytes:
    """Portal-compatible protobuf get-all request, adapted from EcoFlow HA."""
    seq = int(time.time() * 1000) & 0x7FFFFFFF
    return _bytes(1, b"".join((_varint(2, 32), _varint(3, 32), _varint(14, seq), _bytes(23, b"app"))))

class PowerOceanLiveError(RuntimeError): pass

@dataclass(frozen=True)
class PowerOceanLiveConfig:
    email: str; password: str; serial: str; battery_serials: tuple[str, ...] = ()
    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None):
        v=env if env is not None else os.environ; keys=("ECOFLOW_APP_EMAIL","ECOFLOW_APP_PASSWORD","ECOFLOW_POWEROCEAN_SERIAL")
        if any(not v.get(k, "").strip() for k in keys): raise PowerOceanLiveError("Missing required EcoFlow live configuration")
        battery_serials = tuple(dict.fromkeys(
            serial.strip().upper() for serial in v.get("ECOFLOW_BATTERY_SERIALS", "").split(",")
            if serial.strip() and serial.strip().replace("-", "").isalnum()
        ))
        return cls(v["ECOFLOW_APP_EMAIL"],v["ECOFLOW_APP_PASSWORD"],v["ECOFLOW_POWEROCEAN_SERIAL"],battery_serials)

async def app_credentials(config: PowerOceanLiveConfig) -> tuple[dict[str, Any], str]:
    payload={"email":config.email,"password":base64.b64encode(config.password.encode()).decode(),"scene":"IOT_APP","userType":"ECOFLOW"}
    async with aiohttp.ClientSession() as session:
        for base in API_BASES:
            try:
                async with session.post(base+"/auth/login",json=payload,timeout=aiohttp.ClientTimeout(total=10)) as r:
                    body=await r.json()
                data=body.get("data",{})
                token=data.get("token"); user=str((data.get("user") or {}).get("userId", ""))
                if str(body.get("code")) != "0" or not token or not user: continue
                async with session.get(base+"/iot-service/user/device",headers={"Authorization":f"Bearer {token}"},timeout=aiohttp.ClientTimeout(total=10)) as r:
                    devices=await r.json()
                if config.serial not in json.dumps(devices): raise PowerOceanLiveError("Configured PowerOcean was not found in the EcoFlow account")
                async with session.get(base+"/iot-auth/enterprise-development/user/certification",headers={"Authorization":f"Bearer {token}"},timeout=aiohttp.ClientTimeout(total=10)) as r:
                    cert=await r.json()
                encrypted=cert.get("data")
                if str(cert.get("code")) != "0" or not isinstance(encrypted,str): continue
                decryptor=Cipher(algorithms.AES(hashlib.sha256(token.encode()).digest()),modes.CFB(b"ojsajkqjwk1w2dfg")).decryptor()
                raw=decryptor.update(base64.b64decode(encrypted))+decryptor.finalize(); pad=raw[-1] if raw else 0
                if 0 < pad <= 16 and raw[-pad:] == bytes([pad])*pad: raw=raw[:-pad]
                creds=json.loads(raw)
                # EcoFlow has used both names across certificate versions.
                # Keep the raw certificate in memory only and return a stable,
                # minimal connection shape to the worker.
                account = creds.get("certificateAccount") or creds.get("userName")
                credential_password = creds.get("certificatePassword") or creds.get("password")
                if isinstance(creds,dict) and account and credential_password:
                    return {"account": account, "password": credential_password, "url": creds.get("url"), "port": creds.get("port"), "path": creds.get("path")},user
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, json.JSONDecodeError): continue
    raise PowerOceanLiveError("EcoFlow Enhanced Mode authentication failed")

def decode_payload(payload: bytes) -> dict[str, Any]:
    try: value=json.loads(payload.decode())
    except (UnicodeDecodeError,json.JSONDecodeError): return decode_powerocean_protobuf(payload)
    while isinstance(value,dict) and isinstance(value.get("data"),dict): value=value["data"]
    if isinstance(value,dict) and isinstance(value.get("params"),dict): value=value["params"]
    # App reply envelopes commonly put the useful payload in a JSON-encoded
    # value field.  Metadata-only replies must not count as device telemetry.
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        try:
            nested = json.loads(value["value"])
            if isinstance(nested, dict): value = nested
        except json.JSONDecodeError:
            pass
    if not isinstance(value, dict):
        return {}
    capacity = _battery_capacity_wh(value)
    if capacity is not None:
        value = {**value, "battery_capacity_wh": capacity}
    return value

def _protobuf_fields(blob: bytes) -> dict[int, list[Any]]:
    """Small, read-only protobuf reader for the documented PowerOcean frames."""
    out: dict[int, list[Any]] = {}; i=0
    while i < len(blob):
        key=0; shift=0
        while i < len(blob):
            byte=blob[i]; i+=1; key|=(byte & 127)<<shift
            if not byte & 128: break
            shift += 7
        field, wire=key>>3,key&7
        if not field: break
        if wire == 0:
            value=0; shift=0
            while i < len(blob):
                byte=blob[i]; i+=1; value|=(byte&127)<<shift
                if not byte&128: break
                shift+=7
        elif wire == 2:
            length=0; shift=0
            while i < len(blob):
                byte=blob[i]; i+=1; length|=(byte&127)<<shift
                if not byte&128: break
                shift+=7
            value=blob[i:i+length]; i+=length
        elif wire == 5:
            if i+4>len(blob): break
            value=struct.unpack("<f",blob[i:i+4])[0]; i+=4
        elif wire == 1:
            if i+8>len(blob): break
            value=blob[i:i+8]; i+=8
        else: break
        out.setdefault(field,[]).append(value)
    return out

def _decode_powerocean_single(payload: bytes) -> dict[str, Any]:
    """Decode PowerOcean Enhanced Mode energy, EMS and battery heartbeat frames.

    Field layout is adapted from the MIT-licensed ecoflow-energy-ha protocol
    definitions. Unknown frames are ignored rather than guessed.
    """
    outer=_protobuf_fields(payload); header=(outer.get(1) or [b""])[0]
    if not isinstance(header,bytes): return {}
    fields=_protobuf_fields(header)
    # Incremental property frames carry pdata in Header.field 1. A Get-All
    # reply often carries one header plus its pdata in envelope.field 2.
    inner=(fields.get(1) or outer.get(2) or [b""])[0]
    command=(fields.get(8) or [None])[0], (fields.get(9) or [None])[0]
    if not isinstance(inner,bytes): return {}
    data=_protobuf_fields(inner)
    if command == (96,33):
        names={1:"home_direct",2:"grid_raw_f2",3:"solar",4:"batt_pb",5:"soc"}
        return {name:data[number][0] for number,name in names.items() if data.get(number)}
    if command == (96,8):
        names={1:"sys_work_sta",2:"sys_grid_sta",3:"ems_word_mode",7:"soc",21:"pcs_run_sta",9:"bp_online_sum"}
        values={name:data[number][0] for number,name in names.items() if data.get(number)}
        # Only translate status fields that the frame actually contains.
        # Mapping absent fields used to add Python None values, which were
        # subsequently persisted as the text "None".
        if "sys_grid_sta" in values:
            values["sys_grid_sta"]={0:"not_detected",1:"connected"}.get(values["sys_grid_sta"], values["sys_grid_sta"])
        if "ems_word_mode" in values:
            values["ems_word_mode"]={0:"self_use",1:"time_of_use",2:"backup",3:"debug",4:"ac_makeup",5:"drm",6:"remote_schedule",7:"standby",8:"soc_calibration",9:"timer",10:"fcr",11:"third_party",12:"ai_schedule",13:"kraken"}.get(values["ems_word_mode"], values["ems_word_mode"])
        if "pcs_run_sta" in values:
            values["pcs_run_sta"]={0:"standby",1:"running",2:"stopped"}.get(values["pcs_run_sta"], values["pcs_run_sta"])
        return values
    if command == (96,17):
        names = {
            22: "pcs_ac_error_code", 23: "pcs_dc_error_code", 191: "pcs_ac_warning_code",
            194: "mppt1_fault_code", 195: "mppt2_fault_code", 196: "mppt1_warning_code",
            197: "mppt2_warning_code", 198: "battery_line_off", 201: "battery_relay_fault",
            211: "afci_fault_ch1", 216: "afci_fault_ch2",
        }
        return {name: data[number][0] for number, name in names.items() if data.get(number)}
    if command == (96,7):
        packs=[value for value in data.get(1,[]) if isinstance(value,bytes)]
        if not packs: return {}
        battery=_protobuf_fields(packs[0]); decoded=[]
        for pack in packs:
            fields=_protobuf_fields(pack); raw_serial=(fields.get(16) or [b""])[0]
            try: serial=raw_serial.decode("ascii") if isinstance(raw_serial,bytes) else ""
            except UnicodeDecodeError: serial=""
            if serial:
                # Follow ecoflow-energy-ha's BpInfo parser. Newer firmware
                # reports named BMS temperatures rather than legacy bp_temp.
                pack = {"serial": serial, "power": (fields.get(1) or [None])[0],
                        "soc": (fields.get(2) or [None])[0],
                        "soh": (fields.get(3) or [None])[0],
                        "cycles": (fields.get(17) or [None])[0],
                        "full_cap_mah": (fields.get(29) or [None])[0],
                        "remain_wh": (fields.get(54) or [None])[0]}
                temperature_fields = (("env_temp_c", 25), ("max_cell_temp_c", 30),
                    ("min_cell_temp_c", 31), ("max_mos_temp_c", 32),
                    ("hv_mos_temp_c", 19), ("lv_mos_temp_c", 20), ("ptc_temp_c", 21))
                for name, field_number in temperature_fields:
                    if fields.get(field_number):
                        pack[name] = fields[field_number][0]
                # Prefer ambient temperature, then the HA parser's other BMS diagnostics.
                for name, _ in temperature_fields:
                    if pack.get(name) is not None:
                        pack["temperature"] = pack[name]
                        break
                else:
                    pack["temperature"] = (fields.get(5) or [None])[0]
                decoded.append(pack)
        result={"bp_online_sum":len(decoded) or len(packs), "__packs":decoded, "__battery_heartbeat": True}
        if battery.get(1): result["batt_pb"]=battery[1][0]
        if battery.get(2): result["soc"]=battery[2][0]
        return result
    if command == (96,3):
        serials=[]
        for entry in data.get(3, []):
            if isinstance(entry, bytes):
                raw=(_protobuf_fields(entry).get(1) or [b""])[0]
                if isinstance(raw, bytes):
                    try: serials.append(raw.decode("ascii"))
                    except UnicodeDecodeError: pass
        return {"__battery_serials": serials} if serials else {}
    return {}


def decode_powerocean_protobuf(payload: bytes) -> dict[str, Any]:
    """Decode all headers in a PowerOcean MQTT envelope.

    Get-All replies carry several independent headers in one envelope.  The
    battery heartbeat (96/7) is one of those headers, so reading only header
    one loses every pack despite otherwise healthy live power telemetry.
    """
    outer = _protobuf_fields(payload)
    headers = [header for header in outer.get(1, []) if isinstance(header, bytes)]
    if len(headers) <= 1:
        return _decode_powerocean_single(payload)
    merged: dict[str, Any] = {}
    packs: list[dict[str, Any]] = []
    serials: list[str] = []
    heartbeat = False
    for header in headers:
        decoded = _decode_powerocean_single(_bytes(1, header))
        if not decoded:
            continue
        packs.extend(decoded.pop("__packs", []))
        heartbeat = heartbeat or bool(decoded.pop("__battery_heartbeat", False))
        serials.extend(decoded.pop("__battery_serials", []))
        merged.update(decoded)
    if packs:
        merged["__packs"] = packs
        merged["__battery_heartbeat"] = heartbeat
    if serials:
        merged["__battery_serials"] = list(dict.fromkeys(serials))
    return merged

class PowerOceanLiveWorker:
    def __init__(self, stop: threading.Event, dashboard_id: str | None = None, env: dict[str, str] | None = None):
        self.stop=stop; self.env=env; self.engine=get_engine(); self.dashboard_id=dashboard_id; self.catalog=CatalogRepository(self.engine); self.telemetry=TelemetryRepository(self.engine, dashboard_id); self.health=LiveProviderHealthRepository(self.engine, dashboard_id); self.last_message=0.0; self.last_persist=0.0; self.pending: dict[str,Any]={}; self.client: mqtt.Client|None=None; self.battery_packs:set[bytes]=set(); self.battery_topics:set[str]=set(); self.pack_capacity_wh: dict[str, Decimal]={}; self.expected_battery_packs=0; self.inverter_serial=""
    def _metrics(self, values: dict[str,Any]) -> list[dict[str,str|None]]:
        out=[]
        for k,v in values.items():
            if not isinstance(k,str) or not k.replace("_","").isalnum(): continue
            code=CORE.get(k,(k.lower() if k.startswith("ecoflow_") else "ecoflow_"+k.lower(),k,"diagnostic",None))[0]
            if code in {"solar_power", "home_power", "grid_power", "battery_power", "battery_soc"}: continue
            typ="boolean" if isinstance(v,bool) else "number" if isinstance(v,(int,float,Decimal)) else "text" if isinstance(v,str) else None
            if code in {"ecoflow_operating_mode", "ecoflow_inverter_status", "ecoflow_grid_connection"}: typ="text"
            meta = CORE.get(k)
            if typ: out.append({"code":code,"display_name":(meta[1] if meta else k)[:128],"measurement_kind":(meta[2] if meta else "diagnostic"),"canonical_unit":(meta[3] if meta else None),"value_type":typ})
        return out
    def _persist(self, transport: str) -> None:
        if not self.pending: return
        now=datetime.now(timezone.utc); values=self.pending; self.pending={}; self.catalog.register_metrics(self._metrics(values))
        items=[]
        for raw,value in values.items():
            meta=CORE.get(raw); code=meta[0] if meta else (raw.lower() if raw.startswith("ecoflow_") else "ecoflow_"+raw.lower())
            try:
                if code in {"ecoflow_operating_mode", "ecoflow_inverter_status", "ecoflow_grid_connection"}: items.append(TelemetrySampleInput("ecoflow_powerocean",code,now,now,value_text=str(value)[:256]))
                elif isinstance(value,bool): items.append(TelemetrySampleInput("ecoflow_powerocean",code,now,now,value_boolean=value))
                elif isinstance(value,(int,float,Decimal)) and Decimal(str(value)).is_finite(): items.append(TelemetrySampleInput("ecoflow_powerocean",code,now,now,value_number=Decimal(str(value))))
                elif isinstance(value,str) and len(value)<=256: items.append(TelemetrySampleInput("ecoflow_powerocean",code,now,now,value_text=value))
            except (InvalidOperation,ValueError): pass
        if items:
            self.telemetry.insert_batch(items)
            rendered = ", ".join(
                f"{item.metric_code}={item.value_number if item.value_number is not None else item.value_boolean if item.value_boolean is not None else item.value_text}"
                for item in items if "serial" not in item.metric_code
            )
            logger.info("EcoFlow telemetry persisted: %s reading(s)%s", len(items), f"; {rendered}" if rendered else "")
            self.health.update(source_code="ecoflow_powerocean",dataset="telemetry",state="healthy",attempted_at=now,sample_at=now,transport=transport,connection_state="connected")
    def _message(self, client, _userdata, message):
        values=decode_payload(message.payload)
        if _log_payloads_enabled() and values:
            logger.info("EcoFlow decoded payload (sanitized): %s", _sanitized_payload(values))
        # A connection acknowledgement or command reply is not a device
        # reading.  It must never make the UI claim that live data arrived.
        packs = values.pop("__packs", [])
        is_battery_heartbeat = values.pop("__battery_heartbeat", False)
        try:
            self.expected_battery_packs = max(self.expected_battery_packs, int(values.get("bp_online_sum") or 0))
        except (TypeError, ValueError):
            pass
        topic_serial = message.topic.rsplit("/", 1)[-1] if message.topic.startswith("/app/device/property/") else ""
        if is_battery_heartbeat and topic_serial and topic_serial != self.inverter_serial and packs:
            for pack in packs:
                if not pack.get("serial"): pack["serial"] = topic_serial
        for serial in values.pop("__battery_serials", []):
            # PowerOcean exposes attached module serials in its own 96/3
            # inventory frame.  Make that discovery visible immediately;
            # the following 96/7 heartbeat fills the per-pack measurements.
            if serial and serial != self.inverter_serial:
                normalized = "".join(ch.lower() for ch in serial if ch.isalnum())
                if normalized:
                    values[f"ecoflow_pack_{normalized}_serial"] = serial
                if serial not in self.battery_topics:
                    client.subscribe(f"/app/device/property/{serial}", qos=0)
                    self.battery_topics.add(serial)
        for pack in packs:
            serial=pack.get("serial", "")
            if not serial: continue
            self.battery_packs.add(serial.encode())
            capacity = _pack_capacity_wh(pack)
            if capacity is not None:
                self.pack_capacity_wh[serial] = capacity
            prefix="ecoflow_pack_" + "".join(ch.lower() for ch in serial if ch.isalnum())
            for field, value in pack.items():
                if field != "serial" and value is not None: values[f"{prefix}_{field}"] = value
            if capacity is not None:
                values[f"{prefix}_capacity_wh"] = capacity
            values[f"{prefix}_serial"] = serial
        if self.battery_packs: values["bp_online_sum"] = len(self.battery_packs)
        expected = self.expected_battery_packs or len(self.battery_packs)
        if expected and len(self.pack_capacity_wh) >= expected:
            values.setdefault("battery_capacity_wh", sum(self.pack_capacity_wh.values(), Decimal("0")))
        # Some app property replies include status keys with a JSON null value.
        # Do not store these placeholders: they otherwise overwrite a useful
        # status reading and are rendered by the UI as the literal "None".
        values = {
            key: value
            for key, value in values.items()
            if key not in {"command", "deviceSn", "value"} and value is not None
        }
        if values: self.pending.update(values); self.last_message=time.monotonic()
    def _fetch_capacity_quota(self, serial: str) -> None:
        """Use EcoFlow's signed quota endpoint when MQTT omits capacity."""
        environment = self.env if self.env is not None else os.environ
        access_key, secret_key = environment.get("ECOFLOW_ACCESS_KEY", "").strip(), environment.get("ECOFLOW_SECRET_KEY", "")
        if not access_key or not secret_key:
            return
        try:
            capacity = _battery_capacity_wh(EcoFlowAppHistoryClient(access_key, secret_key).quota_all(serial))
        except EcoFlowHistoryError:
            return
        if capacity is not None:
            self.pending["battery_capacity_wh"] = capacity
            logger.info("EcoFlow battery capacity quota received: %s Wh", capacity)
    def run(self) -> None:
        config=PowerOceanLiveConfig.from_environment(self.env)
        while not self.stop.is_set():
            try:
                self._fetch_capacity_quota(config.serial)
                self.health.update(source_code="ecoflow_powerocean",dataset="telemetry",state="syncing",attempted_at=datetime.now(timezone.utc),reason="EcoFlow MQTT is connecting",connection_state="connecting")
                creds,user=asyncio.run(app_credentials(config)); account=creds["account"]; password=creds["password"]
                self.inverter_serial=config.serial
                self.pending["inverter_serial"] = config.serial
                self.battery_topics.update(config.battery_serials)
                parsed=urlparse(str(creds.get("url") or "")); host=parsed.hostname or "mqtt-e.ecoflow.com"; port=int(creds.get("port") or parsed.port or 8084); path=str(creds.get("path") or parsed.path or "/mqtt")
                c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id=generate_client_id(user),transport="websockets",clean_session=True); c.username_pw_set(account,password); c.tls_set_context(ssl.create_default_context()); c.ws_set_options(path=path)
                c.on_message=self._message
                def connected(client, _userdata, _flags, reason_code, _properties=None):
                    code = reason_code.value if hasattr(reason_code, "value") else reason_code
                    if code == 0:
                        logger.info("EcoFlow MQTT connected; requesting current device state.")
                        client.subscribe(f"/open/{account}/{config.serial}/quota",qos=1); client.subscribe(f"/app/device/property/{config.serial}",qos=0); client.subscribe(f"/app/{user}/{config.serial}/thing/property/get_reply",qos=1)
                        # Subscribe to the configured physical packs directly. These are
                        # listen-only Enhanced Mode topics; no command is sent to a battery.
                        for battery_serial in config.battery_serials:
                            client.subscribe(f"/open/{account}/{battery_serial}/quota", qos=1)
                            client.subscribe(f"/app/device/property/{battery_serial}", qos=0)
                        # Match EcoFlow Energy HA: activate EnergyStream, then request
                        # complete device state including inventory and pack reports.
                        client.publish(f"/app/{user}/{config.serial}/thing/property/set", energy_stream_activate_payload(), qos=1)
                        client.publish(f"/app/{user}/{config.serial}/thing/property/get", device_get_all_payload(), qos=1)
                        client.publish(f"/app/{user}/{config.serial}/thing/property/get", device_get_all_payload(), qos=1)
                        client.publish(f"/app/{user}/{config.serial}/thing/property/get", json.dumps({"from":"Android","id":str(int(time.time()*1000)),"moduleType":0,"operateType":"latestQuotas","params":{},"version":"1.0"}), qos=1)
                        self.health.update(source_code="ecoflow_powerocean",dataset="telemetry",state="syncing",attempted_at=datetime.now(timezone.utc),reason="EcoFlow MQTT connected; waiting for telemetry",connection_state="connected")
                c.on_connect=connected
                c.connect(host,port,keepalive=30); c.loop_start(); self.client=c; self.last_message=time.monotonic(); next_activation=time.monotonic()+20; next_snapshot=time.monotonic()+60; next_quotas=time.monotonic()+30; next_capacity_quota=time.monotonic()+21600
                while not self.stop.wait(1):
                    now=time.monotonic()
                    if now >= next_activation:
                        c.publish(f"/app/{user}/{config.serial}/thing/property/set", energy_stream_activate_payload(), qos=1)
                        next_activation=now+20
                    if now >= next_snapshot:
                        # This is a read-only state request. It refreshes the
                        # pack heartbeat (and therefore remaining energy) on
                        # firmware that does not publish it spontaneously.
                        c.publish(f"/app/{user}/{config.serial}/thing/property/get", device_get_all_payload(), qos=1)
                        next_snapshot=now+60
                    if now >= next_quotas:
                        # The full-capacity quota is not part of the regular
                        # PowerOcean heartbeat. Poll it regularly: EcoFlow may
                        # ignore the first request while its MQTT session is
                        # still negotiating after a reconnect.
                        c.publish(f"/app/{user}/{config.serial}/thing/property/get", json.dumps({"from":"Android","id":str(int(time.time()*1000)),"moduleType":0,"operateType":"latestQuotas","params":{},"version":"1.0"}), qos=1)
                        next_quotas=now+30
                    if now >= next_capacity_quota:
                        self._fetch_capacity_quota(config.serial)
                        next_capacity_quota=now+21600
                    if self.pending and now-self.last_persist>=SNAPSHOT_SECONDS: self._persist("mqtt"); self.last_persist=now
                    if now-self.last_message>STALE_SECONDS:
                        logger.warning("EcoFlow MQTT telemetry is stale; reconnecting.")
                        self.health.update(source_code="ecoflow_powerocean",dataset="telemetry",state="failed",attempted_at=datetime.now(timezone.utc),reason="EcoFlow MQTT is connected but telemetry is stale",connection_state="stale")
                        break
                c.loop_stop(); c.disconnect()
                if not self.stop.is_set(): self.stop.wait(5)
            except Exception:
                logger.exception("EcoFlow MQTT worker encountered an error; retrying in 10 seconds.")
                self.health.update(source_code="ecoflow_powerocean",dataset="telemetry",state="failed",attempted_at=datetime.now(timezone.utc),reason="EcoFlow MQTT connection needs attention",connection_state="disconnected")
                self.stop.wait(10)
