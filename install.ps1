param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"

$CtxDir = $PSScriptRoot
$ForwardArgs = @()

if ($RemainingArgs.Count -ge 2 -and $RemainingArgs[0] -eq "--ctx-dir") {
    $CtxDir = $RemainingArgs[1]
    if ($RemainingArgs.Count -gt 2) {
        $ForwardArgs = $RemainingArgs[2..($RemainingArgs.Count - 1)]
    }
}
elseif ($RemainingArgs.Count -ge 1 -and -not $RemainingArgs[0].StartsWith("--")) {
    $CtxDir = $RemainingArgs[0]
    if ($RemainingArgs.Count -gt 1) {
        $ForwardArgs = $RemainingArgs[1..($RemainingArgs.Count - 1)]
    }
}
else {
    $ForwardArgs = $RemainingArgs
}

$Python = $env:PYTHON
if (-not $Python) {
    $Python = "python"
}

$SrcDir = Join-Path $CtxDir "src"
$Separator = [System.IO.Path]::PathSeparator
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$SrcDir$Separator$env:PYTHONPATH"
}
else {
    $env:PYTHONPATH = $SrcDir
}

& $Python -m ctx_init @ForwardArgs
exit $LASTEXITCODE
