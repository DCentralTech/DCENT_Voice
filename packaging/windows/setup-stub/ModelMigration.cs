// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

internal static class ModelMigration
{
    private const string RegistryName = "dcent-voice-models.json";
    private const int MaxModels = 64;
    private const int MaxFilesPerModel = 32;
    private const long MaxFileBytes = 32L * 1024 * 1024 * 1024;
    private static readonly HashSet<string> AllowedModelFiles = new(StringComparer.Ordinal)
    {
        "added_tokens.json",
        "config.json",
        "generation_config.json",
        "merges.txt",
        "model.bin",
        "normalizer.json",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "vocabulary.json",
        "vocabulary.txt",
    };
    private static readonly HashSet<string> AllowedParakeetFiles = new(StringComparer.Ordinal)
    {
        "config.json",
        "decoder_joint-model.int8.onnx",
        "encoder-model.int8.onnx",
        "vocab.txt",
    };

    private sealed record RegistryModel(
        string Provider,
        string ModelId,
        string RelativePath,
        string InstalledAt,
        IReadOnlyDictionary<string, FileDigest> Files);

    private sealed record FileDigest(long Size, string Sha256);

    internal static bool MigrateLegacyModels(string legacyRoot, string durableRoot) =>
        MigrateLegacyModels(
            legacyRoot, durableRoot, afterExistingMoved: null,
            retireLegacyRoot: false, beforeRetireCleanup: null);

    internal static bool MigrateAndRetireLegacyRoot(string legacyRoot, string durableRoot) =>
        MigrateLegacyModels(
            legacyRoot, durableRoot, afterExistingMoved: null,
            retireLegacyRoot: true, beforeRetireCleanup: null);

    internal static bool MigrateAndRetireLegacyRoot(
        string legacyRoot,
        string durableRoot,
        Action<string>? beforeRetireCleanup) =>
        MigrateLegacyModels(
            legacyRoot, durableRoot, afterExistingMoved: null,
            retireLegacyRoot: true, beforeRetireCleanup);

    internal static bool MigrateLegacyModels(
        string legacyRoot,
        string durableRoot,
        Action<string>? afterExistingMoved) =>
        MigrateLegacyModels(
            legacyRoot, durableRoot, afterExistingMoved,
            retireLegacyRoot: false, beforeRetireCleanup: null);

