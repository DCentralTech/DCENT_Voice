// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
//
// Setup proves the install by running the thing it just installed.
//
// After the tree is in place, Setup runs
//     <dest>\dcent-voice.exe doctor --json <tmp>\doctor.json --no-launch-checks --no-zip
// inside a private kill-on-close Job Object (OwnedJob), with an isolated
// DCENT_VOICE_PROFILE_ROOT so the user's real %APPDATA%\DCENT_Voice is never
// touched, DCENT_VOICE_NO_DIALOGS=1 so nothing can block, and
// DCENT_VOICE_DISABLE_AUTOSTART=1 so a diagnostic run cannot register a login
// item. doctor exits 0 (pass/warn), 1 (at least one check failed) or 2 (doctor
// itself could not run).
//
// A failure here is a *host* problem — a missing runtime, a locked folder, no
// audio device — not a bad payload: the payload was already hash-verified by
// ValidatePayload. So the install is kept and only the auto-launch is skipped.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.Json;

internal static class PostInstallCheck
{
    public const int TimeoutSeconds = 300;

    /// <summary>
    /// Closes every self-check failure dialog. The install is kept, but Setup
    /// exits non-zero so an unattended caller sees the failure it cannot read.
    /// </summary>
    public const string NonZeroExitNotice =
        "\nSetup will report this as a failed install so scripted deployments notice.";

    /// <summary>Refuse to parse a report far larger than doctor ever writes.</summary>
    public const long MaxReportBytes = 4 * 1024 * 1024;

    /// <summary>Most failing checks rendered into the dialog; the rest are counted.</summary>
    public const int MaxListedFailures = 8;

    internal sealed class Outcome
    {
        public int ExitCode = -1;
        public bool TimedOut;
        public string StartError = "";
        public string ReportPath = "";
        public string EvidenceDirectory = "";
        public string SummaryLine = "";
        public List<string> Failures = new List<string>();

        /// <summary>Every failing check, including those not rendered into the dialog.</summary>
        public int TotalFailures;

        /// <summary>How many failing checks the dialog had to omit.</summary>
        public int OmittedFailures => Math.Max(0, TotalFailures - Failures.Count);

        /// <summary>True only when doctor ran to completion with no failing check.</summary>
        public bool Passed => !TimedOut && StartError.Length == 0 && ExitCode == 0;

        /// <summary>doctor ran and reported failures (exit 1).</summary>
        public bool ReportedFailures => !TimedOut && StartError.Length == 0 && ExitCode == 1;
    }

    public static Outcome Run(string exe, string dest)
    {
        var outcome = new Outcome();
        var evidence = Path.Combine(
            Path.GetTempPath(),
            "DCENT_Voice-setup-check-" + Guid.NewGuid().ToString("N"));
        var report = Path.Combine(evidence, "doctor.json");
        outcome.EvidenceDirectory = evidence;
        outcome.ReportPath = report;
        try
        {
            Directory.CreateDirectory(evidence);
        }
        catch (Exception error)
        {
            outcome.StartError = error.Message;
            return outcome;
        }

        // The child inherits Setup's environment (OwnedJob merges these over it),
        // so anything the invoking shell happened to export would silently change
        // what the self-check proves. Pin the variables that matter:
        //  - the profile root, so the user's real %APPDATA%\DCENT_Voice is untouched
        //  - dialogs off and autostart off, so a diagnostic run cannot block or
        //    register a login item
        //  - hub access off and every proxy variable emptied, so doctor's
        //    egress.connections result is an honest offline proof rather than a
        //    statement about whichever proxy the installing shell was configured
        //    for. An empty value is deliberate: it must *override* an inherited
        //    one, and omitting the key would let the inherited value through.
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["DCENT_VOICE_PROFILE_ROOT"] = evidence,
            ["DCENT_VOICE_NO_DIALOGS"] = "1",
            ["DCENT_VOICE_DISABLE_AUTOSTART"] = "1",
            ["DCENT_VOICE_ALLOW_HUB"] = "0",
            ["HTTP_PROXY"] = "",
            ["HTTPS_PROXY"] = "",
            ["ALL_PROXY"] = "",
            ["NO_PROXY"] = "",
        };
        var arguments =
            "doctor --json \"" + report.Replace("\"", "\\\"") + "\" --no-launch-checks --no-zip";

        using (var job = new OwnedJob())
        {
            OwnedProcess? process = null;
            try
            {
                process = job.StartSuspendedAssigned(exe, arguments, dest, environment);
                if (!process.WaitForExit(TimeoutSeconds * 1000))
                {
                    outcome.TimedOut = true;
                    try { job.Terminate(124); } catch (Exception) { }
                    process.WaitForExit(5000);
                }
                else
                {
                    outcome.ExitCode = process.ExitCode;
                }
            }
            catch (Exception error)
            {
                outcome.StartError = error.Message;
            }
            finally
            {
                // Closing the private job reaps any descendant that outlived the
                // root process.
                process?.Dispose();
            }
        }

