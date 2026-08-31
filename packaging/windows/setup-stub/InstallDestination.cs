// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;

internal static class InstallDestination
{
    internal static bool IsLegacyModelsOnlyRoot(string candidate) =>
        IsLegacyModelsOnlyRoot(
            candidate,
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "DCENT_Voice"));

    internal static bool IsLegacyModelsOnlyRoot(string candidate, string expectedDefaultRoot)
    {
        if (String.IsNullOrWhiteSpace(candidate)) return false;
        var destination = Canonical(candidate);
        var expected = Canonical(expectedDefaultRoot);
        if (!Same(destination, expected) || !Directory.Exists(destination)) return false;
        try
        {
            AssertExistingAncestorsArePlain(destination);
            AssertPlainDirectory(destination, "legacy application-data root");
            var entries = new DirectoryInfo(destination).EnumerateFileSystemInfos().ToArray();
            if (entries.Length != 1 ||
                entries[0] is not DirectoryInfo models ||
                !models.Name.Equals("models", StringComparison.Ordinal) ||
                (models.Attributes & FileAttributes.ReparsePoint) != 0)
                return false;
            var registry = new FileInfo(Path.Combine(models.FullName, "dcent-voice-models.json"));
            return !registry.Exists ||
                ((registry.Attributes & FileAttributes.ReparsePoint) == 0 &&
                 registry.Length is > 0 and <= 1024 * 1024);
        }
        catch (IOException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
    }

    internal static string ValidateForInstall(string candidate)
    {
        if (String.IsNullOrWhiteSpace(candidate))
            throw new ArgumentException("The install destination is empty.");
        var destination = Canonical(candidate);
        var root = Path.GetPathRoot(destination);
        if (String.IsNullOrWhiteSpace(root) || Same(destination, root))
            throw new InvalidOperationException("Refusing a filesystem root as the install destination.");

        foreach (var protectedPath in ProtectedDirectories())
        {
            if (Same(destination, protectedPath))
                throw new InvalidOperationException(
                    "Refusing a profile, shell, or system directory as the install destination: " + destination);
            if (IsBelow(protectedPath, destination))
                throw new InvalidOperationException(
                    "Refusing a broad directory that contains a protected location: " + destination);
        }

        AssertExistingAncestorsArePlain(destination);
        if (!Directory.Exists(destination))
        {
            if (File.Exists(destination))
                throw new InvalidOperationException("Install destination is an existing file: " + destination);
            return destination;
        }

        AssertPlainDirectory(destination, "install destination");
        if (!Directory.EnumerateFileSystemEntries(destination).Any())
            return destination;
        AssertOwnedInstall(destination);
        return destination;
    }

    private static void AssertOwnedInstall(string destination)
    {
        // Identity is the frozen executable. Do not require Uninstall.ps1,
        // Uninstall.cmd, or the offline-bundle manifest: portable trees omit
        // the helpers, a crashed Setup can leave a valid payload without them,
        // and antivirus commonly quarantines Uninstall.ps1 on other PCs.
        var executable = RequiredPlainFile(destination, "dcent-voice.exe");
        var version = FileVersionInfo.GetVersionInfo(executable);
        if (!String.Equals(version.ProductName, "DCENT_Voice", StringComparison.Ordinal))
            throw new InvalidOperationException(
                "Existing destination is not a registered DCENT_Voice payload (executable identity).");

        var bundle = Path.Combine(destination, "dcent-voice-offline-bundle.json");
        var bundleInfo = new FileInfo(bundle);
        if (!bundleInfo.Exists)
            return;
        if ((bundleInfo.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException(
                "Existing non-empty destination is not an owned DCENT_Voice install: dcent-voice-offline-bundle.json");
        if (bundleInfo.Length <= 0)
            return;

        try
        {
            using var document = JsonDocument.Parse(
                File.ReadAllText(bundle),
                new JsonDocumentOptions { MaxDepth = 16 });
            var product = document.RootElement.GetProperty("product").GetString();
            if (!String.Equals(product, "DCENT_Voice", StringComparison.Ordinal))
                throw new InvalidDataException("unexpected product identity");
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException or KeyNotFoundException)
        {
            throw new InvalidOperationException(
                "Existing destination is not a registered DCENT_Voice payload (manifest identity).",
                error);
        }
    }

    private static string RequiredPlainFile(string root, string relative)
    {
        var path = Path.Combine(root, relative);
        var info = new FileInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0 || info.Length <= 0)
            throw new InvalidOperationException(
                "Existing non-empty destination is not an owned DCENT_Voice install: " + relative);
        return info.FullName;
    }

    private static void AssertExistingAncestorsArePlain(string destination)
    {
        DirectoryInfo? current = new DirectoryInfo(destination);
        while (current is not null)
        {
            if (current.Exists)
                AssertPlainDirectory(current.FullName, "install path ancestor");
            current = current.Parent;
        }
    }

    private static void AssertPlainDirectory(string path, string label)
    {
        var info = new DirectoryInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException(label + " is missing or a reparse point: " + path);
    }

    private static IEnumerable<string> ProtectedDirectories()
    {
        var values = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        void Add(string? path)
        {
            if (!String.IsNullOrWhiteSpace(path)) values.Add(Canonical(path));
        }

        Add(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.Windows));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.System));
        Add(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData));
        return values;
    }

    private static bool IsBelow(string candidate, string parent)
    {
        var prefix = Path.TrimEndingDirectorySeparator(parent) + Path.DirectorySeparatorChar;
        return candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    private static bool Same(string left, string right) =>
        String.Equals(
            Path.TrimEndingDirectorySeparator(Canonical(left)),
            Path.TrimEndingDirectorySeparator(Canonical(right)),
            StringComparison.OrdinalIgnoreCase);

    private static string Canonical(string path) =>
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
}
