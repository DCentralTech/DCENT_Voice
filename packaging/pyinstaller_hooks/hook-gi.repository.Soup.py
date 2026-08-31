# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Collect the Soup ABI paired with the Linux builder's WebKit2GTK."""

from PyInstaller.utils.hooks.gi import GiModuleInfo

binaries = []
datas = []
hiddenimports = []
for version in ("3.0", "2.4"):
    module_info = GiModuleInfo("Soup", version)
    if module_info.available:
        binaries, datas, hiddenimports = module_info.collect_typelib_data()
        break
