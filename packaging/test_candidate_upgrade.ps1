# Runs ONLY on disposable Windows CI runners. Never target user installations.
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[0-9]+[.][0-9]+[.][0-9]+$')]
    [string]$Version
)
$ErrorActionPreference = 'Stop'
$installer = (Resolve-Path ("release\\PhotoCurator-Setup-v" + $Version + ".exe")).Path
# Test an actual previously shipped release, not just the same candidate twice.
$priorVersion = '1.5.0'
$priorSha256 = 'ac31a260b09f31c5578dd397c075817df0a560c25b681a70f3932e327d3a1b5c'
$priorInstaller = Join-Path $env:RUNNER_TEMP ("PhotoCurator-Setup-v" + $priorVersion + ".exe")
$priorUrl = "https://github.com/youzhi1701/PhotoCurator-CN-/releases/download/v$priorVersion/PhotoCurator-Setup-v$priorVersion.exe"
Invoke-WebRequest -Uri $priorUrl -OutFile $priorInstaller -UseBasicParsing
if ((Get-FileHash -LiteralPath $priorInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $priorSha256) {
    throw 'Previously published installer SHA256 verification failed; refusing to execute download'
}
$installRoot = Join-Path $env:RUNNER_TEMP 'PhotoCurator-Candidate-Upgrade-Probe'
$dataRoot = Join-Path $env:LOCALAPPDATA 'PhotoCurator\\data'
$configDir = Join-Path $dataRoot 'config'
$offlineDir = Join-Path $dataRoot 'offline_previews'
$logPath = Join-Path $env:RUNNER_TEMP 'photocurator-upgrade-test.log'
New-Item -ItemType Directory -Force $configDir, $offlineDir | Out-Null
$probeConfig = Join-Path $configDir 'candidate-upgrade-preserve-probe.bin'
$probeOffline = Join-Path $offlineDir 'candidate-upgrade-preserve-probe.bin'
$probeData = [byte[]](0,255,13,10,78,9,122,3,201,4,5,6)
[IO.File]::WriteAllBytes($probeConfig,$probeData)
[IO.File]::WriteAllBytes($probeOffline,$probeData)
$originalHash = (Get-FileHash $probeConfig -Algorithm SHA256).Hash
$externalPhotos = Join-Path $env:RUNNER_TEMP 'photocurator-external-photographs'
New-Item -ItemType Directory -Force $externalPhotos | Out-Null
$externalMarker = Join-Path $externalPhotos 'original-user-photo.jpg'
[IO.File]::WriteAllBytes($externalMarker,$probeData)
$externalHash = (Get-FileHash $externalMarker -Algorithm SHA256).Hash

function AssertPreserved($pass, $expectedVersion) {
    $exe = Join-Path $installRoot 'app\\PhotoCurator.exe'
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "Pass ${pass}: installed PhotoCurator.exe missing"
    }
    $actual = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion
    if (!$actual.StartsWith($expectedVersion)) {
        throw "Pass ${pass}: wrong installed version $actual"
    }
    foreach($file in @($probeConfig, $probeOffline)) {
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Pass ${pass}: retained config/offline preview removed"
        }
        $actualHash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
        if ($actualHash -ne $originalHash) {
            throw "Pass ${pass}: retained config/offline preview changed"
        }
    }
    if ((Get-FileHash -LiteralPath $externalMarker -Algorithm SHA256).Hash -ne $externalHash) {
        throw "Pass ${pass}: an external original photo was modified"
    }
    Write-Host "Pass ${pass}: installed executable, retained user data and original photo verified."
}

for ($pass=1; $pass -le 2; $pass++) {
    $args = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',
              ('/DIR="' + $installRoot + '"'),('/LOG="' + $logPath + '"'))
    $chosenInstaller = if ($pass -eq 1) { $priorInstaller } else { $installer }
    $expectedVersion = if ($pass -eq 1) { $priorVersion } else { $Version }
    $process = Start-Process -FilePath $chosenInstaller -ArgumentList $args -PassThru
    # Do not burn unlimited Actions minutes if a WebView bootstrapper or
    # legacy setup gets stuck on a network-dependent silent installation.
    if (-not $process.WaitForExit(180000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Installer pass ${pass} timed out after 180 seconds"
    }
    if ($process.ExitCode -ne 0) {
        if (Test-Path $logPath) {
            Get-Content $logPath -Tail 35 | Out-Host
        }
        throw "Installer pass $pass failed with exit code $($process.ExitCode)"
    }
    AssertPreserved $pass $expectedVersion
}
Write-Host "Windows v$priorVersion shipped installer -> v$Version candidate in-place upgrade both passed."