        ReadReport(outcome, report);
        if (outcome.Passed)
        {
            // Nothing to explain, so leave nothing behind: the throwaway profile
            // root holds a whole seeded config/state/diagnostics tree. On any
            // failure it is kept as evidence and named in the dialog instead.
            DeleteEvidence(evidence);
            outcome.EvidenceDirectory = "";
            outcome.ReportPath = "";
        }
        return outcome;
    }

    /// <summary>Best-effort, bounded removal. Never fails the install.</summary>
    private static void DeleteEvidence(string evidence)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                if (!Directory.Exists(evidence))
                    return;
                Directory.Delete(evidence, recursive: true);
                return;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            System.Threading.Thread.Sleep(200);
        }
    }

    private static void ReadReport(Outcome outcome, string report)
    {
        var file = new FileInfo(report);
        if (!file.Exists)
        {
            outcome.ReportPath = "";
            return;
        }
        try
        {
            // doctor's report is a few tens of KB. Anything near the cap is not a
            // report Setup should be parsing into a MessageBox, so refuse it and
            // point at the file instead of loading it.
            if (file.Length > MaxReportBytes)
            {
                outcome.Failures.Add(
                    "  • the report at " + report + " is unexpectedly large (" +
                    (file.Length / 1024) + " KB); it was not parsed. Open it directly.");
                return;
            }
            using var document = JsonDocument.Parse(File.ReadAllText(report));
            var root = document.RootElement;
            if (root.TryGetProperty("summary", out var summary))
            {
                outcome.SummaryLine =
                    Text(summary, "status").ToUpperInvariant() + ": " +
                    Number(summary, "pass") + " passed, " +
                    Number(summary, "warn") + " warnings, " +
                    Number(summary, "fail") + " failures";
            }
            if (root.TryGetProperty("checks", out var checks) &&
                checks.ValueKind == JsonValueKind.Array)
            {
                foreach (var check in checks.EnumerateArray())
                {
                    if (!String.Equals(Text(check, "status"), "fail", StringComparison.Ordinal))
                        continue;
                    outcome.TotalFailures++;
                    // A MessageBox that does not fit on screen tells the user
                    // nothing. Keep the first few and count the rest; the report
                    // named below the list has all of them.
                    if (outcome.Failures.Count >= MaxListedFailures)
                        continue;
                    var line = new StringBuilder();
                    line.Append("  • ").Append(Text(check, "id")).Append(" — ")
                        .Append(Text(check, "detail"));
                    var remediation = Text(check, "remediation");
                    if (remediation.Length != 0)
                        line.Append("\n      Fix: ").Append(remediation);
                    outcome.Failures.Add(line.ToString());
                }
            }
        }
        catch (Exception error)
        {
            outcome.Failures.Add("  • the report at " + report + " could not be read: " +
                error.Message);
        }
    }

    private static string Text(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";

    private static long Number(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) &&
        value.ValueKind == JsonValueKind.Number &&
        value.TryGetInt64(out var number)
            ? number
            : 0;

    /// <summary>The dialog text shown when doctor reported failing checks.</summary>
    public static string FailureMessage(Outcome outcome, string dest)
    {
        var text = new StringBuilder();
        text.Append("DCENT_Voice is installed, but its post-install self-check found a problem.\n\n");
        text.Append(dest).Append("\n\n");
        text.Append("The files are in place and nothing was rolled back. These checks failed:\n\n");
        if (outcome.Failures.Count == 0)
            text.Append("  • doctor reported a failure but named no check.\n");
        else
            foreach (var failure in outcome.Failures)
                text.Append(failure).Append('\n');
        if (outcome.OmittedFailures > 0)
            text.Append("  …and ").Append(outcome.OmittedFailures)
                .Append(" more, see report\n");
        if (outcome.SummaryLine.Length != 0)
            text.Append('\n').Append(outcome.SummaryLine).Append('\n');
        if (outcome.ReportPath.Length != 0)
            text.Append("\nFull report: ").Append(outcome.ReportPath).Append('\n');
        text.Append(
            "\nDCENT_Voice was not started. Fix the above, then start it from the Start Menu.\n");
        text.Append(NonZeroExitNotice);
        return text.ToString();
    }

    /// <summary>The dialog text shown when doctor could not run at all.</summary>
    public static string CouldNotRunMessage(Outcome outcome, string dest)
    {
        var reason = outcome.TimedOut
            ? "did not finish within " + TimeoutSeconds + " seconds and was stopped"
            : outcome.StartError.Length != 0
                ? "could not be started: " + outcome.StartError
                : "exited with code " + outcome.ExitCode + " without a usable report";
        var text = new StringBuilder();
        text.Append("DCENT_Voice is installed, but Setup could not run diagnostics.\n\n");
        text.Append(dest).Append("\n\n");
        text.Append("The files are in place and nothing was rolled back. The self-check ")
            .Append(reason).Append(".\n");
        if (outcome.ReportPath.Length != 0)
            text.Append("\nPartial report: ").Append(outcome.ReportPath).Append('\n');
        text.Append(
            "\nDCENT_Voice was not started. Start it from the Start Menu, and run " +
            "\"DCENT_Voice Diagnostics\" there if it does not appear.\n");
        text.Append(NonZeroExitNotice);
        return text.ToString();
    }

    /// <summary>One stderr line per problem, for /S installs and CI logs.</summary>
    public static string SilentDiagnostic(Outcome outcome)
    {
        var text = new StringBuilder();
        if (outcome.ReportedFailures)
        {
            text.Append("DCENT_Voice Setup: the install is complete but the post-install ")
                .Append("self-check failed. ").Append(outcome.SummaryLine).Append('\n');
            foreach (var failure in outcome.Failures)
                text.Append(failure.Replace("\n      ", " ")).Append('\n');
            if (outcome.OmittedFailures > 0)
                text.Append("  …and ").Append(outcome.OmittedFailures)
                    .Append(" more, see report\n");
        }
        else
        {
            text.Append("DCENT_Voice Setup: the install is complete but the post-install ")
                .Append("self-check could not run (")
                .Append(outcome.TimedOut
                    ? "timed out after " + TimeoutSeconds + "s"
                    : outcome.StartError.Length != 0
                        ? outcome.StartError
                        : "exit code " + outcome.ExitCode)
                .Append(").\n");
        }
        if (outcome.ReportPath.Length != 0)
            text.Append("DCENT_Voice Setup: doctor report at ").Append(outcome.ReportPath)
                .Append('\n');
        return text.ToString();
    }
}
