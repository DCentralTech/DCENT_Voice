// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
//
// Ownership boundary for the post-install self-check.
//
// Setup starts the freshly installed dcent-voice.exe to prove the install. That
// child must never outlive Setup, and no instruction inside it may run before
// the boundary exists — so the process is created CREATE_SUSPENDED, assigned to
// a private kill-on-close Job Object, and only then resumed. Closing the job
// reaps the root process and every descendant it managed to spawn.
//
// Ported from packaging/legacy/inno/verify-installed.ps1's DcentOwnedJob, which
// served the retired Inno pipeline.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

internal sealed class OwnedProcess : IDisposable
{
    private const uint WaitObject0 = 0x00000000;
    private const uint WaitTimeout = 0x00000102;
    private IntPtr handle;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr value, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr value);

    internal OwnedProcess(IntPtr processHandle)
    {
        handle = processHandle;
    }

    public bool WaitForExit(int milliseconds)
    {
        var result = WaitForSingleObject(handle, (uint)milliseconds);
        if (result == WaitObject0) return true;
        if (result == WaitTimeout) return false;
        throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public int ExitCode
    {
        get
        {
            if (!GetExitCodeProcess(handle, out var exitCode))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return unchecked((int)exitCode);
        }
    }

    public void Dispose()
    {
        if (handle != IntPtr.Zero)
        {
            CloseHandle(handle);
            handle = IntPtr.Zero;
        }
    }
}

internal sealed class OwnedJob : IDisposable
{
    private const uint KillOnJobClose = 0x00002000;
    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;
    private const uint CreateUnicodeEnvironment = 0x00000400;
    private const uint Infinite = 0xFFFFFFFF;
    private const int ExtendedLimitInformation = 9;
    private IntPtr handle;

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimit
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfo
    {
        public uint cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public uint dwX;
        public uint dwY;
        public uint dwXSize;
        public uint dwYSize;
        public uint dwXCountChars;
        public uint dwYCountChars;
        public uint dwFillAttribute;
        public uint dwFlags;
        public ushort wShowWindow;
        public ushort cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job, int informationClass, ref ExtendedLimit information, uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string? applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        bool inheritHandles,
        uint creationFlags,
        IntPtr environment,
        string? currentDirectory,
        ref StartupInfo startupInfo,
        out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr value, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr value);

    public OwnedJob()
    {
        handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        var information = new ExtendedLimit();
        information.BasicLimitInformation.LimitFlags = KillOnJobClose;
        if (!SetInformationJobObject(
                handle,
                ExtendedLimitInformation,
                ref information,
                (uint)Marshal.SizeOf(typeof(ExtendedLimit))))
        {
            var error = Marshal.GetLastWin32Error();
            Dispose();
            throw new Win32Exception(error);
        }
    }

    /// <summary>
    /// CreateProcess(CREATE_SUSPENDED) -> AssignProcessToJobObject -> ResumeThread.
    /// No child instruction runs before the kill-on-close boundary exists.
    /// </summary>
    public OwnedProcess StartSuspendedAssigned(
        string fileName,
        string arguments,
        string workingDirectory,
        IDictionary<string, string> extraEnvironment)
    {
        var startup = new StartupInfo();
        startup.cb = (uint)Marshal.SizeOf(typeof(StartupInfo));
        // CreateProcessW may write into lpCommandLine (it can insert a NUL to
        // split the module name), so the buffer needs room beyond the string.
        // `new StringBuilder(s)` allocates Capacity == s.Length exactly, which
        // leaves the marshaller no terminator slack — give it an explicit +1.
        var command = "\"" + fileName.Replace("\"", "\\\"") + "\" " + arguments;
        var commandLine = new StringBuilder(command, command.Length + 1);
        var environment = BuildEnvironmentBlock(extraEnvironment);
        var environmentHandle = Marshal.StringToHGlobalUni(environment);
        ProcessInformation created;
        try
        {
            if (!CreateProcess(
                    fileName,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    CreateSuspended | CreateNoWindow | CreateUnicodeEnvironment,
                    environmentHandle,
                    workingDirectory,
                    ref startup,
                    out created))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            Marshal.FreeHGlobal(environmentHandle);
        }

        var assigned = false;
        OwnedProcess? process = null;
        try
        {
            if (!AssignProcessToJobObject(handle, created.hProcess))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            assigned = true;
            process = new OwnedProcess(created.hProcess);
            created.hProcess = IntPtr.Zero;
            if (ResumeThread(created.hThread) == Infinite)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return process;
        }
        catch
        {
            process?.Dispose();
            if (assigned)
                TerminateJobObject(handle, 1);
            else if (created.hProcess != IntPtr.Zero)
                TerminateProcess(created.hProcess, 1);
            if (created.hProcess != IntPtr.Zero)
                WaitForSingleObject(created.hProcess, 5000);
            throw;
        }
        finally
        {
            CloseHandle(created.hThread);
            if (created.hProcess != IntPtr.Zero)
                CloseHandle(created.hProcess);
        }
    }

    public void Terminate(uint exitCode)
    {
        if (handle != IntPtr.Zero && !TerminateJobObject(handle, exitCode))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public void Dispose()
    {
        if (handle != IntPtr.Zero)
        {
            CloseHandle(handle);
            handle = IntPtr.Zero;
        }
    }

    /// <summary>Current environment plus overrides, as a UTF-16 double-NUL block.</summary>
    private static string BuildEnvironmentBlock(IDictionary<string, string> extraEnvironment)
    {
        var merged = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            var name = entry.Key as string;
            if (string.IsNullOrEmpty(name) || name.StartsWith("=", StringComparison.Ordinal))
                continue;
            merged[name] = entry.Value as string ?? string.Empty;
        }
        foreach (var pair in extraEnvironment)
            merged[pair.Key] = pair.Value;
        var block = new StringBuilder();
        foreach (var pair in merged)
            block.Append(pair.Key).Append('=').Append(pair.Value).Append('\0');
        block.Append('\0');
        return block.ToString();
    }
}
