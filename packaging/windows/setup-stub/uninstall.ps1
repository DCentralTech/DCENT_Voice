# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [string]$StatePath = "",
    [string]$ProgramsRoot = "",
    [string]$RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice",
    [string]$RunRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    [switch]$PurgeUserData,
    [switch]$Unregistered,
    [string]$UserDataRoot = "",
    [string]$ModelDataRoot = "",
    [string]$AdeModulesRoot = "",
    [Parameter(DontShow = $true)]
    [string]$CredentialService = "DCENT_Voice",
    [ValidateRange(100, 30000)]
    [int]$GraceTimeoutMs = 5000,
    [ValidateRange(100, 30000)]
    [int]$TerminateTimeoutMs = 5000,
    [Parameter(DontShow = $true)]
    [string]$TestStopAfter = "",
    [Parameter(DontShow = $true)]
    [string]$TestBeforeRenameSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestBeforeRenameContinueSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestDeleteStartedSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestDeleteContinueSignal = "",
    [Parameter(DontShow = $true)]
    [ValidateSet("auto", "basic", "unsupported")]
    [string]$TestDispositionMode = "auto",
    [Parameter(DontShow = $true)]
    [string]$TestFinalDispositionSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestFinalDispositionContinueSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestEntryEnumeratedRelativePath = "",
    [Parameter(DontShow = $true)]
    [string]$TestEntryEnumeratedSignal = "",
    [Parameter(DontShow = $true)]
    [string]$TestEntryEnumeratedContinueSignal = "",
    [Parameter(DontShow = $true)]
    [ValidateSet("snapshot", "tombstone")]
    [string]$TestEntryEnumeratedPhase = "snapshot",
    [Parameter(DontShow = $true)]
    [ValidateRange(4, 65536)]
    [int]$TestMaxPinnedEntries = 8192,
    [Parameter(DontShow = $true)]
    [ValidateRange(2, 256)]
    [int]$TestMaxPinnedDepth = 64
)

$ErrorActionPreference = "Stop"
$script:DefaultUninstallRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice"
$script:DefaultRunRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$script:UninstallTracePath = $env:DCENT_VOICE_UNINSTALL_TRACE
$script:UninstallTraceClock = [Diagnostics.Stopwatch]::StartNew()

function Write-UninstallTrace {
    param([string]$Phase)
    if ([string]::IsNullOrWhiteSpace($script:UninstallTracePath)) { return }
    [IO.File]::AppendAllText(
        $script:UninstallTracePath,
        ("{0:N3}|{1}" -f $script:UninstallTraceClock.Elapsed.TotalSeconds, $Phase) + [Environment]::NewLine
    )
}

Write-UninstallTrace "start"

function Fail-Uninstall {
    param([int]$Code, [string]$Message)
    [Console]::Error.WriteLine("DCENT_Voice uninstall failed: $Message")
    exit $Code
}

function Get-FullPath {
    param([string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
}

function Assert-PlainDirectory {
    param([string]$Path, [string]$Label)
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        throw "$Label is not a directory: $Path"
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label is a reparse point: $Path"
    }
}

function Assert-InstallMarkers {
    param([string]$Path)
    Assert-PlainDirectory $Path "install root"
    foreach ($required in @(
        "dcent-voice.exe",
        "dcent-voice-offline-bundle.json",
        "_internal\base_library.zip"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $required) -PathType Leaf)) {
            throw "refusing an invalid install root: $Path"
        }
    }
}

function Assert-CleanupTargets {
    param(
        [string]$Programs,
        [string]$DataRoot,
        [string]$ModelsRoot,
        [string]$ModulesRoot,
        [string]$RunPath,
        [string]$Service,
        [string]$Registration,
        [bool]$IsUnregistered
    )
    if (-not ([IO.Path]::GetFileName($DataRoot)).Equals("DCENT_Voice", [StringComparison]::OrdinalIgnoreCase)) {
        throw "user-data cleanup root is not the DCENT_Voice application directory"
    }
    if (-not ([IO.Path]::GetFileName($ModulesRoot)).Equals("modules", [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName([IO.Directory]::GetParent($ModulesRoot).FullName)).Equals("DCENT", [StringComparison]::OrdinalIgnoreCase)) {
        throw "ADE cleanup root is not the DCENT modules directory"
    }
    if (-not ([IO.Path]::GetFileName($ModelsRoot)).Equals("DCENT_Voice.Models", [StringComparison]::OrdinalIgnoreCase)) {
        throw "model cleanup root is not the DCENT_Voice.Models application directory"
    }
    if (-not $RunPath.StartsWith("HKCU:\Software\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "autostart cleanup registry path is outside HKCU:\Software"
    }
    if (-not $Service.Equals("DCENT_Voice", [StringComparison]::Ordinal) -and
        -not $Service.StartsWith("DCENT_Voice-Test-", [StringComparison]::Ordinal)) {
        throw "credential cleanup service is outside the DCENT_Voice namespace"
    }
    if (-not $IsUnregistered -and $Registration.Equals($script:DefaultUninstallRegistryPath, [StringComparison]::OrdinalIgnoreCase)) {
        $expectedPrograms = Get-FullPath (Join-Path ([Environment]::GetFolderPath("Programs")) "DCENT_Voice")
        $expectedData = Get-FullPath (Join-Path $env:APPDATA "DCENT_Voice")
        $expectedModels = Get-FullPath (Join-Path $env:LOCALAPPDATA "DCENT_Voice.Models")
        $expectedModules = Get-FullPath (Join-Path $env:LOCALAPPDATA "DCENT\modules")
        if (-not (Get-FullPath $Programs).Equals($expectedPrograms, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Get-FullPath $DataRoot).Equals($expectedData, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Get-FullPath $ModelsRoot).Equals($expectedModels, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Get-FullPath $ModulesRoot).Equals($expectedModules, [StringComparison]::OrdinalIgnoreCase) -or
            -not $RunPath.Equals($script:DefaultRunRegistryPath, [StringComparison]::OrdinalIgnoreCase) -or
            -not $Service.Equals("DCENT_Voice", [StringComparison]::Ordinal)) {
            throw "production cleanup targets do not match their canonical per-user locations"
        }
    }
}

function Remove-AdeDiscoveryRecords {
    param([string]$ModulesRoot)
    foreach ($name in @("dcent-voice.json", "dcent-voice.token", "dcent-voice.install.json")) {
        $path = Join-Path $ModulesRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $item = Get-Item -LiteralPath $path -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "ADE discovery record is a reparse point: $path"
            }
            Remove-Item -LiteralPath $path -Force
        }
    }
    if ((Test-Path -LiteralPath $ModulesRoot -PathType Container) -and
        @(Get-ChildItem -LiteralPath $ModulesRoot -Force).Count -eq 0) {
        Remove-Item -LiteralPath $ModulesRoot -Force
    }
}

