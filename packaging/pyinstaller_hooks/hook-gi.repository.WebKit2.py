# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Collect WebKit2GTK 4.1 when available, otherwise Jammy's 4.0 ABI."""

from PyInstaller.utils.hooks.gi import GiModuleInfo

binaries = []
datas = []
hiddenimports = []
for version in ("4.1", "4.0"):
    module_info = GiModuleInfo("WebKit2", version)
    if module_info.available:
        binaries, datas, hiddenimports = module_info.collect_typelib_data()
        break
