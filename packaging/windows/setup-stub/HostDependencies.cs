// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
//
// Host runtimes the *windows* need — never the ones dictation needs.
//
// Settings, the overlay and the setup wizard are pywebview windows: on Windows
// that is WinForms via pythonnet (.NET Framework 4.7.2+) rendering with the Edge
// WebView2 Evergreen runtime. Hold-to-talk dictation needs neither. So a missing
// runtime is reported, never fatal, and Setup does not bundle the ~150 MB
// standalone WebView2 redistributable (see docs/PACKAGING.md). The only network
// step Setup can ever take is opening the Microsoft download page, and only
// after the user clicks Yes.
//
// These registry locations mirror src/dcent_voice/ui/webview_runtime.py and
// src/dcent_voice/doctor/checks/ui_runtime.py; keep the three in step.
using System;
using System.Collections.Generic;
using Microsoft.Win32;

internal static class HostDependencies
{
    public const string WebView2DownloadUrl = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";

    private const string WebView2Client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    private static readonly string[] WebView2Keys =
    {
        @"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\" + WebView2Client,
        @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" + WebView2Client,
    };

    //: .NET Framework 4.7.2 — the floor pythonnet 3.x needs to host pywebview.
    public const int DotNetFramework472Release = 461808;

    private const string DotNetReleaseKey = @"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full";

    internal sealed class Report
    {
        public bool WebView2Present;
        public string WebView2Version = "";
        public int DotNetFrameworkRelease;

        public bool DotNetFrameworkSatisfied =>
            DotNetFrameworkRelease >= DotNetFramework472Release;

        public bool AllSatisfied => WebView2Present && DotNetFrameworkSatisfied;
    }

    public static Report Inspect()
    {
        var report = new Report();
        foreach (var path in WebView2Keys)
        {
            foreach (var hive in new[] { Registry.LocalMachine, Registry.CurrentUser })
            {
                var value = ReadString(hive, path, "pv");
                // EdgeUpdate leaves a 0.0.0.0 stub behind when the runtime is
                // removed; that is "absent", not "present".
                if (string.IsNullOrWhiteSpace(value) ||
                    string.Equals(value, "0.0.0.0", StringComparison.Ordinal))
                    continue;
                report.WebView2Present = true;
                if (report.WebView2Version.Length == 0)
                    report.WebView2Version = value!;
            }
        }
        report.DotNetFrameworkRelease = ReadDword(Registry.LocalMachine, DotNetReleaseKey, "Release");
        return report;
    }

    /// <summary>Human-readable lines for whatever is missing; empty when all present.</summary>
    public static List<string> MissingSummary(Report report)
    {
        var lines = new List<string>();
        if (!report.WebView2Present)
            lines.Add("Microsoft Edge WebView2 runtime — not installed");
        if (!report.DotNetFrameworkSatisfied)
            lines.Add(
                report.DotNetFrameworkRelease <= 0
                    ? ".NET Framework 4.7.2 or later — not found"
                    : ".NET Framework 4.7.2 or later — found release " +
                      report.DotNetFrameworkRelease + ", which is older");
        return lines;
    }

    private static string? ReadString(RegistryKey hive, string path, string name)
    {
        try
        {
            using var key = hive.OpenSubKey(path, writable: false);
            return key?.GetValue(name) as string;
        }
        catch (Exception) { return null; }
    }

    private static int ReadDword(RegistryKey hive, string path, string name)
    {
        try
        {
            using var key = hive.OpenSubKey(path, writable: false);
            var value = key?.GetValue(name);
            return value is int number ? number : 0;
        }
        catch (Exception) { return 0; }
    }
}