function Remove-WindowsCredentials {
    param([string]$Service)
    if ($null -eq ("DCENTVoiceCredentialCleanup" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class DCENTVoiceCredentialCleanup {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential {
        public uint Flags; public uint Type; public IntPtr TargetName; public IntPtr Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize; public IntPtr CredentialBlob; public uint Persist;
        public uint AttributeCount; public IntPtr Attributes; public IntPtr TargetAlias; public IntPtr UserName;
    }
    [DllImport("advapi32.dll", EntryPoint="CredEnumerateW", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern bool CredEnumerate(string filter, uint flags, out uint count, out IntPtr credentials);
    [DllImport("advapi32.dll", EntryPoint="CredDeleteW", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern bool CredDelete(string target, uint type, uint flags);
    [DllImport("advapi32.dll", SetLastError=true)] private static extern void CredFree(IntPtr buffer);
    public static void Remove(string service) {
        uint count; IntPtr array;
        if (!CredEnumerate(null, 0, out count, out array)) {
            int error = Marshal.GetLastWin32Error();
            if (error == 1168) return;
            throw new System.ComponentModel.Win32Exception(error);
        }
        var targets = new List<string>();
        try {
            for (int index = 0; index < count; index++) {
                IntPtr pointer = Marshal.ReadIntPtr(array, index * IntPtr.Size);
                Credential item = Marshal.PtrToStructure<Credential>(pointer);
                string target = Marshal.PtrToStringUni(item.TargetName) ?? "";
                if (target == service || target.EndsWith("@" + service, StringComparison.Ordinal)) targets.Add(target);
            }
        } finally { CredFree(array); }
        foreach (string target in targets) {
            if (!CredDelete(target, 1, 0) && Marshal.GetLastWin32Error() != 1168)
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
    }
}
'@
    }
    [DCENTVoiceCredentialCleanup]::Remove($Service)
}

function Write-DurableUtf8 {
    param([string]$Path, [string]$Text)
    $bytes = New-Object Text.UTF8Encoding($false)
    $payload = $bytes.GetBytes($Text)
    $stream = New-Object IO.FileStream(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None,
        4096,
        [IO.FileOptions]::WriteThrough
    )
    try {
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Replace-DurableUtf8 {
    param([string]$Path, [string]$Text)
    $temporary = $Path + ".new-" + [Guid]::NewGuid().ToString("N")
    $backup = $Path + ".old-" + [Guid]::NewGuid().ToString("N")
    try {
        Write-DurableUtf8 $temporary $Text
        [IO.File]::Replace($temporary, $Path, $backup, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backup) {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-ProcessPath {
    param([System.Diagnostics.Process]$Process)
    try {
        if ($Process.HasExited) { return $null }
        $candidate = $Process.Path
        if ([string]::IsNullOrWhiteSpace($candidate)) { return $null }
        return Get-FullPath $candidate
    }
    catch {
        return $null
    }
}

function Test-ProcessBelowRoot {
    param([System.Diagnostics.Process]$Process, [string[]]$Roots)
    $candidate = Resolve-ProcessPath $Process
    if ($null -eq $candidate) { return $false }
    foreach ($root in $Roots) {
        $prefix = (Get-FullPath $root) + [IO.Path]::DirectorySeparatorChar
        if ($candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Get-OwnedProcesses {
    param([string[]]$Roots)
    # Query executable paths in one native WMI snapshot. Calling Process.Path
    # for every process on a busy desktop is surprisingly expensive (and made
    # the bounded uninstaller spend most of its deadline inspecting unrelated
    # processes). Re-open only candidate PIDs, then Test-ProcessBelowRoot
    # re-reads the path before either CloseMainWindow or Kill.
    $owned = New-Object Collections.ArrayList
    foreach ($candidate in Get-CimInstance Win32_Process -Property ProcessId, ExecutablePath -ErrorAction Stop) {
        if ([string]::IsNullOrWhiteSpace($candidate.ExecutablePath)) { continue }
        $candidatePath = Get-FullPath ([string]$candidate.ExecutablePath)
        $matches = $false
        foreach ($root in $Roots) {
            $prefix = (Get-FullPath $root) + [IO.Path]::DirectorySeparatorChar
            if ($candidatePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                $matches = $true
                break
            }
        }
        if (-not $matches) { continue }
        try {
            $process = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction Stop
            if (Test-ProcessBelowRoot $process $Roots) { [void]$owned.Add($process) }
        }
        catch { }
    }
    return @($owned)
}

function Stop-OwnedProcesses {
    param([string[]]$Roots)
    $graceDeadline = [DateTime]::UtcNow.AddMilliseconds($GraceTimeoutMs)
    do {
        $owned = @(Get-OwnedProcesses $Roots)
        foreach ($process in $owned) {
            try {
                if (Test-ProcessBelowRoot $process $Roots) {
                    [void]$process.CloseMainWindow()
                }
            }
            catch { }
        }
        if ($owned.Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $graceDeadline)

    $terminateDeadline = [DateTime]::UtcNow.AddMilliseconds($TerminateTimeoutMs)
    do {
        $owned = @(Get-OwnedProcesses $Roots)
        foreach ($process in $owned) {
            try {
                # Re-read the executable path immediately before termination.
                if (Test-ProcessBelowRoot $process $Roots) {
                    $process.Kill()
                }
            }
            catch { }
        }
        if ($owned.Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $terminateDeadline)

    $remaining = @(Get-OwnedProcesses $Roots)
    if ($remaining.Count -ne 0) {
        $ids = ($remaining | ForEach-Object { $_.Id }) -join ","
        Fail-Uninstall 20 "installed processes did not exit within the bounded timeout (PIDs: $ids)"
    }
}

function Test-FilesAvailable {
    param([string]$Root)
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($Root)) {
        $attributes = [IO.File]::GetAttributes($entry)
        $isDirectory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
        $isReparse = ($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isDirectory -and -not $isReparse) {
            Test-FilesAvailable $entry
        }
        elseif (-not $isDirectory -and -not $isReparse) {
            try {
                $probe = [IO.File]::Open(
                    $entry,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::Read,
                    [IO.FileShare]::None
                )
                $probe.Dispose()
            }
            catch {
                Fail-Uninstall 30 "installed file is still in use: $entry"
            }
        }
    }
}

if (-not ("DcentVoice.UninstallNative" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace DcentVoice {
    public sealed class DirectoryChild {
        public string Name;
        public ulong FileId;
        public uint FileAttributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DirectoryIdentity {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    public static class UninstallNative {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFile(
            string name, uint access, uint share, IntPtr security,
            uint creation, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out DirectoryIdentity information);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetFileInformationByHandle(
            SafeFileHandle handle, int informationClass,
            IntPtr information, uint bufferSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandleEx(
            SafeFileHandle handle, int informationClass,
            IntPtr information, uint bufferSize);

        public static DirectoryChild[] EnumerateDirectory(SafeFileHandle directory) {
            const int BufferSize = 65536;
            const int FileIdBothDirectoryInfo = 10;
            const int FileIdBothDirectoryRestartInfo = 11;
            const int ErrorNoMoreFiles = 18;
            var result = new System.Collections.Generic.List<DirectoryChild>();
            IntPtr buffer = Marshal.AllocHGlobal(BufferSize);
            try {
                bool restart = true;
                while (true) {
                    int infoClass = restart
                        ? FileIdBothDirectoryRestartInfo
                        : FileIdBothDirectoryInfo;
                    restart = false;
                    if (!GetFileInformationByHandleEx(
                        directory, infoClass, buffer, BufferSize
                    )) {
                        int error = Marshal.GetLastWin32Error();
                        if (error == ErrorNoMoreFiles) break;
                        throw new Win32Exception(error);
                    }
                    int offset = 0;
                    while (true) {
                        uint next = unchecked((uint)Marshal.ReadInt32(buffer, offset));
                        uint attributes = unchecked((uint)Marshal.ReadInt32(buffer, offset + 56));
                        uint nameBytes = unchecked((uint)Marshal.ReadInt32(buffer, offset + 60));
                        if ((nameBytes & 1) != 0 || nameBytes > BufferSize - offset - 104) {
                            throw new InvalidOperationException("invalid directory enumeration record");
                        }
                        ulong fileId = unchecked((ulong)Marshal.ReadInt64(buffer, offset + 96));
                        string name = Marshal.PtrToStringUni(
                            IntPtr.Add(buffer, offset + 104), checked((int)nameBytes / 2)
                        );
                        if (name != "." && name != "..") {
                            result.Add(new DirectoryChild {
                                Name = name,
                                FileId = fileId,
                                FileAttributes = attributes
                            });
                        }
                        if (next == 0) break;
                        if (next < 104 || next > BufferSize - offset) {
                            throw new InvalidOperationException("invalid directory enumeration offset");
                        }
                        offset = checked(offset + (int)next);
                    }
                }
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
            return result.ToArray();
        }

    }
}
"@
}

function Get-HandleIdentity {
    param($Handle)
    $identity = New-Object DcentVoice.DirectoryIdentity
    if (-not [DcentVoice.UninstallNative]::GetFileInformationByHandle($Handle, [ref]$identity)) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw (New-Object ComponentModel.Win32Exception($errorCode))
    }
    return [pscustomobject]@{
        Volume = [uint32]$identity.VolumeSerialNumber
        IndexHigh = [uint32]$identity.FileIndexHigh
        IndexLow = [uint32]$identity.FileIndexLow
        Attributes = [uint32]$identity.FileAttributes
    }
}

function Open-DirectoryIdentity {
    param([string]$Path)
    $handle = [DcentVoice.UninstallNative]::CreateFile(
        $Path,
        0,
        0x00000001 -bor 0x00000002 -bor 0x00000004,
        [IntPtr]::Zero,
        3,
        0x02000000,
        [IntPtr]::Zero
    )
    if ($handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        $handle.Dispose()
        throw (New-Object ComponentModel.Win32Exception($errorCode))
    }
    $identity = Get-HandleIdentity $handle
    return [pscustomobject]@{
        Handle = $handle
        Volume = $identity.Volume
        IndexHigh = $identity.IndexHigh
        IndexLow = $identity.IndexLow
        Attributes = $identity.Attributes
    }
}

function Open-RetainedEntry {
    param([string]$Path, [bool]$ShareDelete)
    # DELETE | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES. The snapshot used
    # across the root quarantine rename must share delete. It is promoted with
    # ReOpenFile to a no-share-delete handle before any payload byte is removed.
    $share = 0x00000001 -bor 0x00000002
    if ($ShareDelete) { $share = $share -bor 0x00000004 }
    $handle = [DcentVoice.UninstallNative]::CreateFile(
        $Path,
        0x00010000 -bor 0x00000080 -bor 0x00000100 -bor 0x00000001,
        $share,
        [IntPtr]::Zero,
        3,
        0x02000000 -bor 0x00200000,
        [IntPtr]::Zero
    )
    if ($handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        $handle.Dispose()
        throw (New-Object ComponentModel.Win32Exception($errorCode))
    }
    try {
        $identity = Get-HandleIdentity $handle
        return [pscustomobject]@{
            Handle = $handle
            Volume = $identity.Volume
            IndexHigh = $identity.IndexHigh
            IndexLow = $identity.IndexLow
            Attributes = $identity.Attributes
        }
    }
    catch {
        $handle.Dispose()
        throw
    }
}

function Open-PinnedEntry {
    param([string]$Path)
    return Open-RetainedEntry $Path $false
}

function Open-SharedEntry {
    param([string]$Path)
    return Open-RetainedEntry $Path $true
}

function Set-PinnedDeleteDisposition {
    param($Pinned)
    if ($TestDispositionMode -eq "unsupported") {
        throw (New-Object PlatformNotSupportedException("handle deletion was disabled by the deterministic test gate"))
    }

    # FileDispositionInfoEx: DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE.
    if ($TestDispositionMode -ne "basic") {
        $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
        try {
            [Runtime.InteropServices.Marshal]::WriteInt32($buffer, 0x00000001 -bor 0x00000002 -bor 0x00000010)
            if ([DcentVoice.UninstallNative]::SetFileInformationByHandle(
                $Pinned.Handle, 21, $buffer, 4
            )) { return "FileDispositionInfoEx" }
            $extendedError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        }
        finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
        }
        if ($extendedError -notin @(1, 50, 87, 120)) {
            throw (New-Object ComponentModel.Win32Exception($extendedError))
        }
    }

    # Object-bound legacy fallback. Clear readonly using FileBasicInfo on the
    # retained handle itself; no pathname is consulted after acquisition.
    if (($Pinned.Attributes -band [IO.FileAttributes]::ReadOnly) -ne 0) {
        $basic = [Runtime.InteropServices.Marshal]::AllocHGlobal(40)
        try {
            if (-not [DcentVoice.UninstallNative]::GetFileInformationByHandleEx(
                $Pinned.Handle, 0, $basic, 40
            )) {
                $attributeError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                throw (New-Object ComponentModel.Win32Exception($attributeError))
            }
            $attributes = [uint32][Runtime.InteropServices.Marshal]::ReadInt32($basic, 32)
            $attributes = $attributes -band (-bnot [uint32][IO.FileAttributes]::ReadOnly)
            [Runtime.InteropServices.Marshal]::WriteInt32($basic, 32, [int32]$attributes)
            if (-not [DcentVoice.UninstallNative]::SetFileInformationByHandle(
                $Pinned.Handle, 0, $basic, 40
            )) {
                $attributeError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                throw (New-Object ComponentModel.Win32Exception($attributeError))
            }
            $Pinned.Attributes = $attributes
        }
        finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($basic)
        }
    }
    $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    try {
        [Runtime.InteropServices.Marshal]::WriteInt32($buffer, 1)
        if (-not [DcentVoice.UninstallNative]::SetFileInformationByHandle(
            $Pinned.Handle, 4, $buffer, 4
        )) {
            $basicError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw (New-Object ComponentModel.Win32Exception($basicError))
        }
    }
    finally {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
    }
    return "FileDispositionInfo"
}

function Test-SameIdentity {
    param($Left, $Right)
    return $Left.Volume -eq $Right.Volume -and
        $Left.IndexHigh -eq $Right.IndexHigh -and
        $Left.IndexLow -eq $Right.IndexLow
}

function Test-StateIdentity {
    param($Identity, $State)
    return $Identity.Volume -eq [uint32]$State.IdentityVolume -and
        $Identity.IndexHigh -eq [uint32]$State.IdentityIndexHigh -and
        $Identity.IndexLow -eq [uint32]$State.IdentityIndexLow
}

function Get-IdentityFileId {
    param($Identity)
    return [uint64]$Identity.IndexHigh * [uint64]4294967296 + [uint64]$Identity.IndexLow
}

function Invoke-TestBarrier {
    param([string]$Signal, [string]$ContinueSignal, [string]$Payload, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Signal)) { return }
    [IO.File]::WriteAllText((Get-FullPath $Signal), $Payload)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $ContinueSignal) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 10
    }
    if (-not (Test-Path -LiteralPath $ContinueSignal)) {
        throw "$Label continuation timed out"
    }
}

function Get-RetainedDirectoryChildren {
    param($Node)
    $isDirectory = ($Node.Attributes -band [IO.FileAttributes]::Directory) -ne 0
    $isReparse = ($Node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    if (-not $isDirectory -or $isReparse) { return @() }
    return @([DcentVoice.UninstallNative]::EnumerateDirectory($Node.Handle) | Sort-Object Name)
}

$script:EntryEnumerationHookFired = $false

function Add-RetainedTreeNode {
    param(
        $Tree,
        [string]$Path,
        [string]$RelativePath,
        [int]$Depth,
        $ExpectedDirectoryEntry = $null,
        $ParentNode = $null
    )
    if ($Depth -gt $TestMaxPinnedDepth) {
        throw "retained payload depth exceeds the bounded limit of $TestMaxPinnedDepth"
    }
    if ($Path.Length -gt 32000) { throw "retained payload path exceeds the Windows bound" }
    if ($Tree.Nodes.Count -ge $TestMaxPinnedEntries) {
        throw "retained payload entry count exceeds the bounded limit of $TestMaxPinnedEntries"
    }
    $opened = $null
    try {
        $opened = if ($Tree.ShareDelete) { Open-SharedEntry $Path } else { Open-PinnedEntry $Path }
        if ($null -ne $ExpectedDirectoryEntry) {
            if ((Get-IdentityFileId $opened) -ne [uint64]$ExpectedDirectoryEntry.FileId) {
                throw "enumerated object identity changed before handle acquisition: $RelativePath"
            }
            $typeMask = [uint32][IO.FileAttributes]::Directory -bor [uint32][IO.FileAttributes]::ReparsePoint
            if (($opened.Attributes -band $typeMask) -ne
                ([uint32]$ExpectedDirectoryEntry.FileAttributes -band $typeMask)) {
                throw "enumerated object type changed before handle acquisition: $RelativePath"
            }
        }
        $children = New-Object Collections.ArrayList
        $node = [pscustomobject]@{
            Path = $Path
            RelativePath = $RelativePath
            Name = if ([string]::IsNullOrEmpty($RelativePath)) { "" } else { [IO.Path]::GetFileName($RelativePath) }
            Depth = $Depth
            Handle = $opened.Handle
            Volume = $opened.Volume
            IndexHigh = $opened.IndexHigh
            IndexLow = $opened.IndexLow
            Attributes = $opened.Attributes
            ParentVolume = if ($null -eq $ParentNode) { $null } else { $ParentNode.Volume }
            ParentIndexHigh = if ($null -eq $ParentNode) { $null } else { $ParentNode.IndexHigh }
            ParentIndexLow = if ($null -eq $ParentNode) { $null } else { $ParentNode.IndexLow }
            Children = $children
        }
        $opened = $null
        [void]$Tree.Nodes.Add($node)
        if ($null -eq $Tree.Root) { $Tree.Root = $node }

        $seen = @{}
        foreach ($entry in Get-RetainedDirectoryChildren $node) {
            if ([string]::IsNullOrWhiteSpace($entry.Name) -or
                $entry.Name.Contains("\") -or $entry.Name.Contains("/") -or
                $entry.Name -in @(".", "..")) {
                throw "invalid child name returned by retained directory enumeration"
            }
            if ($seen.ContainsKey($entry.Name)) {
                throw "duplicate child name returned by retained directory enumeration: $($entry.Name)"
            }
            $seen[$entry.Name] = $true
            $childRelative = if ([string]::IsNullOrEmpty($RelativePath)) {
                $entry.Name
            } else {
                $RelativePath + "\" + $entry.Name
            }
            $hookPhaseMatches = ($Tree.ShareDelete -and $TestEntryEnumeratedPhase -eq "snapshot") -or
                (-not $Tree.ShareDelete -and $TestEntryEnumeratedPhase -eq "tombstone")
            if ($hookPhaseMatches -and -not $script:EntryEnumerationHookFired -and
                -not [string]::IsNullOrWhiteSpace($TestEntryEnumeratedSignal) -and
                $childRelative.Equals($TestEntryEnumeratedRelativePath, [StringComparison]::OrdinalIgnoreCase)) {
                $script:EntryEnumerationHookFired = $true
                Invoke-TestBarrier $TestEntryEnumeratedSignal $TestEntryEnumeratedContinueSignal $childRelative "test enumerated-entry"
            }
            $childPath = Join-Path $Path $entry.Name
            $child = Add-RetainedTreeNode $Tree $childPath $childRelative ($Depth + 1) $entry $node
            [void]$node.Children.Add($child)
        }
        return $node
    }
    catch {
        if ($null -ne $opened) { $opened.Handle.Dispose() }
        throw
    }
}

function Assert-RetainedTreeNodeClosedWorld {
    param($Node)
    $liveIdentity = Get-HandleIdentity $Node.Handle
    if (-not (Test-SameIdentity $Node $liveIdentity)) {
        throw "retained handle identity changed: $($Node.RelativePath)"
    }
    $actual = @(Get-RetainedDirectoryChildren $Node)
    if ($actual.Count -ne $Node.Children.Count) {
        throw "retained directory child count changed: $($Node.RelativePath)"
    }
    $expectedByName = @{}
    foreach ($child in $Node.Children) { $expectedByName[[string]$child.Name] = $child }
    foreach ($entry in $actual) {
        if (-not $expectedByName.ContainsKey($entry.Name)) {
            throw "unexpected child appeared in retained directory: $($Node.RelativePath)\$($entry.Name)"
        }
        $child = $expectedByName[$entry.Name]
        if ((Get-IdentityFileId $child) -ne [uint64]$entry.FileId) {
            throw "retained directory child identity changed: $($child.RelativePath)"
        }
        if ($child.ParentVolume -ne $Node.Volume -or
            $child.ParentIndexHigh -ne $Node.IndexHigh -or
            $child.ParentIndexLow -ne $Node.IndexLow) {
            throw "retained child parent identity changed: $($child.RelativePath)"
        }
    }
    foreach ($child in $Node.Children) { Assert-RetainedTreeNodeClosedWorld $child }
}

function Assert-RetainedTreeClosedWorld {
    param($Tree)
    Assert-RetainedTreeNodeClosedWorld $Tree.Root
}

function Get-RetainedTreeInventory {
    param($Tree)
    return @($Tree.Nodes | Sort-Object RelativePath | ForEach-Object {
        [pscustomobject][ordered]@{
            RelativePath = [string]$_.RelativePath
            ParentRelativePath = if ([string]::IsNullOrEmpty($_.RelativePath)) {
                ""
            } else {
                [string][IO.Path]::GetDirectoryName($_.RelativePath)
            }
            Volume = [uint32]$_.Volume
            IndexHigh = [uint32]$_.IndexHigh
            IndexLow = [uint32]$_.IndexLow
            Attributes = [uint32]$_.Attributes
        }
    })
}

function Assert-RetainedTreeMatchesInventory {
    param($Tree, $Inventory)
    $records = @($Inventory)
    if ($records.Count -eq 0) { throw "recovery-state payload inventory is missing" }
    if ($records.Count -gt $TestMaxPinnedEntries) {
        throw "recovery-state payload inventory exceeds the bounded entry limit"
    }
    if ($records.Count -ne $Tree.Nodes.Count) {
        throw "retained payload differs from the recorded entry count"
    }
    $nodes = @{}
    foreach ($node in $Tree.Nodes) {
        if ($nodes.ContainsKey($node.RelativePath)) {
            throw "retained payload has a duplicate relative path"
        }
        $nodes[$node.RelativePath] = $node
    }
    $seen = @{}
    foreach ($record in $records) {
        $relative = [string]$record.RelativePath
        if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains("..\") -or
            $relative.Contains("../") -or $relative -eq ".." -or
            $relative.Contains(":") -or $relative.Length -gt 32000) {
            throw "recovery-state payload inventory contains an unsafe relative path"
        }
        if ($seen.ContainsKey($relative)) { throw "recovery-state payload inventory contains a duplicate path" }
        $seen[$relative] = $true
        if (-not $nodes.ContainsKey($relative)) {
            throw "recorded payload entry is missing: $relative"
        }
        $node = $nodes[$relative]
        if ($node.Volume -ne [uint32]$record.Volume -or
            $node.IndexHigh -ne [uint32]$record.IndexHigh -or
            $node.IndexLow -ne [uint32]$record.IndexLow) {
            throw "recorded payload entry identity changed: $relative"
        }
        $typeMask = [uint32][IO.FileAttributes]::Directory -bor [uint32][IO.FileAttributes]::ReparsePoint
        if (($node.Attributes -band $typeMask) -ne ([uint32]$record.Attributes -band $typeMask)) {
            throw "recorded payload entry type changed: $relative"
        }
        $parentRelative = if ([string]::IsNullOrEmpty($relative)) {
            ""
        } else {
            [string][IO.Path]::GetDirectoryName($relative)
        }
        if (-not $parentRelative.Equals([string]$record.ParentRelativePath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "recorded payload parent binding changed: $relative"
        }
    }
    if (-not $seen.ContainsKey("")) { throw "recovery-state payload inventory omits its root" }
}

function Close-RetainedTree {
    param($Tree)
    if ($null -eq $Tree) { return }
    foreach ($node in $Tree.Nodes) {
        if ($null -ne $node.Handle) {
            try { $node.Handle.Dispose() } catch { }
            $node.Handle = $null
        }
    }
}

function New-RetainedTree {
    param([string]$RootPath, [bool]$ShareDelete)
    $tree = [pscustomobject]@{
        RootPath = $RootPath
        ShareDelete = $ShareDelete
        Root = $null
        Nodes = (New-Object Collections.ArrayList)
    }
    try {
        [void](Add-RetainedTreeNode $tree $RootPath "" 0)
        Assert-RetainedTreeClosedWorld $tree
        return $tree
    }
    catch {
        Close-RetainedTree $tree
        throw
    }
}

function Remove-RetainedTree {
    param($Tree, $ExpectedState)
    if ($Tree.ShareDelete) { throw "retained payload was not promoted to exclusive handles" }
    if (-not (Test-StateIdentity $Tree.Root $ExpectedState)) {
        throw "pinned tombstone identity differs from the recorded transaction"
    }
    Assert-RetainedTreeClosedWorld $Tree
    Invoke-TestBarrier $TestDeleteStartedSignal $TestDeleteContinueSignal "started" "test deletion"

    foreach ($node in @($Tree.Nodes | Sort-Object Depth -Descending)) {
        $remaining = @(Get-RetainedDirectoryChildren $node)
        if ($remaining.Count -ne 0) {
            throw "retained directory changed or remained non-empty before disposition: $($node.RelativePath)"
        }
        $mode = Set-PinnedDeleteDisposition $node
        $afterDisposition = Get-HandleIdentity $node.Handle
        if (-not (Test-SameIdentity $node $afterDisposition)) {
            throw "retained object identity changed after handle deletion disposition: $($node.RelativePath)"
        }
        $isRoot = [string]::IsNullOrEmpty($node.RelativePath)
        if ($isRoot) {
            Invoke-TestBarrier $TestFinalDispositionSignal $TestFinalDispositionContinueSignal $mode "test final-disposition"
        }
        # Publish progress while the exact object is still pinned. If a later
        # entry or a newly inserted child blocks completion, recovery compares
        # only the not-yet-disposed identities. A crash in the narrow interval
        # after kernel disposition but before this durable replace fails closed
        # on retry rather than assuming a missing object was deleted.
        $remainingInventory = @($ExpectedState.Inventory | Where-Object {
            -not ([string]$_.RelativePath).Equals(
                [string]$node.RelativePath,
                [StringComparison]::OrdinalIgnoreCase
            )
        })
        $ExpectedState.Inventory = $remainingInventory
        Replace-DurableUtf8 $ExpectedState.StatePath ($ExpectedState | ConvertTo-Json -Depth 5)
        $node.Handle.Dispose()
        $node.Handle = $null
    }
}

function New-RecoveryLauncher {
    param([string]$Path)
    $body = @'
@echo off
setlocal EnableDelayedExpansion
if /i "%~1"=="__go" goto cleanup
for /f %%G in ('powershell.exe -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "RUNID=%%G"
if not defined RUNID exit /b 60
set "RUNNER=%TEMP%\DCENT_Voice-Uninstall-!RUNID!.cmd"
set "HELPER=%TEMP%\DCENT_Voice-Uninstall-!RUNID!.ps1"
copy /y "%~f0" "!RUNNER!" >nul
if errorlevel 1 exit /b 60
copy /y "%~dp0Uninstall.ps1" "!HELPER!" >nul
if errorlevel 1 exit /b 62
"!RUNNER!" __go "%~dp0transaction.json" "!HELPER!"
exit /b 61
:cleanup
set "STATE=%~2"
set "HELPER=%~3"
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%HELPER%" -StatePath "%STATE%"
set "RC=!ERRORLEVEL!"
del /q "%HELPER%" >nul 2>&1
set "DCENT_UNINSTALL_RUNNER=%~f0"
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Milliseconds 250; Remove-Item -LiteralPath $env:DCENT_UNINSTALL_RUNNER -Force -ErrorAction SilentlyContinue"
exit /b !RC!
'@
    Write-DurableUtf8 $Path $body
}

function Write-RecoveryRegistration {
    param($State)
    if (-not $State.Registered) { return }
    $command = '"' + $State.RecoveryCommand + '"'
    Set-ItemProperty -LiteralPath $State.RegistryPath -Name "UninstallString" -Value $command
    Set-ItemProperty -LiteralPath $State.RegistryPath -Name "QuietUninstallString" -Value $command
    New-ItemProperty -LiteralPath $State.RegistryPath -Name "DCENTRecoveryUninstaller" -Value $State.RecoveryCommand -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $State.RegistryPath -Name "DCENTRecoveryState" -Value $State.StatePath -PropertyType String -Force | Out-Null
}

function Assert-State {
    param($State, [string]$LoadedStatePath)
    if ($State.SchemaVersion -notin @(1, 2, 3, 4)) { throw "unsupported recovery-state schema" }
    $id = [Guid]::ParseExact([string]$State.TransactionId, "N").ToString("N")
    if ([uint32]$State.IdentityVolume -eq 0 -and
        [uint32]$State.IdentityIndexHigh -eq 0 -and
        [uint32]$State.IdentityIndexLow -eq 0) {
        throw "recovery-state directory identity is missing"
    }
    $State.InstallRoot = Get-FullPath ([string]$State.InstallRoot)
    $State.TombstonePath = Get-FullPath ([string]$State.TombstonePath)
    $State.RecoveryRoot = Get-FullPath ([string]$State.RecoveryRoot)
    $State.RecoveryCommand = Get-FullPath ([string]$State.RecoveryCommand)
    $State.StatePath = Get-FullPath ([string]$State.StatePath)
    $State.ProgramsRoot = Get-FullPath ([string]$State.ProgramsRoot)
    $loaded = Get-FullPath $LoadedStatePath
    $parent = [IO.Directory]::GetParent($State.InstallRoot).FullName
    $leaf = [IO.Path]::GetFileName($State.InstallRoot)
    $expectedTombstone = Join-Path $parent (".$leaf.uninstall-$id.payload")
    $expectedRecovery = Join-Path $parent (".$leaf.uninstall-$id.recovery")
    if (-not $loaded.Equals($State.StatePath, [StringComparison]::OrdinalIgnoreCase) -or
        -not $State.RecoveryRoot.Equals($expectedRecovery, [StringComparison]::OrdinalIgnoreCase) -or
        -not $State.TombstonePath.Equals($expectedTombstone, [StringComparison]::OrdinalIgnoreCase) -or
        -not $State.StatePath.Equals((Join-Path $expectedRecovery "transaction.json"), [StringComparison]::OrdinalIgnoreCase) -or
        -not $State.RecoveryCommand.Equals((Join-Path $expectedRecovery "Uninstall.cmd"), [StringComparison]::OrdinalIgnoreCase)) {
        throw "recovery-state path binding is invalid"
    }
    Assert-PlainDirectory $State.RecoveryRoot "recovery root"
    if (-not ([string]$State.RegistryPath).StartsWith("HKCU:\Software\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "recovery registry path is outside HKCU:\Software"
    }
    if ($State.SchemaVersion -lt 3) {
        $State | Add-Member -NotePropertyName RunRegistryPath -NotePropertyValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $State | Add-Member -NotePropertyName PurgeUserData -NotePropertyValue $false
        $State | Add-Member -NotePropertyName UserDataRoot -NotePropertyValue (Join-Path $env:APPDATA "DCENT_Voice")
        $State | Add-Member -NotePropertyName AdeModulesRoot -NotePropertyValue (Join-Path $env:LOCALAPPDATA "DCENT\modules")
        $State | Add-Member -NotePropertyName CredentialService -NotePropertyValue "DCENT_Voice"
    }
    if ($State.SchemaVersion -lt 4) {
        $State | Add-Member -NotePropertyName ModelDataRoot -NotePropertyValue (Join-Path $env:LOCALAPPDATA "DCENT_Voice.Models")
        $State | Add-Member -NotePropertyName Unregistered -NotePropertyValue $false
    }
    if ($State.SchemaVersion -lt 1 -or $State.SchemaVersion -gt 4) {
        throw "recovery-state schema is unsupported"
    }
    $State.ProgramsRoot = Get-FullPath ([string]$State.ProgramsRoot)
    $State.UserDataRoot = Get-FullPath ([string]$State.UserDataRoot)
    $State.ModelDataRoot = Get-FullPath ([string]$State.ModelDataRoot)
    $State.AdeModulesRoot = Get-FullPath ([string]$State.AdeModulesRoot)
    if ([bool]$State.Unregistered -and
        -not $State.ProgramsRoot.Equals($State.InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "unregistered recovery cleanup root is not bound to its install root"
    }
    Assert-CleanupTargets $State.ProgramsRoot $State.UserDataRoot $State.ModelDataRoot $State.AdeModulesRoot ([string]$State.RunRegistryPath) ([string]$State.CredentialService) ([string]$State.RegistryPath) ([bool]$State.Unregistered)
}

$mutex = $null
$mutexHeld = $false
$retainedTree = $null
try {
    if ([string]::IsNullOrWhiteSpace($StatePath)) {
        $newTransaction = $true
        if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
            Fail-Uninstall 10 "InstallRoot or StatePath is required"
        }
        try {
            $target = Get-FullPath $InstallRoot
            if ([bool]$Unregistered -and [bool]$PurgeUserData) {
                throw "an unregistered custom install can remove only its own payload"
            }
            if (Test-Path -LiteralPath $target -PathType Container) {
                try { Assert-InstallMarkers $target }
                catch {
                    # A concurrently completing transaction may remove the
                    # root between the existence probe and marker validation.
                    # Join/reconcile it after acquiring the named mutex.
                    if (Test-Path -LiteralPath $target) { throw }
                }
            }
            elseif (Test-Path -LiteralPath $target) {
                throw "install root is not a directory: $target"
            }
        }
        catch {
            Fail-Uninstall 10 "cannot validate install root '$InstallRoot': $($_.Exception.Message)"
        }
        if ([string]::IsNullOrWhiteSpace($ProgramsRoot)) {
            $ProgramsRoot = if ([bool]$Unregistered) {
                $target
            } else {
                Join-Path ([Environment]::GetFolderPath("Programs")) "DCENT_Voice"
            }
        }
        $ProgramsRoot = Get-FullPath $ProgramsRoot
        if ([string]::IsNullOrWhiteSpace($UserDataRoot)) {
            $UserDataRoot = Join-Path $env:APPDATA "DCENT_Voice"
        }
        if ([string]::IsNullOrWhiteSpace($ModelDataRoot)) {
            $ModelDataRoot = Join-Path $env:LOCALAPPDATA "DCENT_Voice.Models"
        }
        if ([string]::IsNullOrWhiteSpace($AdeModulesRoot)) {
            $AdeModulesRoot = Join-Path $env:LOCALAPPDATA "DCENT\modules"
        }
        $UserDataRoot = Get-FullPath $UserDataRoot
        $ModelDataRoot = Get-FullPath $ModelDataRoot
        $AdeModulesRoot = Get-FullPath $AdeModulesRoot
        try { Assert-CleanupTargets $ProgramsRoot $UserDataRoot $ModelDataRoot $AdeModulesRoot $RunRegistryPath $CredentialService $RegistryPath ([bool]$Unregistered) }
        catch { Fail-Uninstall 10 "cannot validate cleanup targets: $($_.Exception.Message)" }
        $transactionId = [Guid]::NewGuid().ToString("N")
        $parent = [IO.Directory]::GetParent($target).FullName
        $leaf = [IO.Path]::GetFileName($target)
        $recoveryRoot = Join-Path $parent (".$leaf.uninstall-$transactionId.recovery")
        $tombstone = Join-Path $parent (".$leaf.uninstall-$transactionId.payload")
        if ((Test-Path -LiteralPath $recoveryRoot) -or (Test-Path -LiteralPath $tombstone)) {
            Fail-Uninstall 60 "unique uninstall transaction path already exists"
        }
        $stateFile = Join-Path $recoveryRoot "transaction.json"
        $state = [pscustomobject][ordered]@{
            SchemaVersion = 4
            TransactionId = $transactionId
            InstallRoot = $target
            TombstonePath = $tombstone
            RecoveryRoot = $recoveryRoot
            RecoveryCommand = (Join-Path $recoveryRoot "Uninstall.cmd")
            StatePath = $stateFile
            ProgramsRoot = $ProgramsRoot
            RegistryPath = $RegistryPath
            Registered = (-not [bool]$Unregistered) -and [bool](Test-Path -LiteralPath $RegistryPath)
            RunRegistryPath = $RunRegistryPath
            PurgeUserData = [bool]$PurgeUserData
            Unregistered = [bool]$Unregistered
            UserDataRoot = $UserDataRoot
            ModelDataRoot = $ModelDataRoot
            AdeModulesRoot = $AdeModulesRoot
            CredentialService = $CredentialService
            IdentityVolume = 0
            IdentityIndexHigh = 0
            IdentityIndexLow = 0
            Inventory = @()
        }
    }
    else {
        $newTransaction = $false
        try {
            $stateFile = Get-FullPath $StatePath
            $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            Assert-State $state $stateFile
            $target = $state.InstallRoot
            $tombstone = $state.TombstonePath
            $recoveryRoot = $state.RecoveryRoot
            $ProgramsRoot = $state.ProgramsRoot
            $RegistryPath = [string]$state.RegistryPath
            $RunRegistryPath = [string]$state.RunRegistryPath
            $PurgeUserData = [bool]$state.PurgeUserData
            $Unregistered = [bool]$state.Unregistered
            $UserDataRoot = [string]$state.UserDataRoot
            $ModelDataRoot = [string]$state.ModelDataRoot
            $AdeModulesRoot = [string]$state.AdeModulesRoot
            $CredentialService = [string]$state.CredentialService
        }
        catch {
            Fail-Uninstall 10 "cannot validate recovery state '$StatePath': $($_.Exception.Message)"
        }
    }

    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($target.ToLowerInvariant()))
    }
    finally { $hash.Dispose() }
    $mutexName = "Local\DCENT_Voice_Uninstall_" + (($digest | ForEach-Object { $_.ToString("x2") }) -join "")
    $mutex = New-Object Threading.Mutex($false, $mutexName)
    try {
        $mutexHeld = $mutex.WaitOne(30000)
    }
    catch [Threading.AbandonedMutexException] {
        $mutexHeld = $true
    }
    if (-not $mutexHeld) { Fail-Uninstall 60 "timed out waiting for another uninstall transaction" }

    # Another invocation may have published or resumed a transaction while
    # this process waited for the mutex. Join that durable transaction rather
    # than inventing a second tombstone or removing its recovery registration.
    if ($newTransaction) {
        $candidateStates = New-Object Collections.Generic.List[string]
        if (-not [bool]$Unregistered) { try {
            $registeredState = (Get-ItemProperty -LiteralPath $RegistryPath -Name "DCENTRecoveryState" -ErrorAction Stop).DCENTRecoveryState
            if (-not [string]::IsNullOrWhiteSpace($registeredState)) {
                $candidateStates.Add((Get-FullPath ([string]$registeredState)))
            }
        }
        catch { } }
        $targetParent = [IO.Directory]::GetParent($target).FullName
        $targetLeaf = [IO.Path]::GetFileName($target)
        foreach ($directory in Get-ChildItem -LiteralPath $targetParent -Directory -Force -Filter (".$targetLeaf.uninstall-*.recovery")) {
            $candidate = Join-Path $directory.FullName "transaction.json"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $candidateStates.Add((Get-FullPath $candidate))
            }
        }
        $candidateStates = @($candidateStates | Sort-Object -Unique)
        if ($candidateStates.Count -gt 1) {
            Fail-Uninstall 31 "multiple recovery transactions exist for this install root"
        }
        if ($candidateStates.Count -eq 1) {
            try {
                $existingState = Get-Content -LiteralPath $candidateStates[0] -Raw | ConvertFrom-Json
                Assert-State $existingState $candidateStates[0]
                if (-not $existingState.InstallRoot.Equals($target, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "pending recovery belongs to a different install root"
                }
                $state = $existingState
                $stateFile = $state.StatePath
                $tombstone = $state.TombstonePath
                $recoveryRoot = $state.RecoveryRoot
                $ProgramsRoot = $state.ProgramsRoot
                $RegistryPath = [string]$state.RegistryPath
                $RunRegistryPath = [string]$state.RunRegistryPath
                $PurgeUserData = [bool]$state.PurgeUserData
                $Unregistered = [bool]$state.Unregistered
                $UserDataRoot = [string]$state.UserDataRoot
                $ModelDataRoot = [string]$state.ModelDataRoot
                $AdeModulesRoot = [string]$state.AdeModulesRoot
                $CredentialService = [string]$state.CredentialService
                $newTransaction = $false
            }
            catch {
                Fail-Uninstall 31 "cannot join pending recovery transaction: $($_.Exception.Message)"
            }
        }
    }

    $targetExists = Test-Path -LiteralPath $target -PathType Container
    $tombstoneExists = Test-Path -LiteralPath $tombstone -PathType Container
    if (-not $targetExists -and -not $tombstoneExists -and $newTransaction) {
        if ((Test-Path -LiteralPath $ProgramsRoot) -or (Test-Path -LiteralPath $RegistryPath)) {
            Fail-Uninstall 10 "install payload is missing and no valid recovery transaction exists"
        }
        exit 0
    }
    if ($targetExists -and $tombstoneExists) {
        Fail-Uninstall 31 "both install root and recovery tombstone exist; refusing ambiguous state"
    }

    if ($targetExists) {
        try { Assert-InstallMarkers $target }
        catch { Fail-Uninstall 10 "cannot revalidate install root: $($_.Exception.Message)" }
        Stop-OwnedProcesses @($target)
        Write-UninstallTrace "owned-processes-stopped"
        Test-FilesAvailable $target

        $preparedIdentity = $null
        try {
            $preparedIdentity = Open-DirectoryIdentity $target
            if ($newTransaction) {
                $state.IdentityVolume = $preparedIdentity.Volume
                $state.IdentityIndexHigh = $preparedIdentity.IndexHigh
                $state.IdentityIndexLow = $preparedIdentity.IndexLow
            }
            elseif (-not (Test-StateIdentity $preparedIdentity $state)) {
                Fail-Uninstall 31 "install root identity differs from the recorded recovery transaction"
            }
        }
        finally {
            if ($null -ne $preparedIdentity) { $preparedIdentity.Handle.Dispose() }
        }

        try {
            if ($newTransaction) {
                New-Item -ItemType Directory -Path $recoveryRoot | Out-Null
                Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $recoveryRoot "Uninstall.ps1")
                New-RecoveryLauncher (Join-Path $recoveryRoot "Uninstall.cmd")
                Write-DurableUtf8 $stateFile ($state | ConvertTo-Json -Depth 3)
            }
            Write-RecoveryRegistration $state
        }
        catch { Fail-Uninstall 60 "could not publish recovery registration: $($_.Exception.Message)" }
        if ($TestStopAfter -eq "registered") {
            [Console]::Error.WriteLine("DCENT_Voice uninstall test stop after recovery registration")
            exit 70
        }
        try {
            # Acquire and validate a complete identity-bearing snapshot before
            # the root rename. These handles share delete so the root can move;
            # they remain bound to every original object across that move.
            $retainedTree = New-RetainedTree $target $true
            Write-UninstallTrace "initial-tree-retained"
            if (-not (Test-StateIdentity $retainedTree.Root $state)) {
                throw "retained install tree root differs from the recorded transaction"
            }
            $recordedInventory = if ($state.PSObject.Properties.Name -contains "Inventory") {
                @($state.Inventory)
            } else { @() }
            if ($recordedInventory.Count -ne 0) {
                Assert-RetainedTreeMatchesInventory $retainedTree $recordedInventory
            }
            else {
                $inventory = @(Get-RetainedTreeInventory $retainedTree)
                $state.SchemaVersion = 4
                if ($state.PSObject.Properties.Name -contains "Inventory") {
                    $state.Inventory = $inventory
                }
                else {
                    $state | Add-Member -NotePropertyName Inventory -NotePropertyValue $inventory
                }
                Replace-DurableUtf8 $stateFile ($state | ConvertTo-Json -Depth 5)
                Write-UninstallTrace "initial-inventory-durable"
            }
        }
        catch {
            Fail-Uninstall 30 "could not acquire a closed-world retained install tree; recovery registration was retained: $($_.Exception.Message)"
        }
        if (-not [string]::IsNullOrWhiteSpace($TestBeforeRenameSignal)) {
            [IO.File]::WriteAllText((Get-FullPath $TestBeforeRenameSignal), "ready")
            $testDeadline = [DateTime]::UtcNow.AddSeconds(10)
            while (-not (Test-Path -LiteralPath $TestBeforeRenameContinueSignal) -and
                [DateTime]::UtcNow -lt $testDeadline) {
                Start-Sleep -Milliseconds 10
            }
            if (-not (Test-Path -LiteralPath $TestBeforeRenameContinueSignal)) {
                Fail-Uninstall 70 "test pre-rename continuation timed out"
            }
        }

        $beforeIdentity = $null
        $afterIdentity = $null
        try {
            Assert-RetainedTreeClosedWorld $retainedTree
            Assert-RetainedTreeMatchesInventory $retainedTree $state.Inventory
            Assert-InstallMarkers $target
            if (-not ([IO.Path]::GetPathRoot($target)).Equals(
                [IO.Path]::GetPathRoot($tombstone),
                [StringComparison]::OrdinalIgnoreCase
            )) { throw "tombstone is not on the install volume" }
            $beforeIdentity = Open-DirectoryIdentity $target
            if (-not (Test-StateIdentity $beforeIdentity $state)) {
                throw "install root identity changed before quarantine rename"
            }
            # Directory.Move cannot rename a directory while descendants are
            # open on supported Windows versions. The durable identity manifest
            # is therefore the bridge across this narrow non-destructive rename:
            # close the validated snapshot, move, then reacquire every recorded
            # object without FILE_SHARE_DELETE before deleting anything.
            Close-RetainedTree $retainedTree
            $retainedTree = $null
            [IO.Directory]::Move($target, $tombstone)
            Write-UninstallTrace "root-renamed"
            Assert-PlainDirectory $tombstone "recovery tombstone"
            $afterIdentity = Open-DirectoryIdentity $tombstone
            if (-not (Test-SameIdentity $beforeIdentity $afterIdentity)) {
                throw "directory identity changed during quarantine rename"
            }
        }
        catch {
            $originalRemains = Test-Path -LiteralPath $target -PathType Container
            $quarantineExists = Test-Path -LiteralPath $tombstone -PathType Container
            if ($originalRemains -and -not $quarantineExists) {
                Fail-Uninstall 30 "atomic quarantine rename was refused; original install remains intact and recovery registration was retained: $($_.Exception.Message)"
            }
            if (-not $originalRemains -and $quarantineExists) {
                Fail-Uninstall 31 "quarantine rename completed but identity verification failed; recovery registration was retained: $($_.Exception.Message)"
            }
            else {
                Fail-Uninstall 31 "ambiguous quarantine rename outcome; recovery registration was retained: $($_.Exception.Message)"
            }
        }
        finally {
            if ($null -ne $afterIdentity) { $afterIdentity.Handle.Dispose() }
            if ($null -ne $beforeIdentity) { $beforeIdentity.Handle.Dispose() }
        }
        $tombstoneExists = Test-Path -LiteralPath $tombstone -PathType Container
        if ($TestStopAfter -eq "renamed") {
            [Console]::Error.WriteLine("DCENT_Voice uninstall test stop after quarantine rename")
            exit 71
        }
        try {
            $retainedTree = New-RetainedTree $tombstone $false
            Write-UninstallTrace "tombstone-tree-retained"
            Assert-RetainedTreeMatchesInventory $retainedTree $state.Inventory
            Assert-RetainedTreeClosedWorld $retainedTree
        }
        catch {
            Fail-Uninstall 30 "could not bind the quarantined payload to its retained object graph; recovery registration was retained: $($_.Exception.Message)"
        }
    }
    elseif (-not $tombstoneExists) {
        # Payload deletion completed before a prior process stopped. Continue
        # registration and recovery cleanup idempotently.
    }

    if ($tombstoneExists) {
        Stop-OwnedProcesses @($target, $tombstone)
        try {
            if ($null -eq $retainedTree) {
                $retainedTree = New-RetainedTree $tombstone $false
            }
            Assert-RetainedTreeMatchesInventory $retainedTree $state.Inventory
            Write-UninstallTrace "payload-removal-start"
            Remove-RetainedTree $retainedTree $state
            Write-UninstallTrace "payload-removed"
        }
        catch {
            Fail-Uninstall 30 "could not remove closed-world handle-pinned quarantined payload; recovery registration was retained: $($_.Exception.Message)"
        }
    }

    try {
        $productionRegistration = -not [bool]$Unregistered -and ([string]$RegistryPath).Equals(
            $script:DefaultUninstallRegistryPath,
            [StringComparison]::OrdinalIgnoreCase
        )
        $explicitRunTarget = -not [bool]$Unregistered -and -not ([string]$RunRegistryPath).Equals(
            $script:DefaultRunRegistryPath,
            [StringComparison]::OrdinalIgnoreCase
        )
        if (($productionRegistration -or $explicitRunTarget) -and (Test-Path -LiteralPath $RunRegistryPath)) {
            Remove-ItemProperty -LiteralPath $RunRegistryPath -Name "DCENT_Voice" -Force -ErrorAction SilentlyContinue
            if ($null -ne (Get-ItemProperty -LiteralPath $RunRegistryPath -Name "DCENT_Voice" -ErrorAction SilentlyContinue)) {
                throw "login autostart value remains"
            }
        }
        if (-not [bool]$Unregistered -and ($productionRegistration -or [bool]$PurgeUserData)) {
            Remove-AdeDiscoveryRecords $AdeModulesRoot
        }
    }
    catch {
        Fail-Uninstall 45 "could not remove autostart/ADE discovery state: $($_.Exception.Message)"
    }

    if ([bool]$PurgeUserData) {
        try {
            if (Test-Path -LiteralPath $UserDataRoot) {
                Assert-PlainDirectory $UserDataRoot "user-data root"
                Remove-Item -LiteralPath $UserDataRoot -Recurse -Force
            }
            if (Test-Path -LiteralPath $UserDataRoot) {
                throw "user-data root remains: $UserDataRoot"
            }
            if (Test-Path -LiteralPath $ModelDataRoot) {
                Assert-PlainDirectory $ModelDataRoot "model-data root"
                Remove-Item -LiteralPath $ModelDataRoot -Recurse -Force
            }
            if (Test-Path -LiteralPath $ModelDataRoot) {
                throw "model-data root remains: $ModelDataRoot"
            }
            Remove-WindowsCredentials $CredentialService
        }
        catch {
            Fail-Uninstall 46 "could not purge user data/credentials: $($_.Exception.Message)"
        }
    }

    try {
        if (-not [bool]$Unregistered -and (Test-Path -LiteralPath $ProgramsRoot)) {
            Remove-Item -LiteralPath $ProgramsRoot -Recurse -Force
        }
        if (-not [bool]$Unregistered -and (Test-Path -LiteralPath $ProgramsRoot)) {
            Fail-Uninstall 40 "Start Menu entry remains: $ProgramsRoot"
        }
    }
    catch {
        Fail-Uninstall 40 "could not remove Start Menu entry: $($_.Exception.Message)"
    }

    try {
        if (-not [bool]$Unregistered -and (Test-Path -LiteralPath $RegistryPath)) {
            Remove-Item -LiteralPath $RegistryPath -Recurse -Force
        }
        if (-not [bool]$Unregistered -and (Test-Path -LiteralPath $RegistryPath)) {
            Fail-Uninstall 50 "uninstall registration remains: $RegistryPath"
        }
    }
    catch {
        Fail-Uninstall 50 "could not remove uninstall registration: $($_.Exception.Message)"
    }

    try {
        if (Test-Path -LiteralPath $recoveryRoot) {
            Remove-Item -LiteralPath $recoveryRoot -Recurse -Force
        }
    }
    catch {
        [Console]::Error.WriteLine("DCENT_Voice uninstall warning: recovery residue could not be removed: $($_.Exception.Message)")
    }
    exit 0
}
finally {
    Write-UninstallTrace "finally"
    Close-RetainedTree $retainedTree
    if ($mutexHeld -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
