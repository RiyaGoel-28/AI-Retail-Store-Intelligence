Write-Host "=== STORE INTELLIGENCE SYSTEM VERIFICATION ===" -ForegroundColor Cyan

# 1. Check store_layout.json
Write-Host "`n[1/5] Checking store_layout.json..." -ForegroundColor Yellow
if (Test-Path "store_layout.json") {
    Write-Host "✓ store_layout.json found" -ForegroundColor Green
} else {
    Write-Host "✗ store_layout.json NOT FOUND" -ForegroundColor Red
}

# 2. Check detection pipeline
Write-Host "`n[2/5] Checking detection pipeline..." -ForegroundColor Yellow
if (Test-Path "pipeline/detect.py") {
    Write-Host "✓ pipeline/detect.py exists" -ForegroundColor Green
} else {
    Write-Host "✗ pipeline/detect.py NOT FOUND" -ForegroundColor Red
}

# 3. Check heatmap endpoint
Write-Host "`n[3/5] Checking heatmap endpoint..." -ForegroundColor Yellow
if (Test-Path "app/heatmap.py") {
    Write-Host "✓ app/heatmap.py exists" -ForegroundColor Green
} else {
    Write-Host "✗ app/heatmap.py NOT FOUND" -ForegroundColor Red
}

# 4. Check docker-compose
Write-Host "`n[4/5] Checking docker-compose..." -ForegroundColor Yellow
if (Test-Path "docker-compose.yml") {
    Write-Host "✓ docker-compose.yml exists" -ForegroundColor Green
} else {
    Write-Host "✗ docker-compose.yml NOT FOUND" -ForegroundColor Red
}

# 5. Run tests
Write-Host "`n[5/5] Running pytest..." -ForegroundColor Yellow
$venv_path = ".\.venv\Scripts\Activate.ps1"
if (Test-Path $venv_path) {
    & $venv_path
}
pytest -q --tb=line

Write-Host "`n=== VERIFICATION COMPLETE ===" -ForegroundColor Cyan
Write-Host "Ready for:" -ForegroundColor Green
Write-Host "  1. pytest -q" -ForegroundColor Green
Write-Host "  2. uvicorn app.main:app --reload" -ForegroundColor Green
Write-Host "  3. docker compose up" -ForegroundColor Green