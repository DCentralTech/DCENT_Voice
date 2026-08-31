// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.IO;
using Microsoft.Win32;

internal sealed class InstallRegistrationSnapshot
{
    private readonly string programsRoot;
    private readonly string shortcutPath;
    private readonly string registrySubKey;
    private readonly bool programsExisted;
    private readonly byte[]? shortcutBytes;
    private readonly Dictionary<string, (object Value, RegistryValueKind Kind)>? registryValues;

    private InstallRegistrationSnapshot(
        string programsRoot,
        string registrySubKey,
        bool programsExisted,
        byte[]? shortcutBytes,
        Dictionary<string, (object, RegistryValueKind)>? registryValues)
    {
        this.programsRoot = programsRoot;
        shortcutPath = Path.Combine(programsRoot, "DCENT_Voice.lnk");
        this.registrySubKey = registrySubKey;
        this.programsExisted = programsExisted;
        this.shortcutBytes = shortcutBytes;
        this.registryValues = registryValues;
    }

    internal static InstallRegistrationSnapshot Capture(
        string programsRoot,
        string registrySubKey = RecoveryCoordinator.DefaultRegistrySubKey)
    {
        var programs = Path.GetFullPath(programsRoot);
        var programsExisted = Directory.Exists(programs);
        if (programsExisted) AssertPlain(programs, "Start Menu directory", directory: true);
        var shortcut = Path.Combine(programs, "DCENT_Voice.lnk");
        byte[]? shortcutBytes = null;
        if (File.Exists(shortcut))
        {
            AssertPlain(shortcut, "Start Menu shortcut", directory: false);
            shortcutBytes = File.ReadAllBytes(shortcut);
        }

        Dictionary<string, (object, RegistryValueKind)>? values = null;
        using (var key = Registry.CurrentUser.OpenSubKey(registrySubKey))
        {
            if (key is not null)
            {
                values = new(StringComparer.Ordinal);
                foreach (var name in key.GetValueNames())
                {
                    var value = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames)
                        ?? throw new InvalidOperationException(
                            "Existing uninstall registration contains an unreadable value: " + name);
                    values.Add(name, (value, key.GetValueKind(name)));
                }
                if (key.SubKeyCount != 0)
                    throw new InvalidOperationException(
                        "Existing uninstall registration has unexpected child keys.");
            }
        }
        return new(programs, registrySubKey, programsExisted, shortcutBytes, values);
    }

    internal void Restore()
    {
        Registry.CurrentUser.DeleteSubKeyTree(registrySubKey, throwOnMissingSubKey: false);
        if (registryValues is not null)
        {
            using var key = Registry.CurrentUser.CreateSubKey(registrySubKey, writable: true)
                ?? throw new InvalidOperationException("Could not restore uninstall registration.");
            foreach (var pair in registryValues)
                key.SetValue(pair.Key, pair.Value.Value, pair.Value.Kind);
        }

        if (Directory.Exists(programsRoot))
            AssertPlain(programsRoot, "Start Menu directory", directory: true);
        if (shortcutBytes is not null)
        {
            Directory.CreateDirectory(programsRoot);
            File.WriteAllBytes(shortcutPath, shortcutBytes);
        }
        else if (File.Exists(shortcutPath))
        {
            AssertPlain(shortcutPath, "Start Menu shortcut", directory: false);
            File.Delete(shortcutPath);
        }
        if (!programsExisted && Directory.Exists(programsRoot) &&
            Directory.GetFileSystemEntries(programsRoot).Length == 0)
            Directory.Delete(programsRoot);
    }

    private static void AssertPlain(string path, string label, bool directory)
    {
        var attributes = File.GetAttributes(path);
        if ((attributes & FileAttributes.ReparsePoint) != 0 ||
            directory != ((attributes & FileAttributes.Directory) != 0))
            throw new InvalidOperationException(label + " is unsafe: " + path);
    }
}
