# DeskKit を単体 exe にビルドする。出力: dist\DeskKit.exe(と一つ上のフォルダへの配布用コピー)
# v0.3: SendPrep が使う ffmpeg(LGPL 版)を同梱する。配布物は自動でダウンロードせず、手で third_party\ffmpeg\ に置く(H-6)。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

# ---- ffmpeg の同梱物(deskkit\_bundled\ffmpeg.zip と ffmpeg.sha256)
$ffName = "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
$ffZip = Join-Path "third_party\ffmpeg" $ffName
if (-not (Test-Path $ffZip) -or -not (Test-Path "third_party\ffmpeg\checksums.sha256")) {
    Write-Host ""
    Write-Host "ffmpeg の配布物がありません。ビルドを中止します。" -ForegroundColor Red
    Write-Host "  1. https://github.com/BtbN/FFmpeg-Builds/releases を開く(latest のリリース)"
    Write-Host "  2. $ffName と checksums.sha256 をダウンロードする"
    Write-Host "     (LGPL 版を使うこと。名前に gpl が付き lgpl が付かないものは使わない)"
    Write-Host "  3. 2つとも $PSScriptRoot\third_party\ffmpeg\ に置く"
    Write-Host "  4. SHA-256 を照合する(このスクリプトも自動で照合する):"
    Write-Host "       (Get-FileHash third_party\ffmpeg\$ffName -Algorithm SHA256).Hash.ToLower()"
    Write-Host "       Select-String $ffName third_party\ffmpeg\checksums.sha256"
    Write-Host "  5. 版が変わったら THIRD_PARTY_LICENSES.txt の ffmpeg の版と入手先を直す"
    Write-Host "     (tools\make_third_party_licenses.py の FFMPEG_VERSION を直して実行する)"
    exit 1
}
if (-not (Test-Path "THIRD_PARTY_LICENSES.txt")) {
    Write-Host "THIRD_PARTY_LICENSES.txt がありません。tools\make_third_party_licenses.py で作ってください。" -ForegroundColor Red
    exit 1
}
& $py tools\make_ffmpeg_bundle.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "ffmpeg の同梱物を作れませんでした(上のエラーを確認してください)。" -ForegroundColor Red
    exit 1
}

& $py tools\make_icon.py build\deskkit.ico
$ver = (& $py -c "import deskkit; print(deskkit.__version__)").Trim()
$parts = ($ver.Split(".") + @("0", "0", "0"))[0..3] -join ", "
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041104B0', [
      StringStruct('CompanyName', 'DeskKit'),
      StringStruct('FileDescription', 'DeskKit - QOL ツールキット'),
      StringStruct('FileVersion', '$ver'),
      StringStruct('InternalName', 'DeskKit'),
      StringStruct('OriginalFilename', 'DeskKit.exe'),
      StringStruct('ProductName', 'DeskKit'),
      StringStruct('ProductVersion', '$ver')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])
  ]
)
"@ | Set-Content -Encoding utf8 build\version_info.txt
& $py -m PyInstaller --noconfirm --clean deskkit.spec
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller が失敗しました。" -ForegroundColor Red
    exit 1
}
$hash = (Get-FileHash dist\DeskKit.exe -Algorithm SHA256).Hash.ToLower()
"$hash  DeskKit.exe" | Set-Content -Encoding ascii -NoNewline dist\DeskKit.exe.sha256
Copy-Item dist\DeskKit.exe ..\DeskKit.exe -Force
Write-Host "sha256 $hash"
Write-Host "size $([math]::Round((Get-Item dist\DeskKit.exe).Length / 1MB, 1)) MB"
Write-Host "built dist\DeskKit.exe (v$ver)"
