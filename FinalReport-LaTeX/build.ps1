# Compile only this standalone paper. No project, Python or ORCA dependency.
param([switch]$Clean)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildDir = Join-Path $PSScriptRoot 'build'
if (-not (Test-Path -LiteralPath $buildDir)) {
    New-Item -ItemType Directory -Path $buildDir | Out-Null
}
$engine = Get-Command xelatex.exe -ErrorAction SilentlyContinue
if ($engine) {
    $enginePath = $engine.Source
} else {
    $candidates = @('E:\texlive\2026\bin\windows\xelatex.exe')
    $candidates += @(Get-ChildItem -Path 'C:\texlive\*\bin\windows\xelatex.exe','D:\texlive\*\bin\windows\xelatex.exe','E:\texlive\*\bin\windows\xelatex.exe' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $enginePath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $enginePath) { throw 'XeLaTeX not found. Install TeX Live and add its bin directory to PATH.' }
# Child TeX utilities must use the same installation.
$env:PATH = (Split-Path -Parent $enginePath) + ';' + $env:PATH
if ($Clean) {
    # Delete only explicitly named generated files in this paper's build folder.
    foreach ($extension in @('aux','log','out','toc','fls','fdb_latexmk','xdv','synctex.gz')) {
        $generated = Join-Path $buildDir ('main.' + $extension)
        if (Test-Path -LiteralPath $generated) { Remove-Item -LiteralPath $generated }
    }
    exit 0
}
foreach ($pass in 1..2) {
    & $enginePath '-interaction=nonstopmode' '-halt-on-error' '-file-line-error' '-synctex=1' '-recorder' '-output-directory=build' 'main.tex'
    if ($LASTEXITCODE -ne 0) { throw "XeLaTeX pass $pass failed. See build/main.log." }
}
Write-Host 'Compiled: build/main.pdf'
