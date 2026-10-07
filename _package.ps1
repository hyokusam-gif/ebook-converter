$ErrorActionPreference = 'Stop'
$proj = $PSScriptRoot
$exclDirs = @('.venv', 'work', '__pycache__', '.pytest_cache', '.git', 'webtoon2pdf.egg-info')
$exclExt  = @('.pdf', '.log')
$exclName = @('storage_state.json')

$stamp = Get-Date -Format 'yyyyMMdd'
$desktop = [Environment]::GetFolderPath('Desktop')
$out = Join-Path $desktop ("전자책변환기_복사본_" + $stamp + ".zip")
$tmp = Join-Path $env:TEMP ('w2p_pkg_' + [guid]::NewGuid().ToString('N'))

Write-Host "==== 다른 PC로 옮길 복사본(zip) 만들기 ===="
Write-Host ""
Write-Host "무거운 파일(.venv, 브라우저, 만든 PDF)은 빼고 소스만 압축합니다."
Write-Host "[1/3] 필요한 파일만 모으는 중..."

New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Get-ChildItem -LiteralPath $proj -Force | ForEach-Object {
    if ($_.PSIsContainer) {
        if ($exclDirs -notcontains $_.Name) {
            Copy-Item -LiteralPath $_.FullName -Destination $tmp -Recurse -Force
        }
    } elseif ($exclExt -notcontains $_.Extension -and $exclName -notcontains $_.Name) {
        Copy-Item -LiteralPath $_.FullName -Destination $tmp -Force
    }
}
# 하위 폴더 안에 섞여 있는 제외 대상(__pycache__ 등)도 정리
Get-ChildItem -LiteralPath $tmp -Recurse -Force -Directory |
    Where-Object { $exclDirs -contains $_.Name } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

if (-not (Test-Path (Join-Path $tmp 'pyproject.toml'))) {
    Write-Host "[오류] 파일 모으기에 실패했습니다." -ForegroundColor Red
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "[2/3] 압축하는 중..."
if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $out -Force

Write-Host "[3/3] 정리 중..."
Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if (Test-Path -LiteralPath $out) {
    $kb = [math]::Round((Get-Item -LiteralPath $out).Length / 1KB)
    Write-Host "==== 완료 ($kb KB) ====" -ForegroundColor Green
    Write-Host "만들어진 복사본:"
    Write-Host "  $out"
    Write-Host ""
    Write-Host "이 zip 파일을 USB나 메일, 카카오톡으로 새 PC에 옮기세요."
    Write-Host "새 PC에서 zip을 풀고 그 안의 '설치.bat'을 더블클릭하면 설치됩니다."
} else {
    Write-Host "[오류] 압축 파일이 만들어지지 않았습니다." -ForegroundColor Red
}
