$ErrorActionPreference = 'SilentlyContinue'
$here = $PSScriptRoot
$exe = Join-Path $here '전자책변환기.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "이 스크립트는 전자책변환기.exe 와 같은 폴더에서 실행해야 합니다."
    Read-Host "엔터를 누르면 닫힙니다"; exit 1
}
$ws = New-Object -ComObject WScript.Shell
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '전자책 변환기.lnk'
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $exe
$s.WorkingDirectory = $here
$s.IconLocation = $exe + ',0'
$s.Description = '웹페이지 이미지를 모아 전자책(PDF/EPUB)으로 만듭니다'
$s.Save()
if (Test-Path -LiteralPath $lnk) { Write-Host "바탕화면에 '전자책 변환기' 바로가기를 만들었습니다." }
Read-Host "엔터를 누르면 닫힙니다"
