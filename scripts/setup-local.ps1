#Requires -Version 5.1
<#
.SYNOPSIS
    First-time and repeat local development setup for Job Matcher.

.DESCRIPTION
    Sets up the Python virtual environment, installs dependencies, copies example
    config files, and creates the logs directory. Safe to re-run at any time --
    existing config files and the venv are never overwritten.

    Works from both the main repository root and from any git worktree. The script
    resolves the repo root automatically via git, so you can run it from a worktree
    directory without adjusting paths.

.EXAMPLE
    .\scripts\setup-local.ps1
    Run from the repository root or any worktree.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Status {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Message,

        [ValidateSet('Success', 'Skip', 'Info', 'Error')]
        [string]$Level = 'Info'
    )

    $color = switch ($Level) {
        'Success' { 'Green' }
        'Skip'    { 'Yellow' }
        'Info'    { 'Cyan' }
        'Error'   { 'Red' }
    }

    $prefix = switch ($Level) {
        'Success' { '[OK]    ' }
        'Skip'    { '[SKIP]  ' }
        'Info'    { '[INFO]  ' }
        'Error'   { '[ERROR] ' }
    }

    Write-Host "$prefix$Message" -ForegroundColor $color
}

function Test-CommandExists {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    return [bool](Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

# ---------------------------------------------------------------------------
# Resolve the repo root (works from main checkout and from worktrees)
# ---------------------------------------------------------------------------

try {
    $repoRoot = (git rev-parse --show-toplevel 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "git rev-parse failed: $repoRoot"
    }
    # Normalise to a native path object so Join-Path works reliably on Windows
    $repoRoot = [System.IO.Path]::GetFullPath($repoRoot.Trim())
}
catch {
    Write-Status "Could not determine repository root. Run this script from inside the repo." -Level Error
    exit 1
}

Write-Status "Repository root: $repoRoot" -Level Info
Set-Location -Path $repoRoot

# ---------------------------------------------------------------------------
# Step 1 -- Create .venv if it does not already exist
# ---------------------------------------------------------------------------

$venvPath = Join-Path $repoRoot '.venv'

if (Test-Path $venvPath) {
    Write-Status ".venv already exists -- skipping creation." -Level Skip
}
else {
    if (Test-CommandExists 'uv') {
        Write-Status "Creating virtual environment with uv..." -Level Info
        uv venv .venv
    }
    else {
        Write-Status "'uv' not found -- falling back to python -m venv." -Level Skip
        python -m venv .venv
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Status "Failed to create virtual environment." -Level Error
        exit 1
    }

    Write-Status ".venv created." -Level Success
}

# ---------------------------------------------------------------------------
# Step 2 -- Install dependencies
# ---------------------------------------------------------------------------

$requirementsPath = Join-Path $repoRoot 'requirements.txt'

if (-not (Test-Path $requirementsPath)) {
    Write-Status "requirements.txt not found at $requirementsPath -- skipping install." -Level Skip
}
else {
    Write-Status "Installing dependencies via uv pip..." -Level Info

    if (Test-CommandExists 'uv') {
        # Use the global uv with --python pointing at the venv interpreter so
        # packages land inside .venv rather than the global environment.
        $venvPython = Join-Path $venvPath 'Scripts\python.exe'
        uv pip install --python $venvPython -r $requirementsPath
    }
    else {
        # Fall back to pip bundled inside the venv
        $venvPip = Join-Path $venvPath 'Scripts\pip.exe'
        & $venvPip install -r $requirementsPath
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Status "Dependency installation failed." -Level Error
        exit 1
    }

    Write-Status "Dependencies installed." -Level Success
}

# ---------------------------------------------------------------------------
# Step 3 -- Copy example config files (idempotent)
# ---------------------------------------------------------------------------

$configMappings = @(
    @{ Source = 'config\config.example.json';    Target = 'config\config.json' }
    @{ Source = 'config\providers.example.json'; Target = 'config\providers.json' }
    @{ Source = 'config\profile.example.json';   Target = 'config\profile.json' }
)

foreach ($mapping in $configMappings) {
    $sourcePath = Join-Path $repoRoot $mapping.Source
    $targetPath = Join-Path $repoRoot $mapping.Target

    if (-not (Test-Path $sourcePath)) {
        Write-Status "Example file not found: $($mapping.Source) -- skipping." -Level Skip
        continue
    }

    if (Test-Path $targetPath) {
        Write-Status "$($mapping.Target) already exists -- skipping." -Level Skip
    }
    else {
        Copy-Item -Path $sourcePath -Destination $targetPath
        Write-Status "Copied $($mapping.Source) -> $($mapping.Target)" -Level Success
    }
}

# ---------------------------------------------------------------------------
# Step 4 -- Copy .env.dev.example -> .env.dev
# ---------------------------------------------------------------------------

$envSource = Join-Path $repoRoot '.env.dev.example'
$envTarget = Join-Path $repoRoot '.env.dev'

if (-not (Test-Path $envSource)) {
    Write-Status ".env.dev.example not found -- skipping." -Level Skip
}
elseif (Test-Path $envTarget) {
    Write-Status ".env.dev already exists -- skipping." -Level Skip
}
else {
    Copy-Item -Path $envSource -Destination $envTarget
    Write-Status "Copied .env.dev.example -> .env.dev" -Level Success
}

# ---------------------------------------------------------------------------
# Step 5 -- Create logs/ directory
# ---------------------------------------------------------------------------

$logsPath = Join-Path $repoRoot 'logs'

if (Test-Path $logsPath) {
    Write-Status "logs/ directory already exists -- skipping." -Level Skip
}
else {
    New-Item -ItemType Directory -Path $logsPath | Out-Null
    Write-Status "Created logs/ directory." -Level Success
}

# ---------------------------------------------------------------------------
# Next steps guidance
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '--------------------------------------------------------------' -ForegroundColor Cyan
Write-Host '  Setup complete. Next steps:' -ForegroundColor Cyan
Write-Host '--------------------------------------------------------------' -ForegroundColor Cyan
Write-Host ''
Write-Host '  1. Edit config\providers.json (or use the /settings UI after' -ForegroundColor White
Write-Host '     starting the app) to add your LLM API keys.' -ForegroundColor White
Write-Host ''
Write-Host '  2. Edit config\profile.json to match your skills and location.' -ForegroundColor White
Write-Host ''
Write-Host '  3. Start the dev database (Docker required):' -ForegroundColor White
Write-Host '       docker compose -f docker-compose.dev.yml --env-file .env.dev -p job-matcher-pr-dev up -d db' -ForegroundColor DarkGray
Write-Host ''
Write-Host '     Or use VS Code: Ctrl+Shift+B to run "Start Job Matcher"' -ForegroundColor White
Write-Host '     (starts the DB and web UI together).' -ForegroundColor White
Write-Host ''
Write-Host '  4. Set DATABASE_URL before running the app or ingestion:' -ForegroundColor White
Write-Host '       $env:DATABASE_URL = "postgresql://jobmatcher:changeme_dev@localhost:5432/jobmatcher_dev"' -ForegroundColor DarkGray
Write-Host ''
Write-Host '     The default password is "changeme_dev" (from .env.dev.example).' -ForegroundColor White
Write-Host '     If you changed POSTGRES_PASSWORD in .env.dev, update this URL.' -ForegroundColor White
Write-Host ''
Write-Host '  5. Start the web UI:' -ForegroundColor White
Write-Host '       .venv\Scripts\python app.py' -ForegroundColor DarkGray
Write-Host '     Then open http://localhost:5000' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  6. Run ingestion:' -ForegroundColor White
Write-Host '       .venv\Scripts\python ingest.py' -ForegroundColor DarkGray
Write-Host ''