    private static bool MigrateLegacyModels(
        string legacyRoot,
        string durableRoot,
        Action<string>? afterExistingMoved,
        bool retireLegacyRoot,
        Action<string>? beforeRetireCleanup)
    {
        var destinationRoot = Canonical(durableRoot);
        var mutexHash = Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(destinationRoot.ToLowerInvariant()))).ToLowerInvariant();
        using var mutex = new System.Threading.Mutex(
            initiallyOwned: false, "Local\\DCENT_Voice_ModelMigration_" + mutexHash);
        var held = false;
        try
        {
            try { held = mutex.WaitOne(TimeSpan.FromSeconds(30)); }
            catch (System.Threading.AbandonedMutexException) { held = true; }
            if (!held)
                throw new TimeoutException("Timed out waiting for another model migration.");
            return MigrateLegacyModelsLocked(
                legacyRoot, destinationRoot, afterExistingMoved,
                retireLegacyRoot, beforeRetireCleanup);
        }
        finally
        {
            if (held) mutex.ReleaseMutex();
        }
    }

    private static bool MigrateLegacyModelsLocked(
        string legacyRoot,
        string durableRoot,
        Action<string>? afterExistingMoved,
        bool retireLegacyRoot,
        Action<string>? beforeRetireCleanup)
    {
        var sourceRoot = Canonical(legacyRoot);
        var destinationRoot = Canonical(durableRoot);
        if (Same(sourceRoot, destinationRoot))
            throw new InvalidOperationException("Legacy and durable model roots must be distinct.");
        if (File.Exists(destinationRoot))
            throw new InvalidOperationException("Durable model root is an existing file.");
        if (!Directory.Exists(sourceRoot))
            return false;

        AssertPlainDirectory(sourceRoot, "legacy model root");
        var registryPath = Path.Combine(sourceRoot, RegistryName);
        if (!File.Exists(registryPath))
        {
            if (!retireLegacyRoot || !Directory.Exists(destinationRoot))
                return false;
            AssertPlainDirectory(destinationRoot, "durable model root");
            var durableRegistry = Path.Combine(destinationRoot, RegistryName);
            AssertPlainFile(durableRegistry, "durable model registry", maxBytes: 1024 * 1024);
            var recoveryRecords = ReadRegistry(durableRegistry, destinationRoot);
            if (recoveryRecords.Count == 0)
                throw new InvalidDataException(
                    "Durable model registry is empty; missing legacy registry cannot be recovered.");
            ValidateClosedWorldLegacyRoot(destinationRoot, recoveryRecords);
            ValidateClosedWorldLegacyRoot(
                sourceRoot, recoveryRecords, allowMissingRegistry: true);
            RetireLegacyRoot(
                sourceRoot, destinationRoot, recoveryRecords, beforeRetireCleanup,
                allowMissingRegistry: true);
            return true;
        }
        AssertPlainFile(registryPath, "legacy model registry", maxBytes: 1024 * 1024);
        var records = ReadRegistry(registryPath, sourceRoot);
        if (records.Count == 0)
            return false;
        if (retireLegacyRoot)
            ValidateClosedWorldLegacyRoot(sourceRoot, records);

        var durableRecords = new List<RegistryModel>();
        if (Directory.Exists(destinationRoot))
        {
            AssertPlainDirectory(destinationRoot, "durable model root");
            var durableRegistry = Path.Combine(destinationRoot, RegistryName);
            if (!File.Exists(durableRegistry))
                throw new InvalidDataException(
                    "Existing durable model root has no verified registry; refusing legacy model loss.");
            AssertPlainFile(durableRegistry, "durable model registry", maxBytes: 1024 * 1024);
            durableRecords = ReadRegistry(durableRegistry, destinationRoot);
            if (durableRecords.Count == 0)
                throw new InvalidDataException(
                    "Existing durable model registry is empty; refusing legacy model loss.");
        }

        var merged = durableRecords.ToDictionary(record => record.ModelId, StringComparer.OrdinalIgnoreCase);
        foreach (var record in records)
        {
            if (merged.TryGetValue(record.ModelId, out var existing))
            {
                if (!SameInventory(existing.Files, record.Files))
                    throw new InvalidDataException(
                        "Legacy and durable model snapshots conflict: " + record.ModelId);
                continue;
            }
            merged.Add(record.ModelId, record);
        }
        var mergedRecords = merged.Values.OrderBy(record => record.ModelId, StringComparer.Ordinal).ToArray();

        var parent = Directory.GetParent(destinationRoot)?.FullName
            ?? throw new InvalidOperationException("Durable model root has no parent directory.");
        Directory.CreateDirectory(parent);
        AssertPlainDirectory(parent, "durable model parent");
        var stage = Path.Combine(parent, ".DCENT_Voice.Models.migrate-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(stage);
        try
        {
            var durableIds = durableRecords.Select(record => record.ModelId)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
            foreach (var record in mergedRecords)
                CopyModel(durableIds.Contains(record.ModelId) ? destinationRoot : sourceRoot, stage, record);
            WriteRegistry(stage, mergedRecords);
            PublishMergedRoot(
                stage,
                destinationRoot,
                Directory.Exists(destinationRoot),
                mergedRecords,
                afterExistingMoved);
            if (retireLegacyRoot)
                RetireLegacyRoot(
                    sourceRoot, destinationRoot, records, beforeRetireCleanup,
                    allowMissingRegistry: false);
            return true;
        }
        finally
        {
            if (Directory.Exists(stage)) Directory.Delete(stage, recursive: true);
        }
    }

    private static bool SameInventory(
        IReadOnlyDictionary<string, FileDigest> left,
        IReadOnlyDictionary<string, FileDigest> right) =>
        left.Count == right.Count && left.All(pair =>
            right.TryGetValue(pair.Key, out var value) && value == pair.Value);

    private static void PublishMergedRoot(
        string stage,
        string destination,
        bool replaceExisting,
        IReadOnlyList<RegistryModel> expectedRecords,
        Action<string>? afterExistingMoved)
    {
        var backup = destination + ".previous-" + Guid.NewGuid().ToString("N");
        var movedExisting = false;
        var committed = false;
        var verified = false;
        try
        {
            if (replaceExisting)
            {
                if (!Directory.Exists(destination))
                    throw new InvalidOperationException("Durable model root disappeared during migration.");
                AssertPlainDirectory(destination, "durable model root");
                Directory.Move(destination, backup);
                movedExisting = true;
                afterExistingMoved?.Invoke(destination);
            }
            else if (Directory.Exists(destination) || File.Exists(destination))
            {
                throw new InvalidOperationException("Durable model root appeared during migration.");
            }
            Directory.Move(stage, destination);
            committed = true;
            var published = ReadRegistry(Path.Combine(destination, RegistryName), destination);
            if (published.Count != expectedRecords.Count || expectedRecords.Any(expected =>
                !published.Any(actual =>
                    actual.ModelId.Equals(expected.ModelId, StringComparison.OrdinalIgnoreCase) &&
                    SameInventory(actual.Files, expected.Files))))
                throw new InvalidDataException("Published durable model registry failed verification.");
            verified = true;
        }
        catch
        {
            if (movedExisting && !committed && !Directory.Exists(destination) && Directory.Exists(backup))
                Directory.Move(backup, destination);
            if (movedExisting && Directory.Exists(backup))
                throw new InvalidOperationException(
                    "Durable model publication was interrupted; the prior verified tree is retained at " +
                    backup + ".");
            throw;
        }
        finally
        {
            if (verified && Directory.Exists(backup)) Directory.Delete(backup, recursive: true);
        }
    }

    private static List<RegistryModel> ReadRegistry(string path, string sourceRoot)
    {
        using var document = JsonDocument.Parse(
            File.ReadAllText(path, Encoding.UTF8),
            new JsonDocumentOptions { MaxDepth = 16 });
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object ||
            !root.TryGetProperty("version", out var version) ||
            version.ValueKind != JsonValueKind.Number ||
            version.GetInt32() is < 1 or > 2 ||
            !root.TryGetProperty("models", out var models) ||
            models.ValueKind != JsonValueKind.Array)
            throw new InvalidDataException("Legacy model registry schema is invalid.");
        if (models.GetArrayLength() > MaxModels)
            throw new InvalidDataException("Legacy model registry has too many entries.");

        var result = new List<RegistryModel>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var item in models.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
                throw new InvalidDataException("Legacy model registry entry is invalid.");
            var provider = RequiredString(item, "provider", 32);
            var modelId = RequiredString(item, "model_id", 256);
            var relative = RequiredString(item, "path", 512).Replace('/', Path.DirectorySeparatorChar);
            var installedAt = OptionalString(item, "installed_at", 128);
            if (provider == "parakeet" &&
                modelId == "istupakov/parakeet-tdt-0.6b-v3-onnx" &&
                relative == "parakeet-tdt-0.6b-v3")
            {
                if (!seen.Add(modelId))
                    throw new InvalidDataException(
                        "Legacy model registry contains a duplicate model ID.");
                var parakeetSource = Path.Combine(sourceRoot, relative);
                AssertPlainDirectory(parakeetSource, "legacy Parakeet snapshot");
                var parakeetInventory = Inventory(parakeetSource, modelId, provider);
                result.Add(new RegistryModel(
                    provider, modelId, relative, installedAt, parakeetInventory));
                continue;
            }
            if (provider != "faster-whisper")
                throw new InvalidDataException("Unsupported legacy model provider: " + provider);
            var safeName = SafeModelDirectoryName(modelId);
            var expectedRelative = Path.Combine("faster-whisper", safeName);
            if (!String.Equals(relative, expectedRelative, StringComparison.Ordinal))
                throw new InvalidDataException("Legacy model registry path is not canonical: " + relative);
            if (!seen.Add(modelId))
                throw new InvalidDataException("Legacy model registry contains a duplicate model ID.");
            var source = Path.Combine(sourceRoot, expectedRelative);
            AssertPlainDirectory(source, "legacy model snapshot");
            var inventory = Inventory(source, modelId, provider);
            result.Add(new RegistryModel(provider, modelId, expectedRelative, installedAt, inventory));
        }
        return result;
    }

    private static IReadOnlyDictionary<string, FileDigest> Inventory(
        string directory,
        string modelId,
        string provider)
    {
        var entries = new DirectoryInfo(directory).EnumerateFileSystemInfos().ToArray();
        if (entries.Length is < 2 or > MaxFilesPerModel)
            throw new InvalidDataException("Legacy model snapshot has an invalid entry count: " + modelId);
        var names = entries.Select(entry => entry.Name).ToHashSet(StringComparer.Ordinal);
        var allowed = provider == "parakeet" ? AllowedParakeetFiles : AllowedModelFiles;
        var required = provider == "parakeet"
            ? AllowedParakeetFiles
            : new HashSet<string>(StringComparer.Ordinal) { "config.json", "model.bin" };
        if (!required.IsSubsetOf(names))
            throw new InvalidDataException("Legacy model snapshot is incomplete: " + modelId);
        var result = new SortedDictionary<string, FileDigest>(StringComparer.Ordinal);
        foreach (var entry in entries)
        {
            if (entry is not FileInfo file ||
                (file.Attributes & FileAttributes.ReparsePoint) != 0 ||
                !allowed.Contains(file.Name) ||
                file.Length <= 0 || file.Length > MaxFileBytes)
                throw new InvalidDataException(
                    "Legacy model snapshot contains an undeclared or unsafe entry: " + entry.Name);
            using var input = new FileStream(
                file.FullName, FileMode.Open, FileAccess.Read, FileShare.Read,
                bufferSize: 1024 * 1024, FileOptions.SequentialScan);
            result.Add(file.Name, new FileDigest(file.Length, Sha256(input)));
        }

        using var config = JsonDocument.Parse(File.ReadAllText(Path.Combine(directory, "config.json")));
        if (config.RootElement.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("Legacy model config is not a JSON object: " + modelId);
        VerifyPinnedManifest(modelId, result);
        return result;
    }

    private static void VerifyPinnedManifest(
        string modelId,
        IReadOnlyDictionary<string, FileDigest> files)
    {
        Dictionary<string, FileDigest>? expected = null;
        if (modelId == "Systran/faster-whisper-base")
        {
            expected = new(StringComparer.Ordinal)
            {
                ["config.json"] = new(2309, "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),
                ["model.bin"] = new(145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
                ["tokenizer.json"] = new(2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
                ["vocabulary.txt"] = new(459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
            };
        }
        else if (modelId == "istupakov/parakeet-tdt-0.6b-v3-onnx")
        {
            expected = new(StringComparer.Ordinal)
            {
                ["config.json"] = new(97, "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
                ["decoder_joint-model.int8.onnx"] = new(18202004, "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70"),
                ["encoder-model.int8.onnx"] = new(652183999, "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09"),
                ["vocab.txt"] = new(93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
            };
        }
        if (expected is null) return;
        if (files.Count != expected.Count || expected.Any(pair =>
            !files.TryGetValue(pair.Key, out var actual) || actual != pair.Value))
            throw new InvalidDataException("Pinned legacy model snapshot failed its manifest: " + modelId);
    }

    private static void ValidateClosedWorldLegacyRoot(
        string sourceRoot,
        IReadOnlyList<RegistryModel> records,
        bool allowMissingRegistry = false)
    {
        AssertPlainDirectory(sourceRoot, "legacy model root");
        var registryPath = Path.Combine(sourceRoot, RegistryName);
        var registryExists = File.Exists(registryPath);
        if (!registryExists && !allowMissingRegistry)
            throw new InvalidDataException("Legacy model registry disappeared during migration.");
        var expectedTop = new HashSet<string>(StringComparer.Ordinal);
        if (registryExists) expectedTop.Add(RegistryName);
        if (records.Any(record => record.Provider == "faster-whisper"))
            expectedTop.Add("faster-whisper");
        if (records.Any(record => record.Provider == "parakeet"))
            expectedTop.Add("parakeet-tdt-0.6b-v3");
        var entries = new DirectoryInfo(sourceRoot).EnumerateFileSystemInfos().ToArray();
        if (!entries.Select(entry => entry.Name).ToHashSet(StringComparer.Ordinal)
                .SetEquals(expectedTop) ||
            entries.Any(entry => (entry.Attributes & FileAttributes.ReparsePoint) != 0))
            throw new InvalidDataException(
                "Legacy models-only root contains undeclared or unsafe entries.");

        var fasterRecords = records.Where(record => record.Provider == "faster-whisper").ToArray();
        if (fasterRecords.Length != 0)
        {
            var fasterRoot = Path.Combine(sourceRoot, "faster-whisper");
            AssertPlainDirectory(fasterRoot, "legacy Faster Whisper root");
            var expectedDirectories = fasterRecords
                .Select(record => Path.GetFileName(record.RelativePath))
                .ToHashSet(StringComparer.Ordinal);
            var modelDirectories = new DirectoryInfo(fasterRoot).EnumerateFileSystemInfos().ToArray();
            if (!modelDirectories.Select(entry => entry.Name).ToHashSet(StringComparer.Ordinal)
                    .SetEquals(expectedDirectories) ||
                modelDirectories.Any(entry =>
                    entry is not DirectoryInfo ||
                    (entry.Attributes & FileAttributes.ReparsePoint) != 0))
                throw new InvalidDataException(
                    "Legacy Faster Whisper root contains undeclared or unsafe entries.");
        }

        var refreshed = registryExists
            ? ReadRegistry(registryPath, sourceRoot)
            : records.Select(record =>
            {
                var path = Path.Combine(sourceRoot, record.RelativePath);
                AssertPlainDirectory(path, "legacy model snapshot");
                return record with
                {
                    Files = Inventory(path, record.ModelId, record.Provider),
                };
            }).ToList();
        if (refreshed.Count != records.Count || records.Any(expected =>
            !refreshed.Any(actual =>
                actual.Provider == expected.Provider &&
                actual.ModelId.Equals(expected.ModelId, StringComparison.OrdinalIgnoreCase) &&
                actual.RelativePath.Equals(expected.RelativePath, StringComparison.Ordinal) &&
                SameInventory(actual.Files, expected.Files))))
            throw new InvalidDataException("Legacy model root changed during migration.");
    }

    private static void RetireLegacyRoot(
        string sourceRoot,
        string durableRoot,
        IReadOnlyList<RegistryModel> records,
        Action<string>? beforeRetireCleanup,
        bool allowMissingRegistry)
    {
        ValidateClosedWorldLegacyRoot(sourceRoot, records, allowMissingRegistry);
        ValidateDurableRootContains(durableRoot, records);
        var legacyParent = Directory.GetParent(sourceRoot)?.FullName
            ?? throw new InvalidOperationException("Legacy model root has no application-data parent.");
        AssertPlainDirectory(legacyParent, "legacy application-data root");
        var outerParent = Directory.GetParent(legacyParent)?.FullName
            ?? throw new InvalidOperationException("Legacy application-data root has no parent.");
        var quarantine = Path.Combine(
            outerParent,
            ".DCENT_Voice.models-migrated-" + Guid.NewGuid().ToString("N"));
        Directory.Move(sourceRoot, quarantine);
        try
        {
            if (Directory.EnumerateFileSystemEntries(legacyParent).Any())
                throw new InvalidDataException(
                    "Legacy application-data root gained an undeclared entry during migration.");
            Directory.Delete(legacyParent, recursive: false);
        }
        catch
        {
            // No object inside the quarantine has been altered yet. A failure
            // before the empty parent is retired therefore restores the exact
            // original tree, including its registry and ReadOnly attributes.
            if (Directory.Exists(quarantine))
            {
                Directory.CreateDirectory(legacyParent);
                if (!Directory.Exists(sourceRoot) && !File.Exists(sourceRoot))
                    Directory.Move(quarantine, sourceRoot);
            }
            throw;
        }

        // The verified durable tree is authoritative after the atomic source
        // rename and empty-parent removal. Cleanup is exact and bounded; a
        // sharing violation leaves a recovery quarantine but must not roll a
        // partially deleted directory back or block installation.
        try
        {
            beforeRetireCleanup?.Invoke(quarantine);
            DeleteVerifiedQuarantine(quarantine, records, allowMissingRegistry);
        }
        catch (Exception error) when (
            error is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            Console.Error.WriteLine(
                "DCENT_Voice Setup warning: verified legacy model cleanup was deferred: " +
                error.Message);
        }
    }

    private static void ValidateDurableRootContains(
        string durableRoot,
        IReadOnlyList<RegistryModel> requiredRecords)
    {
        AssertPlainDirectory(durableRoot, "durable model root");
        var registry = Path.Combine(durableRoot, RegistryName);
        AssertPlainFile(registry, "durable model registry", maxBytes: 1024 * 1024);
        var durableRecords = ReadRegistry(registry, durableRoot);
        if (durableRecords.Count == 0)
            throw new InvalidDataException("Durable model registry is empty after migration.");
        ValidateClosedWorldLegacyRoot(durableRoot, durableRecords);
        if (requiredRecords.Any(required => !durableRecords.Any(actual =>
            actual.Provider == required.Provider &&
            actual.ModelId.Equals(required.ModelId, StringComparison.OrdinalIgnoreCase) &&
            actual.RelativePath.Equals(required.RelativePath, StringComparison.Ordinal) &&
            SameInventory(actual.Files, required.Files))))
            throw new InvalidDataException(
                "Durable model root does not contain every verified legacy model.");
    }

    private static void DeleteVerifiedQuarantine(
        string quarantine,
        IReadOnlyList<RegistryModel> records,
        bool allowMissingRegistry)
    {
        ValidateClosedWorldLegacyRoot(quarantine, records, allowMissingRegistry);
        var files = records
            .SelectMany(record => record.Files.Keys.Select(name =>
                Path.Combine(quarantine, record.RelativePath, name)))
            .ToList();
        var registry = Path.Combine(quarantine, RegistryName);
        if (File.Exists(registry)) files.Add(registry);
        files = files.OrderBy(path => path, StringComparer.Ordinal).ToList();

        foreach (var path in files)
        {
            AssertPlainFile(path, "verified retired model asset", MaxFileBytes);
            var attributes = File.GetAttributes(path);
            File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
        }
        ValidateClosedWorldLegacyRoot(quarantine, records, allowMissingRegistry);

        // Acquire every exact file exclusively before the first deletion. This
        // catches live readers and ordinary replacement races while the full
        // quarantine is still intact.
        var preflight = new List<FileStream>();
        try
        {
            foreach (var path in files)
                preflight.Add(new FileStream(
                    path, FileMode.Open, FileAccess.ReadWrite, FileShare.None,
                    bufferSize: 1, FileOptions.None));
        }
        finally
        {
            foreach (var stream in preflight) stream.Dispose();
        }

        foreach (var path in files) DeleteFileWithRetry(path);

        var directories = records
            .Select(record => Path.Combine(quarantine, record.RelativePath))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderByDescending(path => path.Count(character =>
                character == Path.DirectorySeparatorChar))
            .ToList();
        var fasterRoot = Path.Combine(quarantine, "faster-whisper");
        if (Directory.Exists(fasterRoot)) directories.Add(fasterRoot);
        directories.Add(quarantine);
        foreach (var path in directories.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            AssertPlainDirectory(path, "verified retired model directory");
            var attributes = File.GetAttributes(path);
            File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
            DeleteDirectoryWithRetry(path);
        }
    }

    private static void DeleteFileWithRetry(string path)
    {
        Exception? failure = null;
        for (var attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                File.Delete(path);
                if (!File.Exists(path)) return;
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                failure = error;
            }
            System.Threading.Thread.Sleep(100);
        }
        throw new IOException("Verified retired model file remains: " + Path.GetFileName(path), failure);
    }

    private static void DeleteDirectoryWithRetry(string path)
    {
        Exception? failure = null;
        for (var attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                Directory.Delete(path, recursive: false);
                if (!Directory.Exists(path)) return;
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                failure = error;
            }
            System.Threading.Thread.Sleep(100);
        }
        throw new IOException(
            "Verified retired model directory remains: " + Path.GetFileName(path), failure);
    }

    private static void CopyModel(string sourceRoot, string stageRoot, RegistryModel record)
    {
        var source = Path.Combine(sourceRoot, record.RelativePath);
        var destination = Path.Combine(stageRoot, record.RelativePath);
        Directory.CreateDirectory(destination);
        AssertPlainDirectory(source, "legacy model snapshot");
        var openFiles = new List<(string Name, FileStream Stream)>();
        try
        {
            foreach (var name in record.Files.Keys)
            {
                var path = Path.Combine(source, name);
                AssertPlainFile(path, "legacy model asset", MaxFileBytes);
                openFiles.Add((name, new FileStream(
                    path, FileMode.Open, FileAccess.Read, FileShare.Read,
                    bufferSize: 1024 * 1024, FileOptions.SequentialScan)));
            }
            var currentNames = Directory.EnumerateFileSystemEntries(source)
                .Select(Path.GetFileName).OrderBy(name => name, StringComparer.Ordinal).ToArray();
            if (!currentNames.SequenceEqual(record.Files.Keys, StringComparer.Ordinal))
                throw new InvalidDataException("Legacy model snapshot changed before migration.");

            foreach (var (name, input) in openFiles)
            {
                var expected = record.Files[name];
                var target = Path.Combine(destination, name);
                using var output = new FileStream(
                    target, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                    bufferSize: 1024 * 1024, FileOptions.SequentialScan);
                input.Position = 0;
                using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
                var buffer = new byte[1024 * 1024];
                long copied = 0;
                int count;
                while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    output.Write(buffer, 0, count);
                    digest.AppendData(buffer, 0, count);
                    copied += count;
                }
                output.Flush(flushToDisk: true);
                var actualHash = Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
                if (copied != expected.Size ||
                    !CryptographicOperations.FixedTimeEquals(
                        Convert.FromHexString(actualHash), Convert.FromHexString(expected.Sha256)))
                    throw new InvalidDataException("Legacy model asset changed during migration: " + name);
            }
        }
        finally
        {
            foreach (var item in openFiles) item.Stream.Dispose();
        }
        var copiedInventory = Inventory(destination, record.ModelId, record.Provider);
        if (copiedInventory.Count != record.Files.Count || record.Files.Any(pair =>
            !copiedInventory.TryGetValue(pair.Key, out var actual) || actual != pair.Value))
            throw new InvalidDataException("Migrated model snapshot failed verification: " + record.ModelId);
    }

    private static void WriteRegistry(string stage, IReadOnlyList<RegistryModel> records)
    {
        var models = records.Select(record => new
        {
            provider = record.Provider,
            model_id = record.ModelId,
            path = record.RelativePath.Replace(Path.DirectorySeparatorChar, '/'),
            installed_at = record.InstalledAt,
            files = record.Files.ToDictionary(
                pair => pair.Key,
                pair => new { size = pair.Value.Size, sha256 = pair.Value.Sha256 },
                StringComparer.Ordinal),
        }).ToArray();
        var payload = JsonSerializer.Serialize(
            new { version = 2, models },
            new JsonSerializerOptions { WriteIndented = true }) + Environment.NewLine;
        var path = Path.Combine(stage, RegistryName);
        using var output = new FileStream(
            path, FileMode.CreateNew, FileAccess.Write, FileShare.None,
            bufferSize: 4096, FileOptions.WriteThrough);
        var bytes = new UTF8Encoding(false).GetBytes(payload);
        output.Write(bytes, 0, bytes.Length);
        output.Flush(flushToDisk: true);
    }

    private static string RequiredString(JsonElement item, string name, int maxLength)
    {
        if (!item.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("Legacy model registry is missing " + name + ".");
        var text = value.GetString() ?? "";
        if (String.IsNullOrWhiteSpace(text) || text.Length > maxLength ||
            text.Any(character => Char.IsControl(character)))
            throw new InvalidDataException("Legacy model registry has an invalid " + name + ".");
        return text;
    }

    private static string OptionalString(JsonElement item, string name, int maxLength)
    {
        if (!item.TryGetProperty(name, out var value)) return "";
        if (value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("Legacy model registry has an invalid " + name + ".");
        var text = value.GetString() ?? "";
        if (text.Length > maxLength || text.Any(character => Char.IsControl(character)))
            throw new InvalidDataException("Legacy model registry has an invalid " + name + ".");
        return text;
    }

    private static string SafeModelDirectoryName(string modelId)
    {
        if (modelId.StartsWith('/') || modelId.Contains('\\') || modelId.Contains(':'))
            throw new InvalidDataException("Legacy model ID is path-like.");
        var parts = modelId.Split('/');
        if (parts.Length < 2 || parts.Any(part =>
            String.IsNullOrWhiteSpace(part) || part is "." or ".." ||
            part.Any(character => Char.IsControl(character))))
            throw new InvalidDataException("Legacy model ID is invalid.");
        return String.Join("--", parts);
    }

    private static void AssertPlainDirectory(string path, string label)
    {
        var info = new DirectoryInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException(label + " is missing or a reparse point: " + path);
    }

    private static void AssertPlainFile(string path, string label, long maxBytes)
    {
        var info = new FileInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0 ||
            info.Length <= 0 || info.Length > maxBytes)
            throw new InvalidDataException(label + " is missing or unsafe: " + path);
    }

    private static string Sha256(Stream stream) =>
        Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();

    private static string Canonical(string path) =>
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));

    private static bool Same(string left, string right) =>
        String.Equals(left, right, StringComparison.OrdinalIgnoreCase);
}
