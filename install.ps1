# EvoLab online installer bootstrap for Windows
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._+-]+$')]
    [string]$Version = "v0.2.0",
    [string]$Prefix,
    [switch]$NoPathUpdate
)

$ErrorActionPreference = "Stop"
$archiveName = "evolab-launcher.zip"
$checksumName = "$archiveName.sha256"
$repository = if ($env:EVOLAB_GITHUB_REPOSITORY) {
    $env:EVOLAB_GITHUB_REPOSITORY
} elseif ($env:OPENEVO_GITHUB_REPOSITORY) {
    $env:OPENEVO_GITHUB_REPOSITORY
} else {
    "KDBRonaldo/OpenEvo"
}
if ($repository -notmatch '^[^/]+/[^/]+$') {
    throw "EVOLAB_GITHUB_REPOSITORY must be in owner/repository form."
}
$baseUrl = if ($env:EVOLAB_RELEASE_BASE_URL) {
    $env:EVOLAB_RELEASE_BASE_URL.TrimEnd('/')
} elseif ($env:OPENEVO_RELEASE_BASE_URL) {
    $env:OPENEVO_RELEASE_BASE_URL.TrimEnd('/')
} else {
    "https://github.com/$repository/releases"
}
$downloadRoot = if ($Version -eq "latest") {
    "$baseUrl/latest/download"
} else {
    "$baseUrl/download/$Version"
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("evolab-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
try {
    $archivePath = Join-Path $temporaryRoot $archiveName
    $checksumPath = Join-Path $temporaryRoot $checksumName
    Write-Host "Downloading EvoLab $Version from $downloadRoot ..."
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "$downloadRoot/$archiveName" -OutFile $archivePath
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "$downloadRoot/$checksumName" -OutFile $checksumPath

    $checksumLine = (Get-Content -LiteralPath $checksumPath -Raw -Encoding ASCII).Trim()
    if ($checksumLine -notmatch '^([0-9a-f]{64})\s+\*?evolab-launcher\.zip$') {
        throw "EvoLab release checksum is malformed."
    }
    $expected = $Matches[1]
    $actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "EvoLab launcher checksum verification failed."
    }

    Expand-Archive -LiteralPath $archivePath -DestinationPath $temporaryRoot
    $packageInstaller = Join-Path $temporaryRoot "evolab-launcher\install.ps1"
    if (-not (Test-Path -LiteralPath $packageInstaller -PathType Leaf)) {
        throw "EvoLab launcher archive does not contain a safe installer."
    }
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $packageInstaller)
    if (-not [string]::IsNullOrWhiteSpace($Prefix)) {
        $arguments += @("-Prefix", $Prefix)
    }
    if ($NoPathUpdate) { $arguments += "-NoPathUpdate" }
    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "EvoLab package installer exited with code $LASTEXITCODE."
    }
} finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
