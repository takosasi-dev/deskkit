# DeskKit を単体 exe にビルドする。出力: dist\DeskKit.exe(と一つ上のフォルダへの配布用コピー)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
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
$hash = (Get-FileHash dist\DeskKit.exe -Algorithm SHA256).Hash.ToLower()
"$hash  DeskKit.exe" | Set-Content -Encoding ascii -NoNewline dist\DeskKit.exe.sha256
Copy-Item dist\DeskKit.exe ..\DeskKit.exe -Force
Write-Host "sha256 $hash"
Write-Host "built dist\DeskKit.exe (v$ver)"
