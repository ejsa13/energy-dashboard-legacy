"""Stable logical sources and canonical metrics used by provider adapters."""

from __future__ import annotations

from typing import Final

SOURCE_SEEDS: Final[tuple[dict[str, object], ...]] = (
    {"code": "octopus_gas", "provider": "octopus", "source_type": "gas_meter", "display_name": "Octopus gas", "is_settlement_authority": True},
    {
        "code": "octopus_import",
        "provider": "octopus",
        "source_type": "settlement_meter",
        "display_name": "Octopus import",
        "is_settlement_authority": True,
    },
    {
        "code": "octopus_export",
        "provider": "octopus",
        "source_type": "settlement_meter",
        "display_name": "Octopus export",
        "is_settlement_authority": True,
    },
    {
        "code": "ecoflow_powerocean",
        "provider": "ecoflow",
        "source_type": "inverter",
        "display_name": "EcoFlow PowerOcean",
        "is_settlement_authority": False,
    },
    {
        "code": "octopus_mini",
        "provider": "octopus",
        "source_type": "home_area_network",
        "display_name": "Octopus Home Mini",
        "is_settlement_authority": False,
    },
)

METRIC_SEEDS: Final[tuple[dict[str, object], ...]] = (
    {"code": "gas_consumption_kwh", "display_name": "Gas consumption", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "grid_import_energy", "display_name": "Grid import", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "grid_export_energy", "display_name": "Grid export", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "solar_generation_energy", "display_name": "Solar generation", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "home_consumption_energy", "display_name": "Home consumption", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "battery_charge_energy", "display_name": "Battery charge", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "battery_discharge_energy", "display_name": "Battery discharge", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "ecoflow_to_power_pulse", "display_name": "To Power Pulse", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "solar_power", "display_name": "Solar power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "home_power", "display_name": "Home power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "grid_power", "display_name": "Grid power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "battery_power", "display_name": "Battery power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "battery_soc", "display_name": "Battery state of charge", "measurement_kind": "ratio", "canonical_unit": "%", "value_type": "number"},
    {"code": "octopus_mini_consumption_power", "display_name": "Mini consumption power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "octopus_mini_export_power", "display_name": "Mini export power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "octopus_mini_demand_power", "display_name": "Mini demand power", "measurement_kind": "power", "canonical_unit": "W", "value_type": "number"},
    {"code": "octopus_mini_consumption_delta", "display_name": "Mini consumption delta", "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"},
    {"code": "octopus_mini_cost_delta", "display_name": "Mini cost delta excluding VAT", "measurement_kind": "diagnostic", "canonical_unit": "pence", "value_type": "number"},
    {"code": "octopus_mini_cost_delta_inc_vat", "display_name": "Mini cost delta including VAT", "measurement_kind": "diagnostic", "canonical_unit": "pence", "value_type": "number"},
)
