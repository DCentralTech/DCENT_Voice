// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using Microsoft.Win32;

internal static class RecoveryCoordinator
{
    internal const string DefaultRegistrySubKey =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice";

    private sealed record PendingRecovery(string CommandPath, string StatePath);

    internal static void ReconcilePendingUninstall(
        string installRoot,
        string registrySubKey = DefaultRegistrySubKey)
    {
        var pending = ReadPendingRecovery(registrySubKey);
        if (pending is null)
        {
            return;
        }

        ValidatePendingRecovery(installRoot, registrySubKey, pending);
        var exitCode = RunCommand(pending.CommandPath, "/S", timeoutMilliseconds: 120000);
        if (exitCode != 0)
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery could not be completed (exit " + exitCode + ").");
        }
        if (ReadPendingRecovery(registrySubKey) is not null)
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery returned success but its registration remains.");
        }
    }

    internal static void ClearRecoveryValues(RegistryKey key)
    {
        key.DeleteValue("DCENTRecoveryUninstaller", throwOnMissingValue: false);
        key.DeleteValue("DCENTRecoveryState", throwOnMissingValue: false);
    }

    internal static int RunUninstaller(
        string installRoot,
        bool silent,
        bool purgeUserData,
        string registrySubKey = DefaultRegistrySubKey,
        bool registeredInstall = true)
    {
        var uninstallCommand = Path.Combine(installRoot, "Uninstall.cmd");
        var pending = registeredInstall ? ReadPendingRecovery(registrySubKey) : null;
        if (pending is not null)
        {
            ValidatePendingRecovery(installRoot, registrySubKey, pending);
            uninstallCommand = pending.CommandPath;
        }
        if (!File.Exists(uninstallCommand))
        {
            throw new InvalidOperationException(
                "Registered uninstaller is missing: " + uninstallCommand);
        }

        var arguments = (silent ? "/S" : "") +
            (purgeUserData ? " /PurgeUserData" : "");
        return RunCommand(uninstallCommand, arguments, timeoutMilliseconds: 120000);
    }

    private static PendingRecovery? ReadPendingRecovery(string registrySubKey)
    {
        using var key = Registry.CurrentUser.OpenSubKey(registrySubKey);
        if (key is null)
        {
            return null;
        }

        var commandValue = key.GetValue("DCENTRecoveryUninstaller");
        var stateValue = key.GetValue("DCENTRecoveryState");
        if (commandValue is null && stateValue is null)
        {
            return null;
        }
        if (commandValue is not string command || string.IsNullOrWhiteSpace(command) ||
            stateValue is not string state || string.IsNullOrWhiteSpace(state))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery registration is incomplete or invalid.");
        }
        return new PendingRecovery(Path.GetFullPath(command), Path.GetFullPath(state));
    }

    private static void ValidatePendingRecovery(
        string installRoot,
        string registrySubKey,
        PendingRecovery pending)
    {
        var target = CanonicalPath(installRoot);
        if (!File.Exists(pending.CommandPath) || !File.Exists(pending.StatePath))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery files are missing; refusing to replace the install.");
        }

        var recoveryRoot = Path.GetDirectoryName(pending.StatePath)
            ?? throw new InvalidOperationException("Pending uninstall state has no parent directory.");
        AssertNotReparsePoint(recoveryRoot, "recovery directory");
        AssertNotReparsePoint(pending.CommandPath, "recovery command");
        AssertNotReparsePoint(pending.StatePath, "recovery state");

        using var document = JsonDocument.Parse(File.ReadAllText(pending.StatePath));
        var state = document.RootElement;
        var schemaVersion = RequiredInt32(state, "SchemaVersion");
        if (schemaVersion is < 3 or > 4)
        {
            throw new InvalidOperationException("Pending uninstall recovery schema is unsupported.");
        }

        var transactionText = RequiredString(state, "TransactionId");
        var transactionId = Guid.ParseExact(transactionText, "N").ToString("N");
        var parent = Directory.GetParent(target)?.FullName
            ?? throw new InvalidOperationException("Install root has no parent directory.");
        var leaf = Path.GetFileName(target);
        var expectedRecoveryRoot = CanonicalPath(
            Path.Combine(parent, "." + leaf + ".uninstall-" + transactionId + ".recovery"));
        var expectedTombstone = CanonicalPath(
            Path.Combine(parent, "." + leaf + ".uninstall-" + transactionId + ".payload"));
        var expectedState = CanonicalPath(Path.Combine(expectedRecoveryRoot, "transaction.json"));
        var expectedCommand = CanonicalPath(Path.Combine(expectedRecoveryRoot, "Uninstall.cmd"));

        RequireSamePath(RequiredString(state, "InstallRoot"), target, "install root");
        RequireSamePath(RequiredString(state, "RecoveryRoot"), expectedRecoveryRoot, "recovery root");
        RequireSamePath(RequiredString(state, "TombstonePath"), expectedTombstone, "tombstone");
        RequireSamePath(RequiredString(state, "StatePath"), expectedState, "state file");
        RequireSamePath(RequiredString(state, "RecoveryCommand"), expectedCommand, "command");
        RequireSamePath(pending.StatePath, expectedState, "registered state file");
        RequireSamePath(pending.CommandPath, expectedCommand, "registered command");

        var expectedRegistryPath = "HKCU:\\" + registrySubKey.TrimStart('\\');
        if (!RequiredString(state, "RegistryPath").Equals(
                expectedRegistryPath,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery is bound to a different registry key.");
        }
        if (registrySubKey.Equals(DefaultRegistrySubKey, StringComparison.OrdinalIgnoreCase))
        {
            RequireSamePath(
                RequiredString(state, "ProgramsRoot"),
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                    "DCENT_Voice"),
                "Start Menu cleanup root");
            RequireSamePath(
                RequiredString(state, "UserDataRoot"),
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "DCENT_Voice"),
                "user-data cleanup root");
            if (schemaVersion >= 4)
            {
                RequireSamePath(
                    RequiredString(state, "ModelDataRoot"),
                    Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                        "DCENT_Voice.Models"),
                    "model-data cleanup root");
            }
            RequireSamePath(
                RequiredString(state, "AdeModulesRoot"),
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "DCENT", "modules"),
                "ADE cleanup root");
            if (!RequiredString(state, "RunRegistryPath").Equals(
                    @"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
                    StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Pending uninstall recovery Run-key binding is invalid.");
            if (!RequiredString(state, "CredentialService").Equals(
                    "DCENT_Voice", StringComparison.Ordinal))
                throw new InvalidOperationException(
                    "Pending uninstall recovery credential binding is invalid.");
        }
    }

    private static int RunCommand(string command, string arguments, int timeoutMilliseconds)
    {
        using var process = Process.Start(new ProcessStartInfo
        {
            FileName = command,
            Arguments = arguments,
            UseShellExecute = false,
            CreateNoWindow = true,
        });
        if (process is null)
        {
            throw new InvalidOperationException("Could not start registered uninstaller.");
        }
        if (!process.WaitForExit(timeoutMilliseconds))
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            throw new TimeoutException(
                "Registered uninstaller exceeded its " +
                (timeoutMilliseconds / 1000) + " second bound.");
        }
        return process.ExitCode;
    }

    private static string RequiredString(JsonElement state, string propertyName)
    {
        if (!state.TryGetProperty(propertyName, out var value) ||
            value.ValueKind != JsonValueKind.String ||
            string.IsNullOrWhiteSpace(value.GetString()))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery state omits " + propertyName + ".");
        }
        return value.GetString()!;
    }

    private static int RequiredInt32(JsonElement state, string propertyName)
    {
        if (!state.TryGetProperty(propertyName, out var value) || !value.TryGetInt32(out var result))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery state omits " + propertyName + ".");
        }
        return result;
    }

    private static void RequireSamePath(string actual, string expected, string description)
    {
        if (!CanonicalPath(actual).Equals(CanonicalPath(expected), StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "Pending uninstall recovery " + description + " binding is invalid.");
        }
    }

    private static string CanonicalPath(string path) =>
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));

    private static void AssertNotReparsePoint(string path, string description)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidOperationException(
                "Pending uninstall " + description + " is a reparse point.");
        }
    }
}
