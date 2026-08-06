Param()
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root\..\

Write-Host "Starting services with docker compose..."
docker compose up -d --build

$api = 'http://localhost:8000'
$health = "$api/health"

Write-Host "Waiting for API to become healthy (timeout 120s)"
$sec = 0
while ($sec -lt 120) {
    try {
        $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { break }
    } catch {}
    Start-Sleep -Seconds 2
    $sec += 2
    Write-Host -NoNewline '.'
}

if ($sec -ge 120) {
    Write-Host "Timed out waiting for API health"
    docker compose logs --no-color
    exit 2
}

Write-Host "API is healthy. Posting sample events..."
$sample = Join-Path $Root '..' 'scripts\sample_events_acceptance.json'
$resp = Invoke-RestMethod -Uri ($api + '/events/ingest') -Method Post -Body (Get-Content $sample -Raw) -ContentType 'application/json'
Write-Host "Ingest response:`n" ($resp | ConvertTo-Json -Depth 5)

Write-Host "Running validator"
python scripts\validate_events.py --file $sample

$store = (Get-Content $sample | ConvertFrom-Json)[0].store_id
foreach ($endpoint in @('metrics','funnel','heatmap','anomalies')) {
    $url = "$api/stores/$store/$endpoint"
    Write-Host "GET $url"
    $out = Invoke-RestMethod -Uri $url
    Write-Host ($out | ConvertTo-Json -Depth 5)
}

Write-Host "Acceptance run completed successfully"