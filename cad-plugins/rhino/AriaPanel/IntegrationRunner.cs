// W12.2 — Rhino integration test runner.
//
// Loaded by the AriaPanel plug-in. Reads a plan JSON from a path
// supplied via the ARIA_INTEGRATION_PLAN env var (or a Rhino
// command-line param), executes every op via the existing
// AriaBridge.ExecuteFeature dispatcher, captures a viewport
// screenshot per op, and writes a structured result JSON to
// ARIA_INTEGRATION_OUT.
//
// Invocation modes:
//   A — Manual: run the `AriaIntegrate` Rhino command from the
//       command line. Reads env vars at command time.
//   B — Automated (W12.4 aggregator): the harness sets env vars
//       and invokes `Rhino.exe -nosplash -runscript="!AriaIntegrate"`.
//
// Output layout matches the Fusion runner so the W12.4 aggregator
// can compose a single cross-host report:
//
//   outputs/integration/<ts>/rhino/
//     summary.json
//     op_001_<kind>.json
//     op_001_<kind>.png
//     ...

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Rhino;
using Rhino.Commands;
using Rhino.Display;
using Rhino.Input;

namespace AriaPanel
{
    /// <summary>
    /// `AriaIntegrate` Rhino command — runs a plan from disk and
    /// writes per-op + summary JSON. Designed to be unattended:
    /// reads paths from env vars, no UI prompts.
    /// </summary>
    [System.Runtime.InteropServices.Guid("a26155a0-c0f3-4ce1-be5c-8ee6a33eeaaa")]
    public class IntegrationRunnerCommand : Command
    {
        public override string EnglishName => "AriaIntegrate";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            var planPath = Environment.GetEnvironmentVariable(
                "ARIA_INTEGRATION_PLAN");
            if (string.IsNullOrEmpty(planPath) || !File.Exists(planPath))
            {
                RhinoApp.WriteLine(
                    $"[AriaIntegrate] ARIA_INTEGRATION_PLAN unset or not "
                    + $"a file: {planPath}");
                return Result.Failure;
            }

            var outDir = Environment.GetEnvironmentVariable(
                "ARIA_INTEGRATION_OUT");
            if (string.IsNullOrEmpty(outDir))
            {
                var ts = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ");
                outDir = Path.Combine(
                    Directory.GetCurrentDirectory(),
                    "outputs", "integration", ts, "rhino");
            }
            Directory.CreateDirectory(outDir);

            try
            {
                var summary = RunPlan(planPath, outDir, doc);
                var summaryPath = Path.Combine(outDir, "summary.json");
                File.WriteAllText(summaryPath,
                    JsonConvert.SerializeObject(summary, Formatting.Indented));
                RhinoApp.WriteLine(
                    $"[AriaIntegrate] {summary.NPassed}/{summary.NOps} "
                    + $"passed → {summaryPath}");
                return summary.NFailed == 0 ? Result.Success : Result.Failure;
            }
            catch (Exception exc)
            {
                RhinoApp.WriteLine(
                    $"[AriaIntegrate] runner crashed: {exc.Message}");
                return Result.Failure;
            }
        }

        public class IntegrationSummary
        {
            [JsonProperty("timestamp_utc")]
            public string TimestampUtc { get; set; } = "";
            [JsonProperty("host")]
            public string Host { get; set; } = "rhino";
            [JsonProperty("rhino_version")]
            public string RhinoVersion { get; set; } = "";
            [JsonProperty("plan_id")]
            public string PlanId { get; set; } = "";
            [JsonProperty("plan_path")]
            public string PlanPath { get; set; } = "";
            [JsonProperty("n_ops")]
            public int NOps { get; set; }
            [JsonProperty("n_passed")]
            public int NPassed { get; set; }
            [JsonProperty("n_failed")]
            public int NFailed { get; set; }
            [JsonProperty("failed_at")]
            public int FailedAt { get; set; } = -1;
            [JsonProperty("elapsed_total_s")]
            public double ElapsedTotalS { get; set; }
            [JsonProperty("ops")]
            public List<OpRecord> Ops { get; set; } = new();
        }

