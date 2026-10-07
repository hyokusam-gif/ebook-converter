$ErrorActionPreference = 'Stop'
$proj = $PSScriptRoot
Set-Location -LiteralPath $proj

$venvExe = Join-Path $proj '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path -LiteralPath $venvExe)) {
    Write-Host "[오류] .venv에 pyinstaller가 없습니다. 먼저 '설치.bat'을 실행하세요." -ForegroundColor Red
    Read-Host "엔터를 누르면 닫힙니다"; exit 1
}

Write-Host "==== 설치 없이 쓰는 실행 파일(.exe) 만들기 ===="
Write-Host "[1/3] 빌드 중... (2~5분 걸립니다)"
& $venvExe --noconfirm --clean 'webtoon2pdf.spec'
if ($LASTEXITCODE -ne 0) { Write-Host "[오류] 빌드 실패" -ForegroundColor Red; Read-Host "엔터"; exit 1 }

$distDir = Join-Path $proj 'dist\전자책변환기'
if (-not (Test-Path -LiteralPath (Join-Path $distDir '전자책변환기.exe'))) {
    Write-Host "[오류] 실행 파일이 만들어지지 않았습니다." -ForegroundColor Red; Read-Host "엔터"; exit 1
}

Write-Host "[2/3] 앱 소스(자동 업데이트용)·크롬 확장·안내문 넣는 중..."
# 자동 업데이트 대상인 '두뇌' 소스(webtoon2pdf/)를 exe 옆에 낱개로 둔다. (__pycache__ 제외)
$dstPkg = Join-Path $distDir 'webtoon2pdf'
if (Test-Path -LiteralPath $dstPkg) { Remove-Item -LiteralPath $dstPkg -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $proj 'webtoon2pdf') -Destination $distDir -Recurse -Force
Get-ChildItem -LiteralPath $dstPkg -Recurse -Directory |
    Where-Object { $_.Name -eq '__pycache__' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
# 현재 버전 표시/비교 기준
Copy-Item -LiteralPath (Join-Path $proj 'VERSION') -Destination $distDir -Force
# 크롬 확장·안내문·바로가기 도구
Copy-Item -LiteralPath (Join-Path $proj 'chrome-extension') -Destination $distDir -Recurse -Force
foreach ($f in @('새PC설치안내.txt', '바탕화면 바로가기 만들기.bat', '_desktop_shortcut_dist.ps1')) {
    $src = Join-Path $proj $f
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $distDir -Force }
}

Write-Host "[3/3] 압축하는 중..."
$stamp = Get-Date -Format 'yyyyMMdd'
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) ("전자책변환기_설치없이_" + $stamp + ".zip")
if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
Compress-Archive -Path (Join-Path $distDir '*') -DestinationPath $out -Force

Write-Host ""
if (Test-Path -LiteralPath $out) {
    $mb = [math]::Round((Get-Item -LiteralPath $out).Length / 1MB)
    Write-Host "==== 완료 ($mb MB) ====" -ForegroundColor Green
    Write-Host "만들어진 배포본:"
    Write-Host "  $out"
    Write-Host ""
    Write-Host "이 zip을 새 PC에 옮겨 풀고, 그 안의 '전자책변환기.exe'를 더블클릭하면 됩니다."
    Write-Host "파이썬 설치가 필요 없습니다. (주소 입력 기능은 크롬이 설치돼 있어야 동작)"
} else {
    Write-Host "[오류] 압축 파일이 만들어지지 않았습니다." -ForegroundColor Red
}
Write-Host ""
Read-Host "엔터를 누르면 닫힙니다"
