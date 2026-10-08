param(
    [Parameter(Mandatory=$true)][string]$Target
)
$ErrorActionPreference = "Stop"
if (!(Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "Release signing target does not exist: $Target"
}
if (!$env:PHOTOCURATOR_SIGNING_PFX_BASE64 -or
    !$env:PHOTOCURATOR_SIGNING_PFX_PASSWORD) {
    throw "Missing release signing secrets"
}
$sdk = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
$signTool = Get-ChildItem -Path $sdk -Filter signtool.exe -Recurse -File -ErrorAction Stop |
    Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool[.]exe$' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (!$signTool) { throw "Windows SDK SignTool x64 unavailable" }
$pfx = Join-Path $env:RUNNER_TEMP ("photocurator-sign-" + [guid]::NewGuid().ToString('N') + ".pfx")
try {
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:PHOTOCURATOR_SIGNING_PFX_BASE64))
    & $signTool sign /fd SHA256 /td SHA256 /tr https://timestamp.digicert.com /f $pfx /p $env:PHOTOCURATOR_SIGNING_PFX_PASSWORD $Target
    if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed" }
    & $signTool verify /pa /v $Target
    if ($LASTEXITCODE -ne 0) { throw "Authenticode signature did not verify" }
}
finally {
    Remove-Item -LiteralPath $pfx -Force -ErrorAction SilentlyContinue
}
