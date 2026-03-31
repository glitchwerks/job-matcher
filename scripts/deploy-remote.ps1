#Requires -Version 5.1
<#
.SYNOPSIS
    Deploys Job Matcher project files to a remote Windows Server via PowerShell Remoting (WinRM).

.DESCRIPTION
    Copies the project source tree to a remote machine, excluding runtime artifacts
    (venv, jobs.db, __pycache__, .git, data).

    The config/ directory is handled specially: only *.example.json files are copied;
    runtime files (config.json, keys.json, profile.json, providers.json) are never
    copied to the server so production credentials are never overwritten.

    After copying, ensures a Python virtual environment exists on the remote machine
    and installs requirements if the venv is new.

    Checks whether NSSM is available on the remote machine and warns if not.

    Because setup.ps1 uses Read-Host for interactive prompts, it cannot be driven
    non-interactively over a background PSSession. The default behaviour after file
    copy is to print Enter-PSSession instructions so the operator can complete setup
    interactively. If -RunSetup is specified the script will attempt to invoke
    setup.ps1 via Invoke-Command, but that will fail for any Read-Host prompt --
    a warning is printed before attempting.

.PARAMETER ComputerName
    Hostname or IP address of the remote Windows Server.

.PARAMETER Credential
    PSCredential to use for the remote session. If omitted, Get-Credential is called
    interactively.

.PARAMETER RemoteProjectRoot
    Destination path on the remote machine. Defaults to C:\Apps\job_matcher.

.PARAMETER NssmPath
    Full path to nssm.exe on the remote machine when NSSM is not on PATH
    (e.g. C:\Tools\nssm\win64\nssm.exe). Optional -- a warning is printed if NSSM
    cannot be located either way.

.PARAMETER RunSetup
    If specified, attempt to invoke scripts\setup.ps1 on the remote machine via
    Invoke-Command after copying files. Because setup.ps1 uses Read-Host this will
    only succeed in a full interactive PSSession, not a background remoting call.
    A warning is printed explaining the limitation before the attempt is made.

.EXAMPLE
    .\deploy-remote.ps1 -ComputerName 192.168.1.50

    Prompts for credentials, copies files to C:\Apps\job_matcher on the remote
    machine, then prints Enter-PSSession instructions to complete setup.

.EXAMPLE
    $cred = Get-Credential
    .\deploy-remote.ps1 -ComputerName SERVER01 -Credential $cred -RemoteProjectRoot C:\JobMatcher

    Uses pre-supplied credentials and a custom destination path.

.EXAMPLE
    .\deploy-remote.ps1 -ComputerName 192.168.1.50 -NssmPath 'C:\Tools\nssm\win64\nssm.exe'

    Copies files and verifies that nssm.exe exists at the supplied path on the remote machine.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ComputerName,

    [Parameter()]
    [System.Management.Automation.PSCredential]
    [System.Management.Automation.Credential()]
    $Credential,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RemoteProjectRoot = 'C:\Apps\job_matcher',

    [Parameter()]
    [string]$NssmPath,

    [Parameter()]
    [switch]$RunSetup,

    [Parameter()]
    [ValidateSet('Default', 'Basic', 'Negotiate', 'NegotiateWithImplicitCredential', 'Credssp', 'Digest', 'Kerberos')]
    [string]$Authentication = 'Negotiate'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helper functions (mirrored from setup.ps1 -- do not import from there)
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
    Write-Host ("[DEPLOY] {0}" -f $Text) -ForegroundColor Yellow
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
# Resolve local project root (parent of the scripts\ directory)
# ---------------------------------------------------------------------------

$LocalProjectRoot = Split-Path -Path $PSScriptRoot -Parent

# ---------------------------------------------------------------------------
# Step 1 - Banner
# ---------------------------------------------------------------------------

Write-Banner 'Job Matcher -- Remote Deployment'
Write-Host ("  Local source : {0}" -f $LocalProjectRoot) -ForegroundColor Cyan
Write-Host ("  Remote target: {0} on {1}" -f $RemoteProjectRoot, $ComputerName) -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------------------
# Step 2 - Credential
# ---------------------------------------------------------------------------

if (-not $Credential) {
    Write-Step 'No credential supplied -- prompting via Get-Credential...'
    $Credential = Get-Credential -Message ("Enter credentials for {0}" -f $ComputerName)
}

