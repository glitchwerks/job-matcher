#Requires -Version 5.1
<#
.SYNOPSIS
    Removes the Job Matcher NSSM service and Windows scheduled task.
.DESCRIPTION
    Stops and removes the JobMatcher waitress service registered by setup.ps1,
    and unregisters the JobMatcherIngest scheduled task.

    Optionally removes system environment variables. Does NOT delete the data
    directory or database.
.EXAMPLE
    .\teardown.ps1
.NOTES
    Must be run as Administrator.
    Data and database files are preserved.
#>

[CmdletBinding(SupportsShouldProcess)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$ServiceName = 'JobMatcher'
$TaskName    = 'JobMatcherIngest'
$EnvVarNames = @('DB_PATH', 'ADZUNA_APP_ID', 'ADZUNA_APP_KEY', 'ANTHROPIC_API_KEY', 'FLASK_DEBUG')

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Banner {
    [CmdletBinding()]
    param([string]$Text)
    $border = '=' * 60
    Write-Host ''
    Write-Host $border -ForegroundColor Cyan
    Write-Host ("  {0}" -f $Text) -ForegroundColor Cyan
    Write-Host $border -ForegroundColor Cyan
    Write-Host ''
}

function Write-Step {
    [CmdletBinding()]
    param([string]$Text)
    Write-Host ("[TEAR ] {0}" -f $Text) -ForegroundColor Yellow
}

function Write-Ok {
    [CmdletBinding()]
    param([string]$Text)
    Write-Host ("[  OK ] {0}" -f $Text) -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Step 0 - Administrator check
# ---------------------------------------------------------------------------
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Error 'This script must be run as Administrator. Right-click PowerShell and choose "Run as Administrator".'
    exit 1
}

# ---------------------------------------------------------------------------
# Step 1 - Confirmation prompt
# ---------------------------------------------------------------------------
Write-Banner 'Job Matcher -- Teardown'

Write-Host 'This will remove the JobMatcher service and scheduled task.' -ForegroundColor Yellow
Write-Host 'Your data directory and database will NOT be deleted.' -ForegroundColor Yellow
Write-Host ''
$confirm = Read-Host -Prompt 'Continue? [y/N]'

if ($confirm -notmatch '^[Yy]$') {
    Write-Host 'Teardown cancelled.' -ForegroundColor Cyan
    exit 0
}

# ---------------------------------------------------------------------------
# Step 2 - Stop and remove NSSM service
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Stopping and removing service: $ServiceName"

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    & nssm stop   $ServiceName 2>$null
    Start-Sleep -Seconds 2
    & nssm remove $ServiceName confirm 2>$null
    Write-Ok "Service '$ServiceName' removed."
}
else {
    Write-Host "  Service '$ServiceName' was not found - nothing to remove." -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Step 3 - Remove scheduled task
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Removing scheduled task: $TaskName"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Scheduled task '$TaskName' removed."
}
else {
    Write-Host "  Task '$TaskName' was not found - nothing to remove." -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Step 4 - Remove Windows Firewall rule
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Removing firewall rule: Job Matcher Web UI"
Remove-NetFirewallRule -DisplayName 'Job Matcher Web UI' -ErrorAction SilentlyContinue
Write-Ok 'Firewall rule removed (or was not present)'

# ---------------------------------------------------------------------------
# Step 5 - Optionally remove environment variables
# ---------------------------------------------------------------------------
Write-Host ''
$removeEnv = Read-Host -Prompt 'Remove system environment variables (DB_PATH, API keys, FLASK_DEBUG)? [y/N]'

if ($removeEnv -match '^[Yy]$') {
    Write-Step 'Removing system environment variables...'
    foreach ($varName in $EnvVarNames) {
        $existing = [Environment]::GetEnvironmentVariable($varName, 'Machine')
        if ($null -ne $existing) {
            [Environment]::SetEnvironmentVariable($varName, $null, 'Machine')
            Write-Ok "Removed: $varName"
        }
        else {
            Write-Host "  Not set: $varName" -ForegroundColor Gray
        }
    }
}
else {
    Write-Host '  Environment variables left in place.' -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Step 6 - Note about data directory
# ---------------------------------------------------------------------------
Write-Host ''
$dbPath = [Environment]::GetEnvironmentVariable('DB_PATH', 'Machine')
if ([string]::IsNullOrWhiteSpace($dbPath)) {
    $dbPath = 'C:\ProgramData\JobMatcher\data\jobs.db'
}
$dataDir = Split-Path -Path $dbPath -Parent

Write-Banner 'Teardown Complete'
Write-Host 'Data files were NOT removed.' -ForegroundColor Cyan
Write-Host "  Data directory : $dataDir"
Write-Host "  Database       : $dbPath"
Write-Host ''
Write-Host 'Delete these manually if you no longer need them.' -ForegroundColor Yellow
Write-Host ''
