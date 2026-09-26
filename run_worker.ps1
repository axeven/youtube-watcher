param(
    [string]$Python = $env:WORKER_PYTHON
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $Python) {
    $venv = Join-Path $root "venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { "python" }
}

# Unbuffered so progress is visible immediately in the per-run log.
$env:PYTHONUNBUFFERED = "1"
# PowerShell 5.1 defaults redirection to UTF-16; write readable UTF-8 logs.
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# One log file per run: a hung run must not hold the log open and block the
# next scheduled run (which is what happened with a shared append handle).
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$log = Join-Path $logDir ("worker_{0}.log" -f $stamp)

"[{0}] starting with {1}" -f (Get-Date -Format s), $Python | Set-Content -Path $log
& $Python (Join-Path $root "analyze_worker.py") *>> $log
$code = $LASTEXITCODE
"[{0}] exit {1}" -f (Get-Date -Format s), $code | Add-Content -Path $log
exit $code
