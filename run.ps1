# Start the Efforti outreach engine on Windows (PowerShell).
# First run creates outreach.db and seeds the default sequence.
# Usage:  ./run.ps1     then open http://localhost:8000
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Load .env into the process environment
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $key = $line.Substring(0, $idx).Trim()
            $val = $line.Substring($idx + 1).Trim()
            [Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

# Single-instance guard: stop any previous run of THIS app that's still alive.
# Repeated starts had left several orphaned uvicorn servers fighting over port
# 8000 — an old one on 127.0.0.1 shadowed localhost and kept serving stale code.
# Killing prior instances here means every ./run.ps1 is a clean, single server.
Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*uvicorn*app.main*' } |
    ForEach-Object {
        Write-Host "Stopping previous server (PID $($_.ProcessId))..."
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Milliseconds 400

# --reload: uvicorn watches the source files and restarts automatically when you
# edit code, so changes (routes, email/signature logic, sequence copy) take
# effect without you stopping and re-running this script. Local dev only — the
# production launchers (Procfile / Dockerfile) intentionally omit it.
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
