# Start the Efforti outreach engine on Windows (PowerShell) — ONE command for
# everything: the app, a free Cloudflare tunnel, and open/click tracking wired
# together. The tunnel gives recipients' mail clients a public URL to load the
# open pixel / click links from, so opens & CTR actually register on Analytics.
#
# Usage:
#   ./run.ps1              app + tunnel + tracking (the normal command)
#   ./run.ps1 -NoTunnel    plain local app only (no tunnel; real-recipient
#                          opens/clicks won't register, but everything else works)
#
# First run creates outreach.db and seeds the default sequence. cloudflared is a
# single .exe; if it isn't found the app still starts, just without a tunnel.
param(
  [switch]$NoTunnel,
  [int]$Port = 8000,
  [string]$CloudflaredPath = ""
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Load .env into the process environment.
if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
      $idx = $line.IndexOf("=")
      [Environment]::SetEnvironmentVariable($line.Substring(0, $idx).Trim(),
        $line.Substring($idx + 1).Trim(), "Process")
    }
  }
}

# Single-instance guard: stop any previous run of THIS app so there is only ever
# ONE server (this is what prevents the "two ports fighting over 8000" problem).
Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*uvicorn*app.main*' } |
  ForEach-Object {
    Write-Host "Stopping previous server (PID $($_.ProcessId))..."
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Milliseconds 400

# Free Cloudflare quick tunnel (no account) so opens/clicks reach this local app.
$cf = $null
if (-not $NoTunnel) {
  $cfExe = $null
  $cands = @()
  if ($CloudflaredPath) { $cands += $CloudflaredPath }
  $onPath = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($onPath) { $cands += $onPath.Source }
  $cands += (Join-Path $env:USERPROFILE "cloudflared.exe")
  $cands += (Join-Path $env:USERPROFILE "Downloads\cloudflared.exe")
  $cands += (Join-Path $env:USERPROFILE "Downloads\cloudflared-windows-amd64.exe")
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { $cfExe = $c; break } }

  if ($cfExe) {
    $log = Join-Path $env:TEMP ("cf-{0}.err.log" -f $Port)
    $out = Join-Path $env:TEMP ("cf-{0}.out.log" -f $Port)
    foreach ($f in @($log, $out)) { if (Test-Path $f) { Remove-Item $f -Force } }
    Write-Host "Starting Cloudflare tunnel to http://localhost:$Port ..." -ForegroundColor Cyan
    $cf = Start-Process $cfExe -ArgumentList @("tunnel", "--url", "http://localhost:$Port") `
          -RedirectStandardError $log -RedirectStandardOutput $out -NoNewWindow -PassThru
    $url = $null
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Milliseconds 500
      $txt = ""
      foreach ($f in @($log, $out)) { if (Test-Path $f) { $txt += (Get-Content $f -Raw -ErrorAction SilentlyContinue) } }
      $rx = [regex]::Match($txt, "https://[a-z0-9-]+\.trycloudflare\.com")
      if ($rx.Success) { $url = $rx.Value; break }
    }
    if ($url) {
      $env:APP_BASE_URL = $url
      Write-Host "Tunnel URL : $url" -ForegroundColor Green
      Write-Host "APP_BASE_URL set - open/click tracking will route back to this app." -ForegroundColor Green
    } else {
      Write-Host "Could not get a tunnel URL (see $log). Continuing WITHOUT a tunnel." -ForegroundColor Yellow
      if ($cf -and -not $cf.HasExited) { Stop-Process -Id $cf.Id -Force }
      $cf = $null
    }
  } else {
    Write-Host "cloudflared not found - starting WITHOUT a tunnel (real-recipient opens/clicks won't register)." -ForegroundColor Yellow
    Write-Host '  Get it once: Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile "$env:USERPROFILE\cloudflared.exe"' -ForegroundColor Yellow
  }
}

Write-Host "Open the app at: http://localhost:$Port" -ForegroundColor Green

# --reload: uvicorn watches source files and restarts on edits (local dev only).
try {
  & "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port $Port --reload
}
finally {
  if ($cf -and -not $cf.HasExited) {
    Write-Host "Stopping tunnel..." -ForegroundColor Yellow
    Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue
  }
}