# ---------------------------------------------------------------------------
# Step 3 - Test WinRM connectivity
# ---------------------------------------------------------------------------

Write-Step ("Testing WinRM connectivity to {0}..." -f $ComputerName)

$wsmanParams = @{
    ComputerName   = $ComputerName
    Authentication = $Authentication
    ErrorAction    = 'SilentlyContinue'
}
$wsmanResult = Test-WSMan @wsmanParams

if (-not $wsmanResult) {
    Write-Fail "WinRM is not reachable on $ComputerName."
    Write-Host ''
    Write-Host 'To enable WinRM on the remote server, run the following in an elevated PowerShell:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '    Enable-PSRemoting -Force' -ForegroundColor White
    Write-Host ("    Set-Item WSMan:\localhost\Client\TrustedHosts -Value `"<this machine's IP>`" -Force") -ForegroundColor White
    Write-Host ''
    Write-Host 'Then re-run this script.' -ForegroundColor Yellow
    exit 1
}

Write-Ok ("WinRM is reachable on {0} (ProductVersion: {1})" -f $ComputerName, $wsmanResult.ProductVersion)

# ---------------------------------------------------------------------------
# Step 4 - Establish PSSession
# ---------------------------------------------------------------------------

Write-Step ("Establishing PSSession to {0}..." -f $ComputerName)

$session = $null

try {
    $sessionParams = @{
        ComputerName   = $ComputerName
        Credential     = $Credential
        Authentication = $Authentication
        ErrorAction    = 'Stop'
    }
    $session = New-PSSession @sessionParams
    Write-Ok ("PSSession established (Id: {0})" -f $session.Id)
}
catch {
    Write-Fail ("Failed to create PSSession: {0}" -f $_)
    Write-Host ''
    Write-Host 'WinRM is reachable but the PSSession failed. Run this on YOUR LOCAL MACHINE in an elevated PowerShell:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host ("  Set-Item WSMan:\localhost\Client\TrustedHosts -Value `"{0}`" -Force" -f $ComputerName) -ForegroundColor White
    Write-Host ''
    Write-Host 'To append without overwriting existing entries:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host ('  $existing = (Get-Item WSMan:\localhost\Client\TrustedHosts).Value') -ForegroundColor White
    Write-Host ("  Set-Item WSMan:\localhost\Client\TrustedHosts -Value `"`$existing,{0}`" -Force" -f $ComputerName) -ForegroundColor White
    Write-Host ''
    Write-Host 'If the error mentions authentication or access denied, try -Authentication Basic:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host ("  .\deploy-remote.ps1 -ComputerName {0} -Authentication Basic" -f $ComputerName) -ForegroundColor White
    Write-Host ''
    Write-Host 'Basic auth requires it to be enabled on the remote machine (run in an elevated PowerShell there):' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  winrm set winrm/config/service/auth @{Basic="true"}' -ForegroundColor White
    Write-Host ''
    Write-Host 'Then re-run this script.' -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------------------
# Step 5 - Copy project files
# ---------------------------------------------------------------------------

Write-Step 'Copying project files to remote machine...'

# Build list of items to copy by gathering everything at the project root and
# filtering out excluded names. Copy-Item -ToSession -Recurse does not support
# -Exclude reliably for directory names, so we enumerate top-level items and
# exclude them explicitly, then copy them one by one.

$excludedNames = @(
    'venv',
    'jobs.db',
    'config.json',
    'keys.json',
    'profile.json',
    'providers.json',
    '__pycache__',
    '.git',
    'data',
    'config'   # handled specially below -- only *.example.json files are copied
)

$excludedExtensions = @('.pyc')

try {
    # Ensure the remote destination directory exists
    $null = Invoke-Command -Session $session -ScriptBlock {
        param($root)
        if (-not (Test-Path -Path $root)) {
            $null = New-Item -Path $root -ItemType Directory -Force
        }
    } -ArgumentList $RemoteProjectRoot

    # Collect items to copy (top-level files + directories, filtered)
    $itemsToCopy = Get-ChildItem -Path $LocalProjectRoot |
        Where-Object { $excludedNames -notcontains $_.Name }

    $filesCopied = 0

    foreach ($item in $itemsToCopy) {
        $copyParams = @{
            Path        = $item.FullName
            Destination = $RemoteProjectRoot
            ToSession   = $session
            Recurse     = $true
            Force       = $true
            ErrorAction = 'Stop'
        }
        Copy-Item @copyParams

        # Count files copied (recurse into directories for an accurate count)
        if ($item.PSIsContainer) {
            $filesCopied += (
                Get-ChildItem -Path $item.FullName -Recurse -File |
                    Where-Object { $excludedExtensions -notcontains $_.Extension }
            ).Count
        }
        else {
            if ($excludedExtensions -notcontains $item.Extension) {
                $filesCopied++
            }
        }
    }

    # Copy config/ selectively: only *.example.json files.
    # Runtime files (config.json, keys.json, profile.json, providers.json) are
    # excluded so production credentials on the server are never overwritten.
    $localConfigDir  = Join-Path -Path $LocalProjectRoot -ChildPath 'config'
    $remoteConfigDir = Join-Path -Path $RemoteProjectRoot -ChildPath 'config'

    if (Test-Path -Path $localConfigDir) {
        # Ensure the remote config/ directory exists
        $null = Invoke-Command -Session $session -ScriptBlock {
            param($dir)
            if (-not (Test-Path -Path $dir)) {
                $null = New-Item -Path $dir -ItemType Directory -Force
            }
        } -ArgumentList $remoteConfigDir

        $exampleFiles = Get-ChildItem -Path $localConfigDir -Filter '*.example.json' -File
        foreach ($exFile in $exampleFiles) {
            Copy-Item -Path $exFile.FullName -Destination $remoteConfigDir `
                      -ToSession $session -Force -ErrorAction Stop
            $filesCopied++
        }
    }

    Write-Ok ("{0} files copied to {1}" -f $filesCopied, $RemoteProjectRoot)
}
catch {
    Write-Fail ("File copy failed: {0}" -f $_)
    if ($session) { Remove-PSSession -Session $session -ErrorAction SilentlyContinue }
    exit 1
}

# ---------------------------------------------------------------------------
# Step 6 - Ensure Python venv exists on the remote machine
# ---------------------------------------------------------------------------

Write-Step 'Checking Python virtual environment on remote machine...'

try {
    Invoke-Command -Session $session -ScriptBlock {
        param($projectRoot)

        $venvPython = Join-Path -Path $projectRoot -ChildPath 'venv\Scripts\python.exe'

        if (Test-Path -Path $venvPython -PathType Leaf) {
            Write-Host ('[  OK ] venv already exists at: ' + $venvPython) -ForegroundColor Green
        }
        else {
            Write-Host ('[DEPLOY] venv not found -- creating virtual environment...') -ForegroundColor Yellow

            $origLocation = Get-Location
            Set-Location -Path $projectRoot

            try {
                & python -m venv venv
                if ($LASTEXITCODE -ne 0) {
                    throw "python -m venv exited with code $LASTEXITCODE"
                }
                Write-Host ('[  OK ] venv created.') -ForegroundColor Green

                Write-Host ('[DEPLOY] Installing requirements...') -ForegroundColor Yellow
                & venv\Scripts\pip install -r requirements.txt
                if ($LASTEXITCODE -ne 0) {
                    throw "pip install exited with code $LASTEXITCODE"
                }
                Write-Host ('[  OK ] Requirements installed.') -ForegroundColor Green
            }
            finally {
                Set-Location -Path $origLocation
            }
        }
    } -ArgumentList $RemoteProjectRoot
}
catch {
    Write-Fail ("venv setup failed on remote machine: {0}" -f $_)
    if ($session) { Remove-PSSession -Session $session -ErrorAction SilentlyContinue }
    exit 1
}

# ---------------------------------------------------------------------------
# Step 7 - Check for NSSM on the remote machine
# ---------------------------------------------------------------------------

Write-Step 'Checking for NSSM on remote machine...'

try {
    $nssmStatus = Invoke-Command -Session $session -ScriptBlock {
        param($nssmOverridePath)

        $found = Get-Command -Name 'nssm' -ErrorAction SilentlyContinue
        if ($found) {
            return @{ Found = $true; Path = $found.Source; Via = 'PATH' }
        }

        if (-not [string]::IsNullOrWhiteSpace($nssmOverridePath)) {
            if (Test-Path -Path $nssmOverridePath -PathType Leaf) {
                return @{ Found = $true; Path = $nssmOverridePath; Via = 'NssmPath' }
            }
            else {
                return @{ Found = $false; TriedPath = $nssmOverridePath }
            }
        }

        return @{ Found = $false; TriedPath = $null }
    } -ArgumentList $NssmPath

    if ($nssmStatus.Found) {
        Write-Ok ("NSSM found via {0}: {1}" -f $nssmStatus.Via, $nssmStatus.Path)
    }
    else {
        Write-Host ''
        if ($nssmStatus.TriedPath) {
            Write-Host ("[ WARN] NSSM not on PATH and not found at: {0}" -f $nssmStatus.TriedPath) -ForegroundColor Yellow
        }
        else {
            Write-Host '[ WARN] NSSM not found on PATH of remote machine.' -ForegroundColor Yellow
        }
        Write-Host '        Download NSSM from: https://nssm.cc/download' -ForegroundColor Yellow
        Write-Host '        Extract nssm.exe to a directory on the remote PATH before running setup.ps1.' -ForegroundColor Yellow
        Write-Host ''
        # Not fatal -- setup.ps1 will catch the missing NSSM with its own check.
    }
}
catch {
    # Non-fatal: warn and continue so the operator sees next steps regardless.
    Write-Host ("[ WARN] Could not check for NSSM on remote machine: {0}" -f $_) -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Step 8 - Optionally invoke setup.ps1 (with interactive-limitation warning)
# ---------------------------------------------------------------------------

if ($RunSetup) {
    Write-Host ''
    Write-Host '[ WARN] -RunSetup was specified.' -ForegroundColor Yellow
    Write-Host '        setup.ps1 uses Read-Host for interactive prompts (API keys, data dir, etc.).' -ForegroundColor Yellow
    Write-Host '        Read-Host does NOT work in a background Invoke-Command session -- those prompts' -ForegroundColor Yellow
    Write-Host '        will fail or hang. Use Enter-PSSession instead (instructions below).' -ForegroundColor Yellow
    Write-Host '        Attempting anyway as requested...' -ForegroundColor Yellow
    Write-Host ''

    try {
        $setupScript = Join-Path -Path $RemoteProjectRoot -ChildPath 'scripts\setup.ps1'
        Invoke-Command -Session $session -ScriptBlock {
            param($scriptPath, $projectRoot)
            Set-Location -Path $projectRoot
            & $scriptPath
        } -ArgumentList $setupScript, $RemoteProjectRoot
    }
    catch {
        Write-Fail ("setup.ps1 invocation failed (expected if Read-Host prompts are hit): {0}" -f $_)
        Write-Host '        Use the Enter-PSSession instructions below to complete setup interactively.' -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Step 9 - Close PSSession
# ---------------------------------------------------------------------------

if ($session) {
    Remove-PSSession -Session $session -ErrorAction SilentlyContinue
    Write-Ok 'PSSession closed.'
}

# ---------------------------------------------------------------------------
# Step 10 - Summary and next steps
# ---------------------------------------------------------------------------

Write-Banner 'Deployment Complete'

Write-Host 'Summary:' -ForegroundColor Cyan
Write-Host ("  Remote server      : {0}" -f $ComputerName)
Write-Host ("  Remote project root: {0}" -f $RemoteProjectRoot)
Write-Host ("  Files copied       : {0}" -f $filesCopied)
Write-Host ''
Write-Host 'Next steps -- connect to the remote server to complete setup:' -ForegroundColor Cyan
Write-Host ''
Write-Host ('  Enter-PSSession -ComputerName {0} -Credential (Get-Credential)' -f $ComputerName) -ForegroundColor White
Write-Host ('  cd {0}' -f $RemoteProjectRoot) -ForegroundColor White
Write-Host '  .\scripts\setup.ps1' -ForegroundColor White
Write-Host ''
Write-Host 'setup.ps1 will:' -ForegroundColor Cyan
Write-Host '  - Prompt for API keys (Adzuna, Anthropic) and data directory'
Write-Host '  - Set system environment variables (Machine scope)'
Write-Host '  - Register the JobMatcher NSSM service'
Write-Host '  - Register the JobMatcherIngest scheduled task'
Write-Host ''
Write-Host 'Prerequisites on the remote server before running setup.ps1:' -ForegroundColor Cyan
Write-Host '  - Python on PATH'
Write-Host '  - NSSM on PATH (https://nssm.cc/download)'
Write-Host '  - Script must be run as Administrator'
Write-Host ''
