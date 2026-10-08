# Start the Data analyst (8200), the Journey file agent (8100) and the UI (5173). Close the window to stop.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$analyst = Start-Process $Py -ArgumentList "-m uvicorn analyst.api:app --port 8200" -WorkingDirectory "$Root\backend" -PassThru -NoNewWindow
$journey = Start-Process $Py -ArgumentList "-m uvicorn journey_agent.api:app --port 8100" -WorkingDirectory "$Root\backend" -PassThru -NoNewWindow
try {
  Set-Location "$Root\frontend"
  if (-not (Test-Path "node_modules")) { npm install }
  Write-Host "Open http://localhost:5173"
  npm run dev
} finally {
  Stop-Process -Id $analyst.Id, $journey.Id -ErrorAction SilentlyContinue
}
