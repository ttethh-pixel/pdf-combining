param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    & $Python -c "import sys,struct,tkinter,PIL,pypdf,pypdfium2; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8, 'Use CPython 3.12 x64'"
    if ($LASTEXITCODE) { throw 'Python dependency check failed' }
    New-Item -ItemType Directory -Force build | Out-Null
    & "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll /out:build\DrawingReview.exe tools\Launcher.cs
    if ($LASTEXITCODE) { throw 'C# compilation failed' }
    & $Python tools/package_app.py
    if ($LASTEXITCODE) { throw 'ZIP build failed' }
} finally { Pop-Location }
