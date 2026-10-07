$ErrorActionPreference = 'SilentlyContinue'
$proj = $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '전자책 변환기.lnk'
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = Join-Path $proj '이미지PDF.bat'
$s.WorkingDirectory = $proj
$s.IconLocation = (Join-Path $proj '.venv\Scripts\pythonw.exe') + ',0'
$s.Description = '웹페이지 이미지를 모아 전자책(PDF/EPUB)으로 만듭니다'
$s.Save()
if (Test-Path -LiteralPath $lnk) { Write-Host "바탕화면 바로가기 생성됨: 전자책 변환기" }
