$ErrorActionPreference = 'Stop'
$proj = $PSScriptRoot
Set-Location -LiteralPath $proj

function Fail($msg) { Write-Host "[오류] $msg" -ForegroundColor Red; Read-Host "엔터를 누르면 닫힙니다"; exit 1 }

Write-Host "==== 전자책 변환기 설치 (새 PC에서 한 번만) ===="
Write-Host ""

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[오류] 파이썬이 없습니다. https://www.python.org/downloads/ 에서 설치하세요." -ForegroundColor Red
    Write-Host "       설치 첫 화면 맨 아래 'Add python.exe to PATH' 체크를 꼭 켜세요."
    Read-Host "엔터를 누르면 닫힙니다"
    exit 1
}
Write-Host ("파이썬: " + (python --version 2>&1))

$venvPy = Join-Path $proj '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "[1/4] 파이썬 실행 환경 만드는 중..."
    python -m venv .venv
    if (-not (Test-Path -LiteralPath $venvPy)) { Fail "실행 환경(.venv) 생성 실패" }
} else {
    Write-Host "[1/4] 실행 환경 이미 있음"
}

Write-Host "[2/4] 필요한 프로그램 설치 중..."
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -e .
if ($LASTEXITCODE -ne 0) { Fail "프로그램 설치 실패" }

Write-Host "[3/4] 크롬이 없을 때 쓸 내장 브라우저 설치 중 (몇 분 걸릴 수 있음)..."
& $venvPy -m playwright install chromium

Write-Host "[4/4] 바탕화면 바로가기 만드는 중..."
& (Join-Path $proj '_shortcut.ps1')

Write-Host ""
Write-Host "==== 설치 완료 ====" -ForegroundColor Green
Write-Host "1) 바탕화면의 '전자책 변환기'를 더블클릭하면 변환기가 뜹니다."
Write-Host "2) 크롬 확장(페이지 저장)을 쓰려면 크롬 주소창에 chrome://extensions 입력,"
Write-Host "   오른쪽 위 '개발자 모드'를 켜고 '압축해제된 확장 프로그램을 로드합니다'에서"
Write-Host "   이 폴더 안의 chrome-extension 폴더를 고르세요."
Write-Host ""
Write-Host "* 표지의 권 번호 자동 읽기는 윈도우 한국어 언어팩이 있어야 동작합니다. 없어도 나머지는 정상입니다."
Write-Host ""
Read-Host "엔터를 누르면 닫힙니다"
