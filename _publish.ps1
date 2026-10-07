$ErrorActionPreference = 'Stop'
$proj = $PSScriptRoot
Set-Location -LiteralPath $proj

if (-not (Test-Path -LiteralPath (Join-Path $proj '.git'))) {
    Write-Host "[오류] 아직 깃허브에 연결되지 않았습니다. (처음 한 번 설정이 필요)" -ForegroundColor Red
    Read-Host "엔터를 누르면 닫힙니다"; exit 1
}

$stamp = Get-Date -Format 'yyyyMMddHHmm'
Set-Content -LiteralPath (Join-Path $proj 'VERSION') -Value $stamp -NoNewline -Encoding ascii
Write-Host "새 버전: $stamp"
Write-Host "올리는 중..."

git add -A
git commit -m "업데이트 $stamp" | Out-Host
git push | Out-Host

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "==== 올렸습니다 ====" -ForegroundColor Green
    Write-Host "이제 다른 PC에서 앱을 켜면 '새 버전이 있어요?'가 떠서 자동으로 최신이 됩니다."
} else {
    Write-Host "[오류] 올리기에 실패했습니다. (인터넷/깃허브 로그인 확인)" -ForegroundColor Red
}
Read-Host "엔터를 누르면 닫힙니다"
