// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class Program
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("DCENTSFX");
    private const string Product = "DCENT_Voice";
    private static readonly string Version =
        Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
        ?? throw new InvalidOperationException("Setup assembly has no release version metadata.");

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            EnsureSupportedWindowsHost();
            if (HasFlag(args, "--verify-sfx"))
            {
                var verifySelf = Environment.ProcessPath ??
                    Process.GetCurrentProcess().MainModule!.FileName;
                var verifyZip = Path.Combine(
                    Path.GetTempPath(),
                    "dcent-voice-setup-verify-" + Guid.NewGuid().ToString("N") + ".zip");
                try { ExtractPayload(verifySelf, verifyZip); }
                finally { File.Delete(verifyZip); }
                return 0;
            }
            var customDest = DestFromArgs(args);
            var defaultDest = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                Product);
            var dest = customDest ?? defaultDest;
            var validateRequested = HasOption(args, "--validate-payload");
            var silent = HasFlag(args, "/S") || HasFlag(args, "/silent") ||
                HasFlag(args, "--silent") || validateRequested;
            var registerUninstall = customDest is null;
            if (customDest is not null &&
                Path.TrimEndingDirectorySeparator(Path.GetFullPath(customDest)).Equals(
                    Path.TrimEndingDirectorySeparator(Path.GetFullPath(defaultDest)),
                    StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException(
                    "Do not use /D for the standard per-user install directory; omit /D instead.");
            if (HasFlag(args, "/?") || HasFlag(args, "--help") || HasFlag(args, "-h"))
            {
                Show(
                    silent,
                    Product + " Setup\n\n" +
                    "  (no args)   install for this user\n" +
                    "  /S          silent install\n" +
                    "  /D=path     install directory (skips Add/Remove Programs)\n" +
                    "  /uninstall  remove this user install\n" +
                    "  /purge-user-data  with /uninstall, also remove user data and credentials\n" +
                    "  --verify-sfx verify the embedded archive without installing",
                    MessageBoxIcon.Information);
                return 0;
            }
            if (validateRequested)
            {
                var validateRoot = RequiredArgValue(args, "--validate-payload");
                ValidatePayload(Path.GetFullPath(validateRoot));
                return 0;
            }
            if (HasFlag(args, "/uninstall"))
            {
                var purge = HasFlag(args, "/purge-user-data") || HasFlag(args, "--purge-user-data");
                if (!registerUninstall && purge)
                    throw new InvalidOperationException(
                        "A custom /D install can remove only its own payload; purge the registered user install explicitly.");
                return Uninstall(dest, silent, purge, registerUninstall);
            }

            var legacyModelsOnly = registerUninstall &&
                InstallDestination.IsLegacyModelsOnlyRoot(dest);
            if (!legacyModelsOnly)
                dest = InstallDestination.ValidateForInstall(dest);

            var selfPath = Environment.ProcessPath ?? Process.GetCurrentProcess().MainModule!.FileName;
            var tmpZip = Path.Combine(Path.GetTempPath(), "dcent-voice-setup-" + Guid.NewGuid().ToString("N") + ".zip");
            var stage = dest + ".install-" + Guid.NewGuid().ToString("N");
            string? backup = null;
            InstallRegistrationSnapshot? registrationSnapshot = null;
            try
            {
                ExtractPayload(selfPath, tmpZip);
                Directory.CreateDirectory(stage);
                ZipFile.ExtractToDirectory(tmpZip, stage, overwriteFiles: false);
                ValidatePayload(stage);
                if (registerUninstall)
                {
                    RecoveryCoordinator.ReconcilePendingUninstall(dest);
                    registrationSnapshot = InstallRegistrationSnapshot.Capture(
                        Path.Combine(
                            Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                            Product));
                }
                // Revalidate after extraction/recovery so an unrelated directory
                // cannot be swapped into an earlier-approved empty path. The
                // sole compatibility exception is the exact historical
                // LocalAppData root containing only its model registry tree.
                legacyModelsOnly = registerUninstall &&
                    InstallDestination.IsLegacyModelsOnlyRoot(dest);
                if (!legacyModelsOnly)
                    dest = InstallDestination.ValidateForInstall(dest);
                var localData = Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData);
                if (Directory.Exists(dest))
                {
                    StopProcessesBelowRoot(dest);
                    legacyModelsOnly = registerUninstall &&
                        InstallDestination.IsLegacyModelsOnlyRoot(dest);
                    if (!legacyModelsOnly)
                        dest = InstallDestination.ValidateForInstall(dest);
                }
                var legacyModels = Path.Combine(localData, Product, "models");
                var durableModels = Path.Combine(localData, "DCENT_Voice.Models");
                if (legacyModelsOnly)
                    ModelMigration.MigrateAndRetireLegacyRoot(legacyModels, durableModels);
                else
                    ModelMigration.MigrateLegacyModels(legacyModels, durableModels);
                dest = InstallDestination.ValidateForInstall(dest);
                if (Directory.Exists(dest))
                {
                    backup = dest + ".previous-" + Guid.NewGuid().ToString("N");
                    Directory.Move(dest, backup);
                }
                try
                {
                    Directory.Move(stage, dest);
                }
                catch
                {
                    if (!Directory.Exists(dest) && backup is not null && Directory.Exists(backup))
                        Directory.Move(backup, dest);
                    throw;
                }
            }
            finally
            {
                File.Delete(tmpZip);
                if (Directory.Exists(stage)) Directory.Delete(stage, true);
            }

            var exe = Path.Combine(dest, "dcent-voice.exe");
            try
            {
                if (!File.Exists(exe))
                {
                    throw new InvalidOperationException("Payload did not contain dcent-voice.exe");
                }

                var uninstallCmd = WriteUninstallScript(dest, registerUninstall);
                if (registerUninstall)
                {
                    var programs = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                        Product);
                    Directory.CreateDirectory(programs);
                    WriteShortcut(
                        Path.Combine(programs, Product + ".lnk"), exe, dest,
                        "", "Local-first voice dictation");
                    // The diagnostics entry is the one a stuck user can find
                    // when the app itself will not start (WS4/WS5). Its args
                    // must match dcent_voice.doctor.start_menu_shortcut_args().
                    WriteShortcut(
                        Path.Combine(programs, Product + " Diagnostics.lnk"), exe, dest,
                        "doctor --open",
                        "Diagnose why DCENT_Voice will not start and open the report");
                    WriteUninstall(dest, uninstallCmd, exe);
                }
            }
            catch (Exception installError)
            {
                var rollbackErrors = new System.Collections.Generic.List<Exception>();
                try { RollbackInstallTree(dest, backup); }
                catch (Exception error) { rollbackErrors.Add(error); }
                if (registrationSnapshot is not null)
                {
                    try { registrationSnapshot.Restore(); }
                    catch (Exception error) { rollbackErrors.Add(error); }
                }
                if (rollbackErrors.Count != 0)
                {
                    rollbackErrors.Insert(0, installError);
                    throw new AggregateException(
                        "Setup failed and its prior registration could not be fully restored.",
                        rollbackErrors);
                }
                throw;
            }
            CleanupBackupDeferred(backup);

            // The payload was hash-verified before it moved into place. What is
            // still unknown is the *host*: runtimes, permissions, devices. Report
            // the host runtimes first, then prove the install by running it.
            var hostDependencies = HostDependencies.Inspect();
            ReportHostDependencies(hostDependencies, silent);

            var check = PostInstallCheck.Run(exe, dest);
            if (!check.Passed)
            {
                // A host-dependency failure is not a bad install: keep the tree
                // and the ARP registration, skip the auto-launch, and say exactly
                // what to fix. But Setup must not *declare success* (AC5), so the
                // exit code is non-zero in both interactive and silent mode — a
                // scripted deployment has no dialog to read.
                if (silent)
                    Console.Error.Write(PostInstallCheck.SilentDiagnostic(check));
                else
                    MessageBox.Show(
                        check.ReportedFailures
                            ? PostInstallCheck.FailureMessage(check, dest)
                            : PostInstallCheck.CouldNotRunMessage(check, dest),
                        Product,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                return 3;
            }

            if (!silent)
            {
                var launch = MessageBox.Show(
                    "DCENT_Voice is installed for this user.\n\n" + dest + "\n\nLaunch now?",
                    Product,
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Information);
                if (launch == DialogResult.Yes)
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = exe,
                        WorkingDirectory = dest,
                        UseShellExecute = true,
                    });
                }
            }
            return 0;
        }
        catch (Exception ex)
        {
            try
            {
                var silentError = HasFlag(args, "/S") || HasFlag(args, "/silent") ||
                    HasFlag(args, "--silent") || HasOption(args, "--validate-payload");
                if (silentError)
                    Console.Error.WriteLine("DCENT_Voice Setup failed: " + ex.Message);
                else
                    Show(false, "DCENT_Voice Setup failed:\n\n" + ex.Message, MessageBoxIcon.Error);
            }
            catch
            {
                Console.Error.WriteLine("DCENT_Voice Setup failed: " + ex.Message);
            }
            return 1;
        }
    }

    // Windows 10 version 1809 (build 17763). The floor is not the installer's:
    // the Settings window, the overlay and the setup wizard are pywebview on
    // pythonnet, which needs .NET Framework 4.7.2 — preinstalled from 1803/1809
    // onward. Advertising 1607 shipped a build whose UI could not load. Keep
    // this constant, README.md, docs/PACKAGING.md, docs/QA_FRESH_MACHINE.md and
    // packaging/windows/dcent-voice.exe.manifest in step.
    private const int MinimumWindowsBuild = 17763;

    private static void EnsureSupportedWindowsHost()
    {
        if (!Environment.Is64BitOperatingSystem || !Environment.Is64BitProcess)
            throw new InvalidOperationException(
                "DCENT_Voice Setup requires 64-bit Windows 10 version 1809 (build 17763) or later, or Windows 11.");
        var version = Environment.OSVersion.Version;
        if (version.Major < 10 || (version.Major == 10 && version.Build < MinimumWindowsBuild))
            throw new InvalidOperationException(
                "DCENT_Voice Setup requires Windows 10 version 1809 (build 17763) or later, or Windows 11. " +
                "This computer reports build " + version.Build + ". Earlier builds do not ship the " +
                ".NET Framework 4.7.2 that the Settings window, the overlay and the setup wizard need.");
    }

    /// <summary>
    /// Tell the user what the *windows* still need. Never fatal: hold-to-talk
    /// dictation does not use WebView2 or .NET Framework. Setup does not bundle
    /// the runtime (see docs/PACKAGING.md); the only network step it can take is
    /// opening Microsoft's download page, and only after the user clicks Yes.
    /// </summary>
    private static void ReportHostDependencies(HostDependencies.Report report, bool silent)
    {
        if (report.AllSatisfied)
            return;
        var missing = HostDependencies.MissingSummary(report);
        if (silent)
        {
            Console.Error.WriteLine(
                "DCENT_Voice Setup: dictation works, but Settings/overlay need a runtime this " +
                "computer does not have: " + string.Join("; ", missing) + ". Install the " +
                "Evergreen WebView2 runtime from " + HostDependencies.WebView2DownloadUrl + ".");
            return;
        }
        var text = new StringBuilder();
        text.Append(
            "Dictation works, but Settings/overlay need Microsoft Edge WebView2.\n\n");
        text.Append("Missing on this computer:\n");
        foreach (var item in missing)
            text.Append("  • ").Append(item).Append('\n');
        text.Append(
            "\nHold-to-talk dictation is unaffected and works right now. The Settings " +
            "window, the on-screen overlay and the setup wizard cannot open without these.\n\n");
        text.Append("Open the Microsoft download page in your browser now?");
        var answer = MessageBox.Show(
            text.ToString(), Product, MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
        if (answer != DialogResult.Yes)
            return;
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = HostDependencies.WebView2DownloadUrl,
                UseShellExecute = true,
            })?.Dispose();
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "The download page could not be opened (" + error.Message + ").\n\n" +
                "Open this address manually:\n" + HostDependencies.WebView2DownloadUrl,
                Product,
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }
    }

    private static void ValidatePayload(string root)
    {
        RequireRuntimeFile(Path.Combine(root, "dcent-voice.exe"), "frozen executable");
        RequireRuntimeFile(Path.Combine(root, "_internal", "base_library.zip"), "Python base library archive");
        RequireRuntimeFile(Path.Combine(root, "_internal", "python311.dll"), "Python shared runtime");
        RequireRuntimeFile(Path.Combine(root, "_internal", "vcruntime140.dll"), "VC++ runtime");
        RequireRuntimeFile(Path.Combine(root, "_internal", "vcruntime140_1.dll"), "VC++ runtime (x64 extras)");
        RequireRuntimeFile(Path.Combine(root, "_internal", "msvcp140.dll"), "VC++ C++ runtime");
        RequireRuntimeFile(Path.Combine(root, "_internal", "ctranslate2", "ctranslate2.dll"), "CTranslate2 library");
        RequireRuntimeFile(Path.Combine(root, "_internal", "_sounddevice_data", "portaudio-binaries", "libportaudio64bit.dll"), "PortAudio library");
        RequireRuntimeFile(Path.Combine(root, "_internal", "webview", "lib", "runtimes", "win-x64", "native", "WebView2Loader.dll"), "WebView2 loader");
        RequireRuntimeFile(Path.Combine(root, "_internal", "config.example.toml"), "bundled default configuration (_internal)");
        // The payload root copy is the one a person can find and edit; the app resolves
        // the _internal copy. Both must ship (see DCENT_Voice.spec).
        RequireRuntimeFile(Path.Combine(root, "config.example.toml"), "bundled default configuration (payload root)");
        RequireRuntimeFile(Path.Combine(root, "_internal", "LICENSE"), "DCENT_Voice license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "README.md"), "DCENT_Voice README");
        RequireRuntimeFile(Path.Combine(root, "_internal", "THIRD-PARTY-LICENSES.md"), "third-party license inventory");
        RequireRuntimeFile(Path.Combine(root, "_internal", "THIRD-PARTY-SBOM.cdx.json"), "artifact-derived CycloneDX SBOM");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "CPython-LICENSE.txt"), "CPython license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "Apache-2.0.txt"), "OpenSSL license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "SQLite-LICENSE.md"), "SQLite license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "libffi-LICENSE.txt"), "libffi license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "Microsoft-Visual-Cpp-Runtime-NOTICE.txt"), "Microsoft VC/UCRT redistribution notice");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "PortAudio-LICENSE.txt"), "PortAudio license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "dotnet", "LICENSE.txt"), ".NET runtime license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "runtime", "dotnet", "ThirdPartyNotices.txt"), ".NET runtime third-party notices");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "fonts", "OFL-1.1.txt"), "bundled font license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "fonts", "PROVENANCE.md"), "bundled font provenance");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "models", "faster-whisper-model-LICENSE.txt"), "Faster Whisper model license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "models", "CC-BY-4.0.txt"), "Parakeet model license");
        RequireRuntimeFile(Path.Combine(root, "_internal", "licenses", "models", "Parakeet-TDT-0.6B-v3-ATTRIBUTION.txt"), "Parakeet model attribution");
        RequireRuntimeFile(Path.Combine(root, "_internal", "onnx_asr", "__init__.py"), "Parakeet runtime package");
        RequireRuntimeFile(Path.Combine(root, "_internal", "onnxruntime", "capi", "onnxruntime.dll"), "ONNX Runtime library");
        RequireRuntimeFile(Path.Combine(root, "_internal", "dcent_voice", "asr", "manifests", "faster-whisper-base.json"), "Faster Whisper manifest");
        RequireRuntimeFile(Path.Combine(root, "_internal", "dcent_voice", "asr", "manifests", "parakeet-tdt-0.6b-v3.json"), "Parakeet manifest");
        RequireRuntimeFile(Path.Combine(root, "dcent-voice-offline-bundle.json"), "offline bundle manifest");
        ValidateModels(root);
    }

    private static void RequireRuntimeFile(string path, string label)
    {
        var file = new FileInfo(path);
        if (!file.Exists || (file.Attributes & FileAttributes.ReparsePoint) != 0 || file.Length <= 0)
            throw new InvalidOperationException("Runtime payload is incomplete (" + label + "): " + path);
    }

    private static void ValidateModels(string root)
    {
        ValidateModel(
            Path.Combine(root, "models", "parakeet-tdt-0.6b-v3"),
            new (string, long, string)[] {
                ("config.json", 97, "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
                ("decoder_joint-model.int8.onnx", 18202004, "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70"),
                ("encoder-model.int8.onnx", 652183999, "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09"),
                ("vocab.txt", 93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
            });
        ValidateModel(
            Path.Combine(root, "models", "faster-whisper", "Systran--faster-whisper-base"),
            new (string, long, string)[] {
                ("config.json", 2309, "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),
                ("model.bin", 145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
                ("tokenizer.json", 2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
                ("vocabulary.txt", 459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
            });
    }

    private static void ValidateModel(string directory, (string Name, long Size, string Hash)[] expected)
    {
        var dir = new DirectoryInfo(directory);
        if (!dir.Exists || (dir.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("Model directory is missing or unsafe: " + directory);
        var names = dir.EnumerateFileSystemInfos().Select(item => item.Name).OrderBy(name => name, StringComparer.Ordinal).ToArray();
        var wanted = expected.Select(item => item.Name).OrderBy(name => name, StringComparer.Ordinal).ToArray();
        if (!names.SequenceEqual(wanted, StringComparer.Ordinal))
            throw new InvalidOperationException("Model directory has missing or undeclared entries: " + directory);
        foreach (var item in expected)
        {
            var file = new FileInfo(Path.Combine(directory, item.Name));
            if (!file.Exists || (file.Attributes & FileAttributes.ReparsePoint) != 0 || file.Length != item.Size)
                throw new InvalidOperationException("Model file is missing or unsafe: " + item.Name);
            using var input = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.Read);
            var hash = Convert.ToHexString(SHA256.HashData(input)).ToLowerInvariant();
            if (!String.Equals(hash, item.Hash, StringComparison.Ordinal))
                throw new InvalidOperationException("Model checksum mismatch: " + item.Name);
        }
    }

    private static string? DestFromArgs(string[] args)
    {
        string? result = null;
        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            string? raw = null;
            if (arg.StartsWith("/D=", StringComparison.OrdinalIgnoreCase) ||
                arg.StartsWith("--dest=", StringComparison.OrdinalIgnoreCase))
            {
                raw = arg[(arg.IndexOf('=') + 1)..];
                raw = JoinPathTokens(raw, args, ref i);
            }
            else if (arg.Equals("/D", StringComparison.OrdinalIgnoreCase) ||
                     arg.Equals("--dest", StringComparison.OrdinalIgnoreCase))
            {
                if (i + 1 >= args.Length ||
                    args[i + 1].StartsWith("/", StringComparison.Ordinal) ||
                    args[i + 1].StartsWith("--", StringComparison.Ordinal))
                {
                    throw new ArgumentException(arg + " requires an install directory.");
                }
                i++;
                raw = JoinPathTokens(args[i], args, ref i);
            }
            else
            {
                continue;
            }

            if (result is not null)
                throw new ArgumentException("The install directory may only be specified once.");
            var value = raw?.Trim().Trim('"');
            if (String.IsNullOrWhiteSpace(value))
                throw new ArgumentException(arg + " requires an install directory.");
            result = Path.GetFullPath(value);
        }
        return result;
    }

    private static string JoinPathTokens(string first, string[] args, ref int index)
    {
        var parts = new System.Collections.Generic.List<string> { first.Trim().Trim('"') };
        while (index + 1 < args.Length)
        {
            var next = args[index + 1];
            if (next.StartsWith("/", StringComparison.Ordinal) ||
                next.StartsWith("--", StringComparison.Ordinal))
            {
                break;
            }
            index++;
            parts.Add(next.Trim().Trim('"'));
        }
        return string.Join(" ", parts);
    }

    private static bool HasFlag(string[] args, string flag)
    {
        foreach (var arg in args)
        {
            if (arg.Equals(flag, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static bool HasOption(string[] args, string option)
    {
        foreach (var arg in args)
        {
            if (arg.Equals(option, StringComparison.OrdinalIgnoreCase) ||
                arg.StartsWith(option + "=", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static string RequiredArgValue(string[] args, string option)
    {
        string? result = null;
        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            string? candidate = null;
            if (arg.StartsWith(option + "=", StringComparison.OrdinalIgnoreCase))
            {
                candidate = arg[(arg.IndexOf('=') + 1)..];
            }
            else if (arg.Equals(option, StringComparison.OrdinalIgnoreCase))
            {
                if (i + 1 >= args.Length ||
                    args[i + 1].StartsWith("/", StringComparison.Ordinal) ||
                    args[i + 1].StartsWith("--", StringComparison.Ordinal))
                {
                    throw new ArgumentException(option + " requires a payload directory.");
                }
                candidate = args[++i];
            }
            else
            {
                continue;
            }

            if (result is not null)
            {
                throw new ArgumentException(option + " may only be specified once.");
            }
            result = candidate?.Trim().Trim('"');
        }
        if (string.IsNullOrWhiteSpace(result))
        {
            throw new ArgumentException(option + " requires a payload directory.");
        }
        return result;
    }

    private static void Show(bool silent, string text, MessageBoxIcon icon)
    {
        if (silent)
        {
            return;
        }
        MessageBox.Show(text, Product, MessageBoxButtons.OK, icon);
    }

    private static void ExtractPayload(string selfPath, string destination)
    {
        const int hashSize = 32;
        var trailerSize = hashSize + sizeof(ulong) + Magic.Length;
        using var input = new FileStream(
            selfPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 1024 * 1024,
            FileOptions.SequentialScan);
        var payloadEnd = GetSignedContentEnd(input);
        var trailerEnd = FindTrailerEnd(input, payloadEnd);
        if (trailerEnd < trailerSize + 1)
        {
            throw new InvalidOperationException("Installer is missing its payload.");
        }
        var trailer = new byte[trailerSize];
        input.Position = trailerEnd - trailerSize;
        input.ReadExactly(trailer);
        for (var i = 0; i < Magic.Length; i++)
        {
            if (trailer[trailerSize - Magic.Length + i] != Magic[i])
            {
                throw new InvalidOperationException("Installer trailer is not a DCENT Setup payload.");
            }
        }
        var lengthOffset = hashSize;
        var zipLen = BitConverter.ToUInt64(trailer, lengthOffset);
        if (zipLen == 0 || zipLen > long.MaxValue || zipLen > (ulong)(trailerEnd - trailerSize))
        {
            throw new InvalidOperationException("Installer payload length is corrupt.");
        }
        var zipStart = trailerEnd - trailerSize - checked((long)zipLen);
        if (zipStart < 1)
        {
            throw new InvalidOperationException("Installer payload overlaps the native stub.");
        }

        input.Position = zipStart;
        using var output = new FileStream(
            destination,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            bufferSize: 1024 * 1024,
            FileOptions.SequentialScan);
        using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[1024 * 1024];
        var remaining = checked((long)zipLen);
        while (remaining > 0)
        {
            var count = input.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
            if (count <= 0)
            {
                throw new EndOfStreamException("Installer payload ended before its declared length.");
            }
            output.Write(buffer, 0, count);
            digest.AppendData(buffer, 0, count);
            remaining -= count;
        }
        output.Flush(flushToDisk: true);
        var actualHash = digest.GetHashAndReset();
        if (!CryptographicOperations.FixedTimeEquals(actualHash, trailer.AsSpan(0, hashSize)))
        {
            throw new InvalidOperationException("Installer payload checksum mismatch.");
        }
    }

    private static long GetSignedContentEnd(FileStream input)
    {
        // Authenticode appends WIN_CERTIFICATE after the SFX overlay. The PE
        // security directory uses a file offset (not an RVA), so a signed
        // Setup must read its trailer immediately before that certificate.
        using var reader = new BinaryReader(input, Encoding.UTF8, leaveOpen: true);
        if (input.Length < 64)
            throw new InvalidOperationException("Installer PE header is truncated.");
        input.Position = 0;
        if (reader.ReadUInt16() != 0x5A4D)
            throw new InvalidOperationException("Installer DOS header is invalid.");
        input.Position = 0x3c;
        var peOffset = reader.ReadUInt32();
        if (peOffset > input.Length - 24)
            throw new InvalidOperationException("Installer PE header offset is invalid.");
        input.Position = peOffset;
        if (reader.ReadUInt32() != 0x00004550)
            throw new InvalidOperationException("Installer PE signature is invalid.");
        input.Position = checked((long)peOffset + 20);
        var optionalSize = reader.ReadUInt16();
        var optionalStart = checked((long)peOffset + 24);
        if (optionalSize < 2 || optionalStart + optionalSize > input.Length)
            throw new InvalidOperationException("Installer optional header is invalid.");
        input.Position = optionalStart;
        var optionalMagic = reader.ReadUInt16();
        var dataDirectoryOffset = optionalMagic switch
        {
            0x10b => 96L,
            0x20b => 112L,
            _ => throw new InvalidOperationException("Installer PE format is unsupported."),
        };
        var numberOffset = optionalStart + dataDirectoryOffset - 4;
        if (numberOffset + 4 > optionalStart + optionalSize)
            throw new InvalidOperationException("Installer PE data directories are truncated.");
        input.Position = numberOffset;
        if (reader.ReadUInt32() <= 4)
            return input.Length;
        var securityOffset = optionalStart + dataDirectoryOffset + (4 * 8L);
        if (securityOffset + 8 > optionalStart + optionalSize)
            throw new InvalidOperationException("Installer PE security directory is truncated.");
        input.Position = securityOffset;
        var certificateOffset = reader.ReadUInt32();
        var certificateSize = reader.ReadUInt32();
        if (certificateOffset == 0 && certificateSize == 0)
            return input.Length;
        if (certificateOffset == 0 || certificateSize == 0 ||
            (ulong)certificateOffset + certificateSize != (ulong)input.Length)
            throw new InvalidOperationException("Installer Authenticode certificate range is invalid.");
        return certificateOffset;
    }

    private static long FindTrailerEnd(FileStream input, long signedContentEnd)
    {
        const int maxPadding = 7;
        var windowSize = (int)Math.Min(Magic.Length + maxPadding, signedContentEnd);
        var tail = new byte[windowSize];
        input.Position = signedContentEnd - windowSize;
        input.ReadExactly(tail);
        for (var padding = 0; padding <= maxPadding; padding++)
        {
            var magicStart = tail.Length - padding - Magic.Length;
            if (magicStart < 0) break;
            var zeroPadding = true;
            for (var index = tail.Length - padding; index < tail.Length; index++)
                zeroPadding &= tail[index] == 0;
            if (!zeroPadding) continue;
            var matches = true;
            for (var index = 0; index < Magic.Length; index++)
                matches &= tail[magicStart + index] == Magic[index];
            if (matches) return signedContentEnd - padding;
        }
        throw new InvalidOperationException("Installer trailer is not a DCENT Setup payload.");
    }

    private static void StopProcessesBelowRoot(string root)
    {
        var canonicalRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var prefix = canonicalRoot + Path.DirectorySeparatorChar;
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                string? executable;
                try { executable = process.MainModule?.FileName; }
                catch { continue; }
                if (string.IsNullOrWhiteSpace(executable)) continue;
                var full = Path.GetFullPath(executable);
                if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) continue;
                try
                {
                    var current = process.MainModule?.FileName;
                    if (current is null || !Path.GetFullPath(current).StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                        continue;
                    if (process.CloseMainWindow() && process.WaitForExit(5000)) continue;
                    process.Kill(entireProcessTree: true);
                    if (!process.WaitForExit(10000))
                        throw new TimeoutException("A running DCENT_Voice process could not be stopped for upgrade.");
                }
                catch (InvalidOperationException) { }
            }
        }
    }

    private static void RollbackInstallTree(string dest, string? backup)
    {
        var failed = dest + ".failed-" + Guid.NewGuid().ToString("N");
        if (Directory.Exists(dest)) Directory.Move(dest, failed);
        try
        {
            if (backup is not null && Directory.Exists(backup)) Directory.Move(backup, dest);
        }
        catch
        {
            if (!Directory.Exists(dest) && Directory.Exists(failed)) Directory.Move(failed, dest);
            throw;
        }
        try { if (Directory.Exists(failed)) Directory.Delete(failed, recursive: true); } catch { }
    }

    private static void CleanupBackupDeferred(string? backup)
    {
        if (backup is null || !Directory.Exists(backup)) return;
        try
        {
            Directory.Delete(backup, recursive: true);
            return;
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        var start = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command \"" +
                "$p=$env:DCENT_SETUP_CLEANUP_ROOT; for($i=0;$i -lt 60 -and (Test-Path -LiteralPath $p);$i++){" +
                "Start-Sleep -Milliseconds 500; Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue};" +
                "if(Test-Path -LiteralPath $p){exit 1}\"",
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.Environment["DCENT_SETUP_CLEANUP_ROOT"] = backup;
        Process.Start(start)?.Dispose();
    }

    private static void WriteShortcut(
        string path, string target, string workDir, string arguments, string description)
    {
        var escapedPath = path.Replace("'", "''");
        var escapedTarget = target.Replace("'", "''");
        var escapedWork = workDir.Replace("'", "''");
        var escapedArguments = arguments.Replace("'", "''");
        var escapedDescription = description.Replace("'", "''");
        var script =
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + escapedPath + "');" +
            "$s.TargetPath='" + escapedTarget + "';" +
            "$s.Arguments='" + escapedArguments + "';" +
            "$s.WorkingDirectory='" + escapedWork + "';" +
            "$s.Description='" + escapedDescription + "';$s.Save()";
        using var process = Process.Start(new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -ExecutionPolicy Bypass -Command \"" + script + "\"",
            UseShellExecute = false,
            CreateNoWindow = true,
        }) ?? throw new InvalidOperationException("Could not start the shortcut writer.");
        if (!process.WaitForExit(15000))
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            throw new TimeoutException("Start Menu shortcut creation timed out.");
        }
        if (process.ExitCode != 0)
            throw new InvalidOperationException(
                "Start Menu shortcut creation failed (exit " + process.ExitCode + ").");
        var shortcut = new FileInfo(path);
        if (!shortcut.Exists ||
            (shortcut.Attributes & FileAttributes.ReparsePoint) != 0 ||
            shortcut.Length <= 0)
            throw new InvalidOperationException("Start Menu shortcut was not published safely.");
    }

    private static string WriteUninstallScript(string dest, bool registeredInstall)
    {
        var script = Path.Combine(dest, "Uninstall.cmd");
        var helper = Path.Combine(dest, "Uninstall.ps1");
        using (var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream(
            "DCENT_Voice.Uninstall.ps1"))
        {
            if (resource is null)
            {
                throw new InvalidOperationException("Embedded uninstall helper is missing.");
            }
            using var reader = new StreamReader(resource, Encoding.UTF8, true);
            File.WriteAllText(helper, reader.ReadToEnd(), new UTF8Encoding(false));
        }
        var body =
            "@echo off\r\n" +
            "setlocal EnableDelayedExpansion\r\n" +
            "if /i \"%~1\"==\"__go\" goto cleanup\r\n" +
            "set \"PURGE=\"\r\n" +
            "if /i \"%~1\"==\"/PurgeUserData\" set \"PURGE=/PurgeUserData\"\r\n" +
            "if /i \"%~2\"==\"/PurgeUserData\" set \"PURGE=/PurgeUserData\"\r\n" +
            "for /f %%G in ('powershell.exe -NoProfile -Command \"[guid]::NewGuid().ToString('N')\"') do set \"RUNID=%%G\"\r\n" +
            "if not defined RUNID exit /b 60\r\n" +
            "set \"RUNNER=%TEMP%\\DCENT_Voice-Uninstall-!RUNID!.cmd\"\r\n" +
            "set \"HELPER=%TEMP%\\DCENT_Voice-Uninstall-!RUNID!.ps1\"\r\n" +
            "copy /y \"%~f0\" \"!RUNNER!\" >nul\r\n" +
            "if errorlevel 1 exit /b 60\r\n" +
            "copy /y \"%~dp0Uninstall.ps1\" \"!HELPER!\" >nul\r\n" +
            "if errorlevel 1 exit /b 62\r\n" +
            // Invoking another batch without CALL transfers control instead
            // of returning to this soon-to-be-deleted installed batch.
            "\"!RUNNER!\" __go \"%~dp0.\" \"!HELPER!\" \"!PURGE!\"\r\n" +
            "exit /b 61\r\n" +
            ":cleanup\r\n" +
            "set \"INSTALLROOT=%~2\"\r\n" +
            "set \"HELPER=%~3\"\r\n" +
            "set \"PURGEARG=\"\r\n" +
            "if /i \"%~4\"==\"/PurgeUserData\" set \"PURGEARG=-PurgeUserData\"\r\n" +
            "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass " +
            "-File \"%HELPER%\" -InstallRoot \"%INSTALLROOT%\" !PURGEARG! " +
            (registeredInstall ? "" : "-Unregistered") + "\r\n" +
            "set \"RC=!ERRORLEVEL!\"\r\n" +
            "del /q \"%HELPER%\" >nul 2>&1\r\n" +
            "set \"DCENT_UNINSTALL_RUNNER=%~f0\"\r\n" +
            "start \"\" /b powershell.exe -NoProfile -WindowStyle Hidden -Command " +
            "\"Start-Sleep -Milliseconds 250; Remove-Item -LiteralPath " +
            "$env:DCENT_UNINSTALL_RUNNER -Force -ErrorAction SilentlyContinue\"\r\n" +
            "exit /b !RC!\r\n";
        File.WriteAllText(script, body);
        return script;
    }

    private static void WriteUninstall(string dest, string uninstallCmd, string exe)
    {
        using var key = Registry.CurrentUser.CreateSubKey(
            RecoveryCoordinator.DefaultRegistrySubKey, writable: true)
            ?? throw new InvalidOperationException("Could not create Add/Remove Programs registration.");
        key.SetValue("DisplayName", Product);
        key.SetValue("Publisher", "D-Central Technologies");
        key.SetValue("DisplayVersion", Version);
        key.SetValue("InstallLocation", dest);
        key.SetValue("DisplayIcon", exe);
        key.SetValue("UninstallString", "\"" + uninstallCmd + "\"");
        key.SetValue("QuietUninstallString", "\"" + uninstallCmd + "\"");
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        key.SetValue("EstimatedSize", EstimateSizeKb(dest), RegistryValueKind.DWord);
        RecoveryCoordinator.ClearRecoveryValues(key);
        if (!String.Equals(key.GetValue("DisplayName") as string, Product, StringComparison.Ordinal) ||
            !String.Equals(key.GetValue("InstallLocation") as string, dest, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Add/Remove Programs registration did not persist.");
    }

    private static int Uninstall(
        string dest,
        bool silent,
        bool purgeUserData,
        bool registeredInstall)
    {
        if (!silent)
        {
            if (!registeredInstall)
            {
                var customConfirm = MessageBox.Show(
                    "Remove this custom DCENT_Voice application payload?\n\nSettings, credentials, ADE records, and durable models will be retained.",
                    Product,
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Question);
                if (customConfirm != DialogResult.Yes)
                    return 0;
                purgeUserData = false;
            }
            else
            {
                var confirm = MessageBox.Show(
                    "Remove DCENT_Voice?\n\nYes: remove the app and permanently purge settings, personalization, consent/egress records, and saved provider credentials.\n\nNo: remove the app but keep those user records for a future reinstall.\n\nCancel: do nothing.",
                    Product,
                    MessageBoxButtons.YesNoCancel,
                    MessageBoxIcon.Question);
                if (confirm == DialogResult.Cancel)
                {
                    return 0;
                }
                purgeUserData = confirm == DialogResult.Yes;
            }
        }
        var exitCode = RecoveryCoordinator.RunUninstaller(
            dest, silent, purgeUserData,
            RecoveryCoordinator.DefaultRegistrySubKey,
            registeredInstall);
        if (exitCode != 0)
        {
            return exitCode;
        }
        if (!silent)
        {
            MessageBox.Show("DCENT_Voice was removed.", Product, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        return 0;
    }

    private static int EstimateSizeKb(string dest)
    {
        long bytes = 0;
        if (!Directory.Exists(dest))
        {
            return 0;
        }
        foreach (var file in Directory.GetFiles(dest, "*", SearchOption.AllDirectories))
        {
            try { bytes += new FileInfo(file).Length; } catch { /* skip */ }
        }
        return (int)Math.Min(int.MaxValue, bytes / 1024);
    }
}
