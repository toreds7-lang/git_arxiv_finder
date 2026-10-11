# Start Scout on http://127.0.0.1:8000
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"
# Stop a stale Scout server still holding the port
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
    if ($p.CommandLine -match "finder\.main:app") {
        Write-Host "Stopping stale Scout server (PID $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force
    } else {
        throw "Port 8000 is in use by another program: $($p.Name) (PID $($p.ProcessId))"
    }
}
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn finder.main:app --host 127.0.0.1 --port 8000
