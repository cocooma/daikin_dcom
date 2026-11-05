"""Base entity."""
from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DcomCoordinator


class DcomEntity(CoordinatorEntity[DcomCoordinator]):
    """Common base for all DCOM entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DcomCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = coordinator.device_info
