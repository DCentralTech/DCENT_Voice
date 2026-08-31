# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""ADE attachment helpers for DCENT_Voice."""

from .client import AttachError, VoiceAttachClient
from .contract import API_VERSION
from .registry import (
    ModuleRegistryEntry,
    create_registry_entry,
    default_registry_dir,
    read_registry_entry,
    remove_registry_entry,
    remove_stale_registry_entries,
    write_registry_entry,
)
from .single_instance import SingleInstanceLock

__all__ = [
    "API_VERSION",
    "AttachError",
    "ModuleRegistryEntry",
    "SingleInstanceLock",
    "VoiceAttachClient",
    "create_registry_entry",
    "default_registry_dir",
    "read_registry_entry",
    "remove_registry_entry",
    "remove_stale_registry_entries",
    "write_registry_entry",
]
