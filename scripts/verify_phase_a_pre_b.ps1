<#
.SYNOPSIS
    Phase A pre-Phase-B acceptance verification for job-matcher-pr issue #352.

.DESCRIPTION
    Bundles three deferred Phase A acceptance criteria (AC #13, #16, #17) into
    a single local-dev verification run. Run this on Windows against the dev DB
    before Phase B (#347) starts.

    Steps performed:
      1. Source-string fixture refresh  - compares DB source strings to
                                          tests/fixtures/db_source_strings.json
                                          and refreshes the file if they differ.
      2. Pre-aggregator baseline        - captures docs/baselines/2026-04-27-pre-aggregator.json
                                          via scripts/capture_ingest_baseline.py.
      3. Live ingest smoke run          - runs ingest.py --hours 24 --verbose with
                                          JOB_AGGREGATOR_SOURCES=arbeitnow and verifies
                                          routing markers in the log (skippable via -SkipSmoke).
      4. Post-aggregator baseline       - captures a post-smoke baseline and prints
                                          row-count delta vs the pre baseline.

    A summary table is printed at the end. Exits non-zero if any non-skipped step fails.

.PARAMETER DatabaseUrl
    PostgreSQL connection string. Defaults to $env:DATABASE_URL.
    Example: postgresql://jobmatcher:secret@localhost:5432/jobmatcher_dev

.PARAMETER WorktreeRoot
    Root directory of the worktree. Defaults to the current working directory.
    All relative paths (scripts, tests/fixtures, docs/baselines) are resolved
    from this root.

.PARAMETER SkipSmoke
    Skip step 3 (live ingest.py --hours 24 smoke run). Useful when only
    baselines need to be refreshed without running a full ingest cycle.

.EXAMPLE
    # Full run from the worktree directory
    .\scripts\verify_phase_a_pre_b.ps1 -DatabaseUrl "postgresql://jobmatcher:secret@localhost:5432/jobmatcher_dev"

.EXAMPLE
    # Run from a different working directory by supplying -WorktreeRoot explicitly
    .\scripts\verify_phase_a_pre_b.ps1 -WorktreeRoot "I:\Web Development\job-matcher-pr\.worktrees\feat-352-phase-a-pre-b-verify" -DatabaseUrl $env:DATABASE_URL

.EXAMPLE
    # Skip the live ingest run (steps 1, 2, 4 only)
    .\scripts\verify_phase_a_pre_b.ps1 -SkipSmoke

.NOTES
    Issue: #352
    Depends on Phase A helpers from PR #351 (feat-346-aggregator-spike-arbeitnow).
    Run after that branch is checked out / merged.
#>

[CmdletBinding()]
param(
    [string] $DatabaseUrl  = $env:DATABASE_URL,
    [string] $WorktreeRoot = (Get-Location).Path,
    [switch] $SkipSmoke
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-StepHeader {
    param([string] $Title)
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Get-UtcTimestamp {
    return (Get-Date -AsUTC).ToString("yyyyMMddTHHmmssZ")
}

# Result accumulator: each entry is a PSCustomObject with Step, Result, Notes.
$summary = [System.Collections.Generic.List[psobject]]::new()

function Add-Result {
    param(
        [string] $Step,
        [ValidateSet("PASS","FAIL","SKIPPED")] [string] $Result,
        [string] $Notes = ""
    )
    $color = switch ($Result) {
        "PASS"    { "Green"  }
        "FAIL"    { "Red"    }
        "SKIPPED" { "Yellow" }
    }
    Write-Host "  [$Result] $Step" -ForegroundColor $color
    if ($Notes) { Write-Host "         $Notes" -ForegroundColor DarkGray }
    $summary.Add([pscustomobject]@{
        Step   = $Step
        Result = $Result
        Notes  = $Notes
    })
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "verify_phase_a_pre_b.ps1 - Phase A pre-Phase-B verification" -ForegroundColor White
Write-Host "WorktreeRoot : $WorktreeRoot"
# Anchor to the LAST @ before the host segment: [^/?@]+ ensures the suffix
# cannot match an @ inside the password, so the replacement always targets
# everything between the first colon and the final @ (handles raw or
# percent-encoded @ characters in the password without leaking them).
$redactedUrl = if ($DatabaseUrl) { $DatabaseUrl -replace '(?<prefix>postgresql://[^:/@]+:).+(?<suffix>@[^/?@]+)', '${prefix}***REDACTED***${suffix}' } else { '(not set)' }
Write-Host "DatabaseUrl  : $redactedUrl"
Write-Host "SkipSmoke    : $SkipSmoke"

if (-not $DatabaseUrl) {
    Write-Error "DatabaseUrl is not set and `$env:DATABASE_URL is empty. Set -DatabaseUrl or export DATABASE_URL."
    exit 1
}

# Verify psql is available before step 1 touches the DB.
$psqlCmd = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psqlCmd) {
    Write-Error "psql is not on PATH. Install PostgreSQL client tools (e.g. winget install PostgreSQL.psql) and ensure the bin directory is in PATH."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 1 - Source-string fixture refresh
# ---------------------------------------------------------------------------

Write-StepHeader "Step 1 - Source-string fixture refresh"

$step1Result = "FAIL"
$step1Notes  = ""

try {
    $fixtureFile = Join-Path $WorktreeRoot "tests\fixtures\db_source_strings.json"

    # Query DISTINCT source values from the DB (-t = tuples only, -A = unaligned).
    $psqlArgs = @(
        $DatabaseUrl
        "--tuples-only"
        "--no-align"
        "--command"
        "SELECT DISTINCT source FROM listings ORDER BY source"
    )
    $psqlOutput = & psql @psqlArgs 2>&1
    $psqlExit   = $LASTEXITCODE

    if ($psqlExit -ne 0) {
        $step1Notes = "psql exited $psqlExit - DB may be unreachable. Check DATABASE_URL and that the dev stack is running (docker compose up)."
        Write-Warning $step1Notes
    }
    else {
        # Parse lines into a sorted string array, filtering blank lines.
        $dbSources = @($psqlOutput |
            Where-Object { ($_ -is [string]) -and ($_.Trim() -ne "") } |
            Sort-Object)

        # Read current fixture.
        $fixtureJson    = Get-Content -Path $fixtureFile -Raw -Encoding UTF8
        $fixtureSources = @($fixtureJson | ConvertFrom-Json)

        # Compare using Compare-Object (symmetric diff).
        $diff = Compare-Object -ReferenceObject $fixtureSources -DifferenceObject $dbSources

        if ($null -eq $diff) {
            $step1Result = "PASS"
            $step1Notes  = "no drift ($($fixtureSources.Count) keys)"
            Write-Verbose "Source strings match fixture; no update needed."
        }
        else {
            $added   = @($diff | Where-Object { $_.SideIndicator -eq "=>" } | ForEach-Object { $_.InputObject })
            $removed = @($diff | Where-Object { $_.SideIndicator -eq "<=" } | ForEach-Object { $_.InputObject })

            Write-Host "  Drift detected - updating fixture:"
            if ($added.Count   -gt 0) { Write-Host "  Added  : $($added   -join ', ')" -ForegroundColor Green  }
            if ($removed.Count -gt 0) { Write-Host "  Removed: $($removed -join ', ')" -ForegroundColor Yellow }

            # Write refreshed fixture: sorted JSON array with trailing newline.
            $newJson = (ConvertTo-Json @($dbSources | Sort-Object)) + "`n"
            Set-Content -Path $fixtureFile -Value $newJson -Encoding UTF8

            $step1Result = "PASS"
            $step1Notes  = "$($added.Count) key(s) added, $($removed.Count) key(s) removed - fixture updated"
        }
    }
}
catch {
    $step1Notes = "Unexpected error: $_"
    Write-Warning "Step 1 failed: $_"
}

Add-Result -Step "1. Source-string fixture refresh" -Result $step1Result -Notes $step1Notes

# ---------------------------------------------------------------------------
# Step 2 - Pre-aggregator baseline
# ---------------------------------------------------------------------------

Write-StepHeader "Step 2 - Pre-aggregator baseline"

$step2Result = "FAIL"
$step2Notes  = ""
$preBaseline = Join-Path $WorktreeRoot "docs\baselines\2026-04-27-pre-aggregator.json"

try {
    $captureScript = Join-Path $WorktreeRoot "scripts\capture_ingest_baseline.py"
    $env:DATABASE_URL = $DatabaseUrl

    $captureArgs = @(
        $captureScript
        "--label"
        "pre-aggregator"
        "--output"
        $preBaseline
    )
    Write-Verbose "Running: python $captureArgs"
    & python @captureArgs 2>&1 | Write-Verbose
    $captureExit = $LASTEXITCODE

    if ($captureExit -ne 0) {
        $step2Notes = "capture_ingest_baseline.py exited $captureExit - check DATABASE_URL and that psycopg2 is installed"
    }
    elseif (-not (Test-Path $preBaseline)) {
        $step2Notes = "Output file not created: $preBaseline"
    }
    else {
        $fileSize = (Get-Item $preBaseline).Length

        if ($fileSize -le 200) {
            $step2Notes = "Output file looks like a stub ($fileSize bytes <= 200); capture may have failed silently"
        }
        else {
            $parsed = Get-Content $preBaseline -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
            $hasRequiredKeys = (
                $parsed -and
                ($null -ne ($parsed | Get-Member -Name "captured_at" -MemberType NoteProperty -ErrorAction SilentlyContinue)) -and
                ($null -ne ($parsed | Get-Member -Name "label"       -MemberType NoteProperty -ErrorAction SilentlyContinue)) -and
                ($null -ne ($parsed | Get-Member -Name "sources"     -MemberType NoteProperty -ErrorAction SilentlyContinue))
            )

            if (-not $hasRequiredKeys) {
                $step2Notes = "Output JSON is missing expected top-level keys (captured_at, label, sources)"
            }
            else {
                $kb = [math]::Round($fileSize / 1KB, 1)
                $step2Result = "PASS"
                $step2Notes  = "$preBaseline ($kb KB)"
            }
        }
    }
}
catch {
    $step2Notes = "Unexpected error: $_"
    Write-Warning "Step 2 failed: $_"
}

Add-Result -Step "2. Pre-aggregator baseline" -Result $step2Result -Notes $step2Notes

# ---------------------------------------------------------------------------
# Step 3 - Live ingest smoke run
# ---------------------------------------------------------------------------

Write-StepHeader "Step 3 - Live ingest smoke run"

$step3Result = "SKIPPED"
$step3Notes  = "skipped via -SkipSmoke"
$smokeLog    = ""

if ($SkipSmoke) {
    Write-Host "  Skipping step 3 (-SkipSmoke set)" -ForegroundColor Yellow
}
else {
    $step3Result = "FAIL"
    $step3Notes  = ""

    try {
        # Ensure logs/ directory exists.
        $logsDir = Join-Path $WorktreeRoot "logs"
        if (-not (Test-Path $logsDir)) {
            New-Item -ItemType Directory -Path $logsDir | Out-Null
            Write-Verbose "Created logs/ directory at $logsDir"
        }

        $ts       = Get-UtcTimestamp
        $smokeLog = Join-Path $logsDir "phase-a-pre-b-smoke-$ts.log"

        # Set JOB_AGGREGATOR_SOURCES for the duration of this step only.
        $prevAggSources             = $env:JOB_AGGREGATOR_SOURCES
        $env:JOB_AGGREGATOR_SOURCES = "arbeitnow"
        $env:DATABASE_URL           = $DatabaseUrl

        Write-Host "  Running: python ingest.py --hours 24 --verbose"
        Write-Host "  Log    : $smokeLog"
        Write-Host "  (This may take several minutes while scraping and LLM scoring run)"

        $ingestScript = Join-Path $WorktreeRoot "ingest.py"
        & python $ingestScript --hours 24 --verbose 2>&1 | Tee-Object -FilePath $smokeLog
        $ingestExitCode = $LASTEXITCODE

        # Restore env var.
        $env:JOB_AGGREGATOR_SOURCES = $prevAggSources

        if ($ingestExitCode -ne 0) {
            $step3Notes = "ingest.py exited $ingestExitCode - see $smokeLog"
            Write-Warning "ingest.py exited $ingestExitCode"
        }
        else {
            # Verify two routing markers in the log:
            #
            # Marker 1 - JobAggregatorProvider
            #   Matches the line logged by ingest.py get_sources_for_run() at
            #   the point where it reads JOB_AGGREGATOR_SOURCES and routes
            #   arbeitnow through the aggregator bridge. The log line format is:
            #     "JOB_AGGREGATOR_SOURCES=... routing ['arbeitnow'] through JobAggregatorProvider"
            #   Searching for "JobAggregatorProvider" is sufficient and avoids
            #   matching on the em-dash character in the format string.
            #
            # Marker 2 - LegacyInTreeProvider (indirect evidence)
            #   There is no explicit "LegacyInTreeProvider" log line at runtime.
            #   The evidence that the other 9 sources ran through the legacy path
            #   is the presence of "Fetching from source" log lines for sources
            #   other than arbeitnow; those sources can only reach that log line
            #   via LegacyInTreeProvider when JOB_AGGREGATOR_SOURCES=arbeitnow.
            #
            $aggMarker    = Select-String -Path $smokeLog -Pattern "JobAggregatorProvider" -Quiet
            $legacyMarker = Select-String -Path $smokeLog -Pattern "Fetching from source" -Quiet

            if (-not $aggMarker -and -not $legacyMarker) {
                $step3Notes = "Neither JobAggregatorProvider nor 'Fetching from source' found in log. See $smokeLog"
            }
            elseif (-not $aggMarker) {
                $step3Notes = "JobAggregatorProvider marker missing - arbeitnow may not have routed through the bridge. Check JOB_AGGREGATOR_SOURCES handling. See $smokeLog"
            }
            elseif (-not $legacyMarker) {
                $step3Notes = "'Fetching from source' marker missing - no sources appear to have been fetched via the legacy path. See $smokeLog"
            }
            else {
                $step3Result = "PASS"
                $step3Notes  = "JobAggregatorProvider + LegacyInTreeProvider markers seen; log: $(Split-Path $smokeLog -Leaf)"
            }
        }

        if ($step3Result -eq "FAIL" -and $smokeLog -and (Test-Path $smokeLog)) {
            Write-Warning "Step 3 failed. Last 30 lines of log:"
            Get-Content $smokeLog -Tail 30 | ForEach-Object { Write-Warning $_ }
        }
    }
    catch {
        $step3Notes = "Unexpected error: $_"
        Write-Warning "Step 3 failed: $_"
    }
}

Add-Result -Step "3. Live ingest smoke run" -Result $step3Result -Notes $step3Notes

# ---------------------------------------------------------------------------
# Step 4 - Post-aggregator baseline + delta
# ---------------------------------------------------------------------------

Write-StepHeader "Step 4 - Post-aggregator baseline + delta"

$step4Result  = "FAIL"
$step4Notes   = ""
$postBaseline = Join-Path $WorktreeRoot "docs\baselines\2026-04-27-post-aggregator-smoke.json"

try {
    $captureScript = Join-Path $WorktreeRoot "scripts\capture_ingest_baseline.py"
    $env:DATABASE_URL = $DatabaseUrl

    $captureArgs = @(
        $captureScript
        "--label"
        "post-aggregator"
        "--output"
        $postBaseline
    )
    Write-Verbose "Running: python $captureArgs"
    & python @captureArgs 2>&1 | Write-Verbose
    $captureExit = $LASTEXITCODE

    if ($captureExit -ne 0) {
        $step4Notes = "capture_ingest_baseline.py exited $captureExit for post-aggregator baseline"
    }
    elseif (-not (Test-Path $postBaseline)) {
        $step4Notes = "Post-aggregator baseline file not created: $postBaseline"
    }
    else {
        $postParsed = Get-Content $postBaseline -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue

        if (-not $postParsed) {
            $step4Notes = "Post-aggregator baseline is not valid JSON"
        }
        else {
            # Compute total row-count delta between pre and post baselines.
            $deltaMsg = "delta: (pre-baseline not available or invalid)"

            if (Test-Path $preBaseline) {
                $preParsed = Get-Content $preBaseline -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue

                if ($preParsed -and $preParsed.sources -and $postParsed.sources) {
                    $preTotal  = ($preParsed.sources.PSObject.Properties  |
                                  ForEach-Object { $_.Value.count } |
                                  Measure-Object -Sum).Sum
                    $postTotal = ($postParsed.sources.PSObject.Properties |
                                  ForEach-Object { $_.Value.count } |
                                  Measure-Object -Sum).Sum
                    $delta    = $postTotal - $preTotal
                    $sign     = if ($delta -ge 0) { "+" } else { "" }
                    $deltaMsg = "delta: $sign$delta rows (pre=$preTotal, post=$postTotal)"
                    Write-Host "  Row-count delta: $deltaMsg"
                }
            }

            $step4Result = "PASS"
            $step4Notes  = $deltaMsg
        }
    }
}
catch {
    $step4Notes = "Unexpected error: $_"
    Write-Warning "Step 4 failed: $_"
}

Add-Result -Step "4. Post-aggregator baseline" -Result $step4Result -Notes $step4Notes

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "================================================================" -ForegroundColor White
Write-Host "SUMMARY" -ForegroundColor White
Write-Host "================================================================" -ForegroundColor White

$summary | Format-Table -AutoSize @{
    Label = "Step"; Expression = { $_.Step }; Width = 40
}, @{
    Label = "Result"; Expression = { $_.Result }; Width = 8
}, @{
    Label = "Notes"; Expression = { $_.Notes }
}

# Color-coded reprint (Format-Table strips colors).
Write-Host ""
foreach ($row in $summary) {
    $color = switch ($row.Result) {
        "PASS"    { "Green"  }
        "FAIL"    { "Red"    }
        "SKIPPED" { "Yellow" }
    }
    Write-Host "  [$($row.Result)]  $($row.Step)" -ForegroundColor $color
    if ($row.Notes) {
        Write-Host "         $($row.Notes)" -ForegroundColor DarkGray
    }
}
Write-Host ""

# Exit non-zero if any non-skipped step failed.
$anyFail = @($summary | Where-Object { $_.Result -eq "FAIL" })
if ($anyFail.Count -gt 0) {
    Write-Host "One or more steps FAILED. See notes above." -ForegroundColor Red
    exit 1
}
else {
    Write-Host "All non-skipped steps PASSED." -ForegroundColor Green
    exit 0
}
