$ErrorActionPreference = 'Stop'
Write-Host 'Installing Donut...'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'uv installation failed' }
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv was installed but is not on PATH. Restart your terminal and retry.'
}
$source = $env:DONUT_INSTALL_SOURCE
if (-not $source) {
    $release = Invoke-RestMethod https://api.github.com/repos/ihawn/Donut/releases/latest
    $wheels = @($release.assets | Where-Object { $_.name -like '*-py3-none-any.whl' })
    if ($wheels.Count -ne 1) { throw 'Expected one Donut wheel in the latest release' }
    $source = $wheels[0].browser_download_url
}
& uv tool install --force --python 3.13 $source
if ($LASTEXITCODE -ne 0) { throw 'Donut installation failed' }
if ($env:CI -ne 'true') {
    & uv tool update-shell
    if ($LASTEXITCODE -ne 0) { throw 'Could not update PATH; add the uv tool bin directory manually.' }
}
Write-Host "`nDonut installed. Open a new terminal, then run: donut setup"
