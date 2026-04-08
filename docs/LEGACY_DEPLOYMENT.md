# Legacy Deployment: Native Windows Server

> **This deployment path is no longer active.** These instructions are preserved here for reference in case you need to run Job Matcher natively on Windows without Docker. The primary and recommended deployment path is Docker on Linux — see [docs/DOCKER.md](DOCKER.md) and the Docker deployment section of the [README](../README.md).

Use this approach if you want the web UI running as a Windows service and ingestion triggered by Task Scheduler.

## Quick start

`scripts/setup.ps1` automates all the manual steps described in this section. Run it once as Administrator from the project root:

```powershell
.\scripts\setup.ps1
```

What it does:

- Prompts for the data directory path and daily ingest time (infrastructure only — no credential prompts)
- Sets system environment variables (`DB_PATH`, `FLASK_DEBUG`)
- Creates the data directory and a `logs/` subfolder
- Copies `config/keys.example.json` → `config/keys.json` and restricts its ACL to the current user
- Copies `config/config.example.json` → `config/config.json` (if absent)
- Registers the `JobMatcher` Windows service (waitress via NSSM) set to auto-start
- Registers the `JobMatcherIngest` daily Task Scheduler task
- Opens Windows Firewall inbound TCP port 5000 so the UI is reachable from the network

After the script completes, open the **Settings** page to configure your LLM provider API keys and any job source credentials (including Adzuna App ID and App Key).

## Prerequisites

- Python venv already set up and `uv pip install -r requirements.txt` run
- `config/profile.json` present in the project (`config/config.json` is created from the example by the script if absent)
- [NSSM](https://nssm.cc/download) downloaded and either on `PATH` or referenced by full path

## Environment variables (reference)

Set the database path as a machine-level environment variable so both the service and the scheduled task pick it up automatically. Adzuna credentials are optional — only set them if you are using the Adzuna source:

```powershell
[System.Environment]::SetEnvironmentVariable("DB_PATH", "C:\path\to\data\jobs.db", "Machine")

# Optional — only needed if using the Adzuna source
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_ID", "your_id", "Machine")
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_KEY", "your_key", "Machine")
```

Restart your terminal after setting machine-level variables for them to take effect.

LLM provider API keys (Anthropic, OpenAI, Gemini) are managed through `config/providers.json` and the `/settings` UI — do not set them as environment variables.

## Web service — NSSM (reference)

Register waitress as a Windows service named `JobMatcher`:

```powershell
nssm install JobMatcher "C:\Apps\job-matcher-pr\venv\Scripts\waitress-serve.exe"
nssm set JobMatcher AppParameters "--host=0.0.0.0 --port=5000 app:app"
nssm set JobMatcher AppDirectory "C:\Apps\job-matcher-pr"
nssm set JobMatcher Start SERVICE_AUTO_START
nssm start JobMatcher

# Allow inbound connections from the network (run as Administrator)
New-NetFirewallRule -DisplayName "Job Matcher Web UI" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

Service management:

```powershell
nssm stop JobMatcher
nssm restart JobMatcher
nssm status JobMatcher
```

## Scheduled ingest — Task Scheduler (reference)

Create a daily ingest task running at 6am:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Apps\job-matcher-pr\venv\Scripts\python.exe" `
    -Argument "ingest.py --hours 25" `
    -WorkingDirectory "C:\Apps\job-matcher-pr"

$trigger = New-ScheduledTaskTrigger -Daily -At 6am

Register-ScheduledTask -TaskName "JobMatcherIngest" `
    -Action $action `
    -Trigger $trigger `
    -RunLevel Highest `
    -Force
```

Run the task manually at any time:

```powershell
Start-ScheduledTask -TaskName "JobMatcherIngest"
```

## Data directory (reference)

Create the directory that will hold the database and point `DB_PATH` at it:

```powershell
New-Item -ItemType Directory -Force -Path "C:\path\to\data"
```

The SQLite database (`jobs.db`) is created there automatically on the first ingest run.

## API keys (LLM providers)

LLM provider keys (Anthropic, OpenAI, Gemini, etc.) are stored in `config/providers.json` alongside job source credentials. The file is gitignored and never committed. After running `scripts/setup.ps1`, open the **Settings** page to enter your API keys — the Settings UI validates each key before saving.

## Ops commands

```powershell
# Show service status, last task run, and DB row count
.\scripts\status.ps1

# Remove the service and scheduled task cleanly
.\scripts\teardown.ps1

# Run ingest immediately without waiting for the scheduled trigger
Start-ScheduledTask -TaskName "JobMatcherIngest"
```
