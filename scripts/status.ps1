#Requires -Version 5.1
<#
.SYNOPSIS
    Shows the current status of the Job Matcher deployment.
.DESCRIPTION
    Reports on four areas:
      1. NSSM service status (JobMatcher)
      2. Scheduled task status (JobMatcherIngest) - last/next run times
      3. Database statistics - total listings, scored count, last fetch time
      4. Environment variable values (API keys masked)

    Read-only. Does not require Administrator.
.EXAMPLE
    .\status.ps1
.NOTES
    Requires the project venv to exist for database queries.
    Uses nssm if available; falls back to Get-Service.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$ServiceName   = 'JobMatcher'
$TaskName      = 'JobMatcherIngest'
$ProjectRoot   = 'C:\Apps\job_matcher'
$VenvPython    = Join-Path -Path $ProjectRoot -ChildPath 'venv\Scripts\python.exe'
$DefaultDbPath = 'C:\ProgramData\JobMatcher\data\jobs.db'

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-SectionHeader {
    [CmdletBinding()]
    param([string]$Title)
    Write-Host ''
    Write-Host ('--- {0} ' -f $Title).PadRight(60, '-') -ForegroundColor Cyan
}

function Write-LabelValue {
    [CmdletBinding()]
    param(
        [string]$Label,
        [string]$Value,
        [string]$Color = 'White'
    )
    Write-Host ('  {0,-24} ' -f ($Label + ':')) -NoNewline
    Write-Host $Value -ForegroundColor $Color
}

function Get-MaskedSecret {
    [CmdletBinding()]
    [OutputType([string])]
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return '(not set)'
    }
    $prefix = $Value.Substring(0, [Math]::Min(4, $Value.Length))
    return "{0}****" -f $prefix
}

# ---------------------------------------------------------------------------
# Section 1 - Service status
# ---------------------------------------------------------------------------
Write-SectionHeader 'Service Status'

$nssmAvailable = Get-Command -Name 'nssm' -ErrorAction SilentlyContinue

try {
    if ($nssmAvailable) {
        $nssmOutput = & nssm status $ServiceName 2>&1
        $statusText = ($nssmOutput | Out-String).Trim()

        if ($statusText -match 'SERVICE_RUNNING') {
            Write-LabelValue -Label $ServiceName -Value 'RUNNING' -Color 'Green'
        }
        elseif ($statusText -match 'SERVICE_STOPPED') {
            Write-LabelValue -Label $ServiceName -Value 'STOPPED' -Color 'Red'
        }
        else {
            Write-LabelValue -Label $ServiceName -Value $statusText -Color 'Yellow'
        }
    }
    else {
        $svc = Get-Service -Name $ServiceName -ErrorAction Stop
        $color = if ($svc.Status -eq 'Running') { 'Green' } else { 'Red' }
        Write-LabelValue -Label $ServiceName -Value $svc.Status.ToString() -Color $color
    }
}
catch {
    Write-LabelValue -Label $ServiceName -Value 'Not installed' -Color 'Yellow'
}

# ---------------------------------------------------------------------------
# Section 2 - Scheduled task status
# ---------------------------------------------------------------------------
Write-SectionHeader 'Scheduled Task Status'

try {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop

    $lastRunTime   = if ($taskInfo.LastRunTime -and $taskInfo.LastRunTime.Year -gt 1900) {
        $taskInfo.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
    } else { 'Never' }

    $nextRunTime   = if ($taskInfo.NextRunTime -and $taskInfo.NextRunTime.Year -gt 1900) {
        $taskInfo.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')
    } else { 'Not scheduled' }

    $lastResult    = $taskInfo.LastTaskResult
    $resultColor   = if ($lastResult -eq 0) { 'Green' } elseif ($lastResult -eq 267011) { 'Gray' } else { 'Red' }
    $resultText    = if ($lastResult -eq 0) { '0 (Success)' } elseif ($lastResult -eq 267011) { "$lastResult (Never run)" } else { "$lastResult (Error)" }

    Write-LabelValue -Label 'Task'          -Value $TaskName
    Write-LabelValue -Label 'Last run'      -Value $lastRunTime
    Write-LabelValue -Label 'Last result'   -Value $resultText  -Color $resultColor
    Write-LabelValue -Label 'Next run'      -Value $nextRunTime
}
catch {
    Write-LabelValue -Label $TaskName -Value 'Not registered' -Color 'Yellow'
}

# ---------------------------------------------------------------------------
# Section 3 - Database statistics
# ---------------------------------------------------------------------------
Write-SectionHeader 'Database'

$dbPath = [Environment]::GetEnvironmentVariable('DB_PATH', 'Machine')
if ([string]::IsNullOrWhiteSpace($dbPath)) {
    $dbPath = $DefaultDbPath
}

Write-LabelValue -Label 'DB_PATH' -Value $dbPath

if (-not (Test-Path -Path $dbPath -PathType Leaf)) {
    Write-LabelValue -Label 'Status' -Value "Database not found at $dbPath" -Color 'Yellow'
}
elseif (-not (Test-Path -Path $VenvPython -PathType Leaf)) {
    Write-LabelValue -Label 'Status' -Value "venv python not found at $VenvPython" -Color 'Yellow'
    Write-LabelValue -Label 'Hint'   -Value 'Run setup.ps1 to create the venv with dependencies' -Color 'Gray'
}
else {
    $pyScript = @'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM listings")
total = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM listings WHERE score IS NOT NULL")
scored = cur.fetchone()[0]
cur.execute("SELECT MAX(fetched_at) FROM listings")
last_fetch = cur.fetchone()[0] or "Never"
conn.close()
print(total)
print(scored)
print(last_fetch)
'@

    try {
        $rawOutput = & $VenvPython -c $pyScript $dbPath 2>&1
        $lines = $rawOutput | Where-Object { $_ -ne '' }

        if ($lines.Count -ge 3) {
            Write-LabelValue -Label 'Total listings'   -Value $lines[0]
            Write-LabelValue -Label 'Scored listings'  -Value $lines[1]
            Write-LabelValue -Label 'Last fetch'       -Value $lines[2]
        }
        else {
            Write-LabelValue -Label 'Status' -Value 'Could not parse query output' -Color 'Yellow'
            Write-Host ($rawOutput | Out-String).Trim() -ForegroundColor Gray
        }
    }
    catch {
        Write-LabelValue -Label 'Status' -Value "Query failed: $_" -Color 'Red'
    }
}

# ---------------------------------------------------------------------------
# Section 4 - Environment variables
# ---------------------------------------------------------------------------
Write-SectionHeader 'Environment Variables'

$plainVars  = @('DB_PATH', 'FLASK_DEBUG')
$secretVars = @('ADZUNA_APP_ID', 'ADZUNA_APP_KEY', 'ANTHROPIC_API_KEY')

foreach ($varName in $plainVars) {
    $val = [Environment]::GetEnvironmentVariable($varName, 'Machine')
    $display = if ([string]::IsNullOrWhiteSpace($val)) { '(not set)' } else { $val }
    $color   = if ([string]::IsNullOrWhiteSpace($val)) { 'Yellow' } else { 'White' }
    Write-LabelValue -Label $varName -Value $display -Color $color
}

foreach ($varName in $secretVars) {
    $val     = [Environment]::GetEnvironmentVariable($varName, 'Machine')
    $display = Get-MaskedSecret -Value $val
    $color   = if ($display -eq '(not set)') { 'Yellow' } else { 'Gray' }
    Write-LabelValue -Label $varName -Value $display -Color $color
}

Write-Host ''
