#Requires -Version 5.1
<#
.SYNOPSIS
    Interactive setup script for the Job Matcher native deployment.
.DESCRIPTION
    Registers the JobMatcher gunicorn service via NSSM and a daily ingest
    scheduled task. Must be run as Administrator.

    Prompts for Adzuna credentials and data directory, sets system environment
    variables (Machine scope), copies keys.json from the example template,
    hardens its ACL to the current user, installs the NSSM service, and
    registers the Windows Task Scheduler task.

    LLM provider API keys (Anthropic, OpenAI, Gemini, etc.) are managed
    through the /settings UI and stored in keys.json — they are NOT set as
    environment variables by this script.
.EXAMPLE
    .\setup.ps1
.NOTES
    Requires NSSM (https://nssm.cc/download) on PATH.
    Requires gunicorn installed in the project venv before running.
#>

[CmdletBinding(SupportsShouldProcess)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$ProjectRoot  = 'C:\Apps\job_matcher'
$VenvScripts  = Join-Path -Path $ProjectRoot -ChildPath 'venv\Scripts'
$ServiceName  = 'JobMatcher'
$TaskName     = 'JobMatcherIngest'

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Banner {
    [CmdletBinding()]
    param([string]$Text)
    $width = 60
    $border = '=' * $width
    Write-Host ''
    Write-Host $border -ForegroundColor Cyan
    Write-Host ("  {0}" -f $Text) -ForegroundColor Cyan
    Write-Host $border -ForegroundColor Cyan
    Write-Host ''
}

function Write-Step {
    [CmdletBinding()]
    param([string]$Text)
    Write-Host ("[SETUP] {0}" -f $Text) -ForegroundColor Yellow
}

function Write-Ok {
    [CmdletBinding()]
    param([string]$Text)
    Write-Host ("[  OK ] {0}" -f $Text) -ForegroundColor Green
}

function Write-Fail {
    [CmdletBinding()]
    param([string]$Text)
    Write-Host ("[ FAIL] {0}" -f $Text) -ForegroundColor Red
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
# Step 1 - Banner
# ---------------------------------------------------------------------------
Write-Banner 'Job Matcher -- Native Deployment Setup'

# ---------------------------------------------------------------------------
# Step 2 - Prerequisite checks
# ---------------------------------------------------------------------------
Write-Step 'Checking prerequisites...'

# Python on PATH
try {
    $pythonVersion = & python --version 2>&1
    Write-Ok "Python found: $pythonVersion"
}
catch {
    Write-Fail 'Python is not on PATH.'
    Write-Error 'Install Python and ensure it is on your system PATH, then re-run this script.'
    exit 1
}

# gunicorn.exe in venv
$gunicornExe = Join-Path -Path $VenvScripts -ChildPath 'gunicorn.exe'
if (-not (Test-Path -Path $gunicornExe -PathType Leaf)) {
    Write-Fail "gunicorn.exe not found at: $gunicornExe"
    Write-Host ''
    Write-Host 'Create and populate the venv before running setup:' -ForegroundColor Yellow
    Write-Host "  cd `"$ProjectRoot`""
    Write-Host '  python -m venv venv'
    Write-Host '  venv\Scripts\pip install -r requirements.txt'
    Write-Host ''
    exit 1
}
Write-Ok "gunicorn.exe found at: $gunicornExe"

# NSSM on PATH
$nssm = Get-Command -Name 'nssm' -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Fail 'nssm is not on PATH.'
    Write-Host ''
    Write-Host 'Download NSSM from: https://nssm.cc/download' -ForegroundColor Yellow
    Write-Host 'Extract nssm.exe to a directory on your PATH (e.g. C:\Windows\System32).'
    Write-Host ''
    exit 1
}
Write-Ok "nssm found at: $($nssm.Source)"

# ---------------------------------------------------------------------------
# Step 3 - Prompt for configuration
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step 'Configuration prompts (press Enter to accept defaults)...'
Write-Host ''

$dataDir = Read-Host -Prompt 'Data directory [C:\ProgramData\JobMatcher\data]'
if ([string]::IsNullOrWhiteSpace($dataDir)) {
    $dataDir = 'C:\ProgramData\JobMatcher\data'
}

$adzunaAppId = Read-Host -Prompt 'Adzuna App ID'
if ([string]::IsNullOrWhiteSpace($adzunaAppId)) {
    Write-Error 'Adzuna App ID is required.'
    exit 1
}

$adzunaAppKey = Read-Host -Prompt 'Adzuna App Key'
if ([string]::IsNullOrWhiteSpace($adzunaAppKey)) {
    Write-Error 'Adzuna App Key is required.'
    exit 1
}

$ingestTime = Read-Host -Prompt 'Daily ingest time (24h HH:MM) [06:00]'
if ([string]::IsNullOrWhiteSpace($ingestTime)) {
    $ingestTime = '06:00'
}
if ($ingestTime -notmatch '^\d{2}:\d{2}$') {
    Write-Error "Invalid time format '$ingestTime'. Expected HH:MM (e.g. 06:00)."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 4 - Create data directory
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Creating data directory: $dataDir"

$logsDir = Join-Path -Path $dataDir -ChildPath 'logs'
$null    = New-Item -Path $dataDir  -ItemType Directory -Force
$null    = New-Item -Path $logsDir  -ItemType Directory -Force
Write-Ok "Directory ready: $dataDir"
Write-Ok "Logs directory:  $logsDir"

# ---------------------------------------------------------------------------
# Step 5 - Set up keys.json
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step 'Setting up keys.json...'

$keysPath        = Join-Path -Path $ProjectRoot -ChildPath 'keys.json'
$keysExamplePath = Join-Path -Path $ProjectRoot -ChildPath 'keys.example.json'

if (-not (Test-Path -Path $keysPath -PathType Leaf)) {
    if (Test-Path -Path $keysExamplePath -PathType Leaf) {
        Copy-Item -Path $keysExamplePath -Destination $keysPath
        Write-Ok 'keys.json created from example — configure API keys at http://localhost:5000/settings'
    }
    else {
        Write-Host '  keys.example.json not found — skipping keys.json creation.' -ForegroundColor Yellow
        Write-Host '  Create keys.json manually in the project root before starting the service.' -ForegroundColor Yellow
    }
}
else {
    Write-Ok 'keys.json already present — skipping'
}

# ---------------------------------------------------------------------------
# Step 6 - Harden keys.json ACL
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step 'Hardening keys.json permissions...'

if (Test-Path -Path $keysPath -PathType Leaf) {
    # Restrict keys.json to the current user only (removes inherited permissions)
    $acl = Get-Acl $keysPath
    $acl.SetAccessRuleProtection($true, $false)   # break inheritance, don't copy existing
    $userRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
        'FullControl',
        'Allow'
    )
    $acl.SetAccessRule($userRule)
    Set-Acl $keysPath $acl
    Write-Ok 'keys.json permissions restricted to current user'
}
else {
    Write-Host '  keys.json not found — skipping ACL step.' -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Step 7 - Set system environment variables
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step 'Setting system environment variables (Machine scope)...'

$dbPath = Join-Path -Path $dataDir -ChildPath 'jobs.db'

$envVars = [ordered]@{
    DB_PATH        = $dbPath
    ADZUNA_APP_ID  = $adzunaAppId
    ADZUNA_APP_KEY = $adzunaAppKey
    FLASK_DEBUG    = '0'
}

foreach ($key in $envVars.Keys) {
    [Environment]::SetEnvironmentVariable($key, $envVars[$key], 'Machine')
    if ($key -eq 'ADZUNA_APP_KEY') {
        Write-Ok "Set $key = $($envVars[$key].Substring(0, [Math]::Min(4, $envVars[$key].Length)))****"
    }
    else {
        Write-Ok "Set $key = $($envVars[$key])"
    }
}

# ---------------------------------------------------------------------------
# Step 8 - Register NSSM service
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Registering NSSM service: $ServiceName"

# Remove existing service if present
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host "  Existing service found - stopping and removing..." -ForegroundColor Yellow
    & nssm stop   $ServiceName 2>$null
    & nssm remove $ServiceName confirm 2>$null
    Write-Ok 'Existing service removed.'
}

$webLog   = Join-Path -Path $logsDir -ChildPath 'web.log'
$errorLog = Join-Path -Path $logsDir -ChildPath 'web-error.log'

try {
    & nssm install $ServiceName $gunicornExe
    & nssm set $ServiceName AppParameters   "app:app --bind 0.0.0.0:5000 --workers 2"
    & nssm set $ServiceName AppDirectory    $ProjectRoot
    & nssm set $ServiceName Start           SERVICE_AUTO_START
    & nssm set $ServiceName AppStdout       $webLog
    & nssm set $ServiceName AppStderr       $errorLog
    Write-Ok 'Service configured.'
}
catch {
    Write-Fail "Failed to configure NSSM service: $_"
    exit 1
}

try {
    & nssm start $ServiceName
    Start-Sleep -Seconds 2
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') {
        Write-Ok "Service status: $($svc.Status)"
    }
    else {
        $status = if ($svc) { $svc.Status } else { 'Not found' }
        Write-Host "  Service status: $status" -ForegroundColor Yellow
        Write-Host "  Check logs at: $logsDir" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "  Could not confirm service start: $_" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Step 9 - Register scheduled task
# ---------------------------------------------------------------------------
Write-Host ''
Write-Step "Registering scheduled task: $TaskName"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$pythonExe = Join-Path -Path $VenvScripts -ChildPath 'python.exe'

$timeParts      = $ingestTime -split ':'
$triggerHour    = [int]$timeParts[0]
$triggerMinute  = [int]$timeParts[1]
$triggerAt      = (Get-Date).Date.AddHours($triggerHour).AddMinutes($triggerMinute)

try {
    $actionParams = @{
        Execute          = $pythonExe
        Argument         = 'ingest.py --hours 25'
        WorkingDirectory = $ProjectRoot
    }
    $action = New-ScheduledTaskAction @actionParams

    $trigger = New-ScheduledTaskTrigger -Daily -At $triggerAt

    $settingsParams = @{
        ExecutionTimeLimit   = (New-TimeSpan -Hours 2)
        RestartCount         = 1
        RestartInterval      = (New-TimeSpan -Minutes 10)
        StartWhenAvailable   = $true
    }
    $settings = New-ScheduledTaskSettingsSet @settingsParams

    $principalParams = @{
        UserId    = 'SYSTEM'
        RunLevel  = 'Highest'
        LogonType = 'ServiceAccount'
    }
    $principal = New-ScheduledTaskPrincipal @principalParams

    $registerParams = @{
        TaskName    = $TaskName
        Action      = $action
        Trigger     = $trigger
        Settings    = $settings
        Principal   = $principal
        Description = 'Daily Job Matcher ingest: fetches and scores listings via Adzuna + Anthropic'
        Force       = $true
    }
    Register-ScheduledTask @registerParams | Out-Null

    Write-Ok "Scheduled task registered: $TaskName"
    Write-Ok "Runs daily at $ingestTime as SYSTEM"
}
catch {
    Write-Fail "Failed to register scheduled task: $_"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 10 - Footer
# ---------------------------------------------------------------------------
Write-Host ''
Write-Banner 'Setup Complete'
Write-Host 'Next steps:' -ForegroundColor Cyan
Write-Host "  1. Configure API keys:  http://localhost:5000/settings"
Write-Host "  2. Open the feed:       http://localhost:5000"
Write-Host "  3. Check status:        .\scripts\status.ps1"
Write-Host "  4. View web logs:       $logsDir\web.log"
Write-Host "  5. Force ingest now:    $pythonExe ingest.py --hours 25"
Write-Host ''
Write-Host 'To remove everything cleanly, run: .\scripts\teardown.ps1' -ForegroundColor Yellow
Write-Host ''

# ===========================================================================
# GITHUB ACTIONS SELF-HOSTED RUNNER SETUP (one-time, manual)
# ===========================================================================
# Complete these steps on this server after running this setup script,
# to enable automatic deployment via GitHub Actions push-to-main.
#
# 1. Go to: https://github.com/cbeaulieu-gt/job-matcher-ui/settings/actions/runners
#    Click "New self-hosted runner" -> select Windows -> follow the download
#    and configure instructions.
#
# 2. When prompted for runner labels during configuration, add: self-hosted
#    (the default). No additional labels are required.
#
# 3. Install the runner as a Windows service so it survives reboots:
#       .\svc.ps1 install
#       .\svc.ps1 start
#
# 4. Verify the runner service account (usually "SYSTEM" or a local admin)
#    has permission to run `nssm restart JobMatcher`. Test with:
#       nssm restart JobMatcher
#    If it fails due to permissions, configure the runner service to run as
#    a local administrator account via services.msc.
#
# 5. Confirm outbound HTTPS to github.com is not blocked by firewall/proxy.
#
# Once registered, pushing to main will automatically:
#   git pull -> pip install -r requirements.txt -> nssm restart JobMatcher
# ===========================================================================