        public class OpRecord
        {
            [JsonProperty("seq")]            public int Seq { get; set; }
            [JsonProperty("kind")]           public string Kind { get; set; } = "";
            [JsonProperty("ok")]             public bool Ok { get; set; }
            [JsonProperty("error")]          public string Error { get; set; }
            [JsonProperty("screenshot")]     public string Screenshot { get; set; }
            [JsonProperty("elapsed_s")]      public double ElapsedS { get; set; }
        }

        private static string SafeName(string kind, int seq)
        {
            var safe = (kind ?? "unknown")
                .Replace('/', '_').Replace(' ', '_');
            return $"op_{seq:D3}_{safe}";
        }

        private static bool CaptureViewport(string outPath)
        {
            try
            {
                var view = RhinoDoc.ActiveDoc?.Views.ActiveView;
                if (view == null) return false;
                var bmp = view.CaptureToBitmap(new System.Drawing.Size(1024, 768));
                if (bmp == null) return false;
                Directory.CreateDirectory(Path.GetDirectoryName(outPath));
                bmp.Save(outPath, System.Drawing.Imaging.ImageFormat.Png);
                return true;
            }
            catch { return false; }
        }

        private IntegrationSummary RunPlan(string planPath, string outDir, RhinoDoc doc)
        {
            var raw = File.ReadAllText(planPath);
            var token = JToken.Parse(raw);

            JArray ops;
            string planId = Path.GetFileNameWithoutExtension(planPath);
            if (token is JObject obj && obj["plan"] is JArray planArr)
            {
                ops = planArr;
                if (obj["id"] is JValue idVal) planId = idVal.ToString();
            }
            else if (token is JArray arr)
            {
                ops = arr;
            }
            else
            {
                throw new InvalidDataException(
                    $"Plan {planPath} must be array OR {{plan: [...]}}");
            }

            var bridge = new AriaBridge(/* panel host = */ null);
            var summary = new IntegrationSummary
            {
                TimestampUtc = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ"),
                RhinoVersion = RhinoApp.Version.ToString(),
                PlanId = planId,
                PlanPath = planPath,
                NOps = ops.Count,
            };

            var sw = Stopwatch.StartNew();
            int passed = 0, failed = 0, failedAt = -1;
            foreach (var (token2, idx) in ops.Select((t, i) => (t, i)))
            {
                int seq = idx + 1;
                if (!(token2 is JObject opObj))
                {
                    summary.Ops.Add(new OpRecord
                    {
                        Seq = seq, Kind = "?",
                        Ok = false, Error = "op is not a JSON object",
                    });
                    failed++;
                    if (failedAt < 0) failedAt = seq;
                    break;
                }
                var kind = opObj["kind"]?.ToString() ?? "?";
                var paramsObj = opObj["params"] as JObject ?? new JObject();
                var stem = SafeName(kind, seq);
                var rec = new OpRecord { Seq = seq, Kind = kind };
                var opSw = Stopwatch.StartNew();
                try
                {
                    var result = bridge.ExecuteFeature(kind, paramsObj);
                    rec.Ok = true;
                    passed++;
                }
                catch (Exception exc)
                {
                    rec.Ok = false;
                    rec.Error = $"{exc.GetType().Name}: {exc.Message}\n"
                                + (exc.StackTrace ?? "");
                    failed++;
                    if (failedAt < 0) failedAt = seq;
                }
                opSw.Stop();
                rec.ElapsedS = Math.Round(opSw.Elapsed.TotalSeconds, 2);

                var pngPath = Path.Combine(outDir, $"{stem}.png");
                if (CaptureViewport(pngPath))
                    rec.Screenshot = Path.GetFileName(pngPath);

                File.WriteAllText(
                    Path.Combine(outDir, $"{stem}.json"),
                    JsonConvert.SerializeObject(rec, Formatting.Indented));
                summary.Ops.Add(rec);

                if (!rec.Ok) break;   // halt on first failure (matches Fusion)
            }
            sw.Stop();
            summary.NPassed = passed;
            summary.NFailed = failed;
            summary.FailedAt = failedAt;
            summary.ElapsedTotalS = Math.Round(sw.Elapsed.TotalSeconds, 2);
            return summary;
        }
    }
}
