[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [switch]$Execute,
    [ValidateRange(1,50)][int]$Concurrency = 50,
    [ValidateRange(1,10)][int]$MaxAttempts = 3
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Project .venv is missing; install the project images extra first.'
}
$ManifestPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Manifest)
$Arguments = @('-m', 'openai_images.batch', '--manifest', $ManifestPath,
               '--env-file', (Join-Path $ProjectRoot '.env'),
               '--concurrency', "$Concurrency", '--max-attempts', "$MaxAttempts")
if ($Execute) { $Arguments += '--execute' }
# No global profile changes, no server requests until explicit -Execute.
& $Python @Arguments
exit $LASTEXITCODE
