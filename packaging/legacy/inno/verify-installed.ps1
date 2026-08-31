# DCENT_Voice — bounded installed-tree verifier used by Inno Setup.
# SPDX-License-Identifier: MIT
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$Payload,
    [ValidateRange(1, 900)][int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

# This verifier is copied into Inno's temporary directory, so it cannot depend
# on a source checkout or external Python. Keep its ownership boundary
# self-contained: a private kill-on-close Job Object is assigned to the exact
# Process instance retained below, and no process-name/PID-tree discovery is
# ever used.
Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

public sealed class DcentOwnedProcess : IDisposable
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

    internal DcentOwnedProcess(IntPtr processHandle)
    {
        handle = processHandle;
    }

    public bool WaitForExit(int milliseconds)
    {
        uint result = WaitForSingleObject(handle, (uint)milliseconds);
        if (result == WaitObject0)
            return true;
        if (result == WaitTimeout)
            return false;
        throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public int ExitCode
    {
        get
        {
            uint exitCode;
            if (!GetExitCodeProcess(handle, out exitCode))
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

public sealed class DcentOwnedJob : IDisposable
{
    private const uint KillOnJobClose = 0x00002000;
    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;
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
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        ref ExtendedLimit information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        bool inheritHandles,
        uint creationFlags,
        IntPtr environment,
        string currentDirectory,
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

    public DcentOwnedJob()
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
            int error = Marshal.GetLastWin32Error();
            Dispose();
            throw new Win32Exception(error);
        }
    }

    public DcentOwnedProcess StartSuspendedAssigned(ProcessStartInfo start)
    {
        if (start == null)
            throw new ArgumentNullException("start");
        var startup = new StartupInfo();
        startup.cb = (uint)Marshal.SizeOf(typeof(StartupInfo));
        var commandLine = new StringBuilder(
            "\"" + start.FileName.Replace("\"", "\\\"") + "\" " + start.Arguments);
        ProcessInformation created;
        if (!CreateProcess(
                start.FileName,
                commandLine,
                IntPtr.Zero,
                IntPtr.Zero,
                false,
                CreateSuspended | CreateNoWindow,
                IntPtr.Zero,
                start.WorkingDirectory,
                ref startup,
                out created))
            throw new Win32Exception(Marshal.GetLastWin32Error());

        bool assigned = false;
        DcentOwnedProcess process = null;
        try
        {
            if (!AssignProcessToJobObject(handle, created.hProcess))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            assigned = true;
            process = new DcentOwnedProcess(created.hProcess);
            created.hProcess = IntPtr.Zero;
            if (ResumeThread(created.hThread) == Infinite)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return process;
        }
        catch
        {
            if (process != null)
                process.Dispose();
            if (assigned)
                TerminateJobObject(handle, 1);
            else
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
}
"@

$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = (Get-Item -LiteralPath $Executable -ErrorAction Stop).FullName
$resolvedPayload = (Get-Item -LiteralPath $Payload -ErrorAction Stop).FullName
$start.Arguments = 'verify-payload "' + $resolvedPayload.Replace('"', '\"') + '"'
$start.WorkingDirectory = $resolvedPayload
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$process = $null
$job = New-Object DcentOwnedJob
try {
    # CreateProcess(CREATE_SUSPENDED) -> assign private Job -> ResumeThread.
    # No verifier instruction can run before the ownership boundary exists.
    $process = $job.StartSuspendedAssigned($start)
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $job.Terminate(124)
        $process.WaitForExit(5000) | Out-Null
        [Console]::Error.WriteLine(
            "Installed payload verifier timed out after $TimeoutSeconds seconds and was terminated."
        )
        exit 124
    }
    if ($process.ExitCode -ne 0) {
        [Console]::Error.WriteLine(
            "Installed payload verification failed with exit code $($process.ExitCode)."
        )
        exit $process.ExitCode
    }
} finally {
    # Closing the private job also reaps any verifier descendants that outlive
    # an otherwise successful root process.
    $job.Dispose()
    if ($null -ne $process) {
        $process.Dispose()
    }
}
