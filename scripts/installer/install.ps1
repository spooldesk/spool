$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$exitCode = 0
$lockContainer = $null
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = [Console]::OutputEncoding
$env:DOCKER_CLI_HINTS = 'false'

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker $($args[0]) failed. Check the message above, fix the cause and run this file again." }
}

try {
    $bundle = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    Set-Location -LiteralPath $bundle
    Write-Host "`nSpooldesk installer | Windows`nInstallation folder: $bundle\spooldesk`n"
    if ($bundle.Contains(',')) { throw 'Extract the ZIP to a folder without commas in its path.' }
    if (-not (Test-Path -LiteralPath 'SHA256SUMS' -PathType Leaf)) {
        throw 'Extract the entire installation ZIP first, then open INSTALL-WINDOWS.cmd.'
    }
    $required = @('package/spooldesk-image.tar.gz','package/spooldesk-image-arm64.tar.gz','package/spooldesk-source.zip',
        'package/spooldesk-source.tar.gz','package/IMAGE_ID','package/IMAGE_TAG','package/IMAGE_ID_ARM64','package/IMAGE_TAG_ARM64',
        'package/IMAGE_IDS','package/IMAGE_IDS_ARM64','package/SOURCE_COMMIT','package/VALIDATION.json',
        'README.txt','INSTALLATION.md','INSTALLATION_RU.md','TROUBLESHOOTING.md','INSTALL-WINDOWS.cmd','INSTALL-MACOS.command',
        'INSTALL-LINUX.sh','installer/configure.py','installer/install.ps1','installer/install.sh')
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    Write-Host '[1/6] Checking package integrity...'
    foreach ($directory in @('installer', 'package')) {
        if ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Linked package folders are not supported.'
        }
    }
    foreach ($line in Get-Content -LiteralPath 'SHA256SUMS') {
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw 'Invalid checksum manifest.' }
        $expected = $Matches[1]; $name = $Matches[2]
        if ([Array]::IndexOf($required, $name) -lt 0 -or -not $seen.Add($name)) {
            throw 'Unexpected or duplicate checksum entry.'
        }
        $file = Get-Item -LiteralPath (Join-Path $bundle $name)
        if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Linked package files are not supported.'
        }
        if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower() -ne $expected) {
            throw "Package checksum mismatch: $name. Download a fresh ZIP."
        }
    }
    if ($seen.Count -ne $required.Count) { throw 'Incomplete checksum manifest.' }
    Write-Host '[2/6] Checking Docker...'
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and run this file again. Python and Git are not required.'
    }
    $endpoint = if ($env:DOCKER_HOST) { $env:DOCKER_HOST } else { (Invoke-Docker context inspect --format '{{.Endpoints.docker.Host}}').Trim() }
    if ($endpoint -notmatch '^(npipe|unix)://') { throw 'This installer requires a local Docker engine, not a remote context.' }
    $dockerInfo = (Invoke-Docker info --format '{{.OSType}} {{.Architecture}}').Trim()
    switch ($dockerInfo) {
        { $_ -in 'linux x86_64','linux amd64' } { $arch = 'amd64'; $suffix = ''; $imageFile = 'spooldesk-image.tar.gz' }
        { $_ -in 'linux aarch64','linux arm64' } { $arch = 'arm64'; $suffix = '_ARM64'; $imageFile = 'spooldesk-image-arm64.tar.gz' }
        default { throw 'Start Docker Desktop with Linux containers (amd64 or arm64).' }
    }
    Invoke-Docker compose version
    # Prevent unrelated shell settings from overriding this installation's .env.
    foreach ($name in @('POSTGRES_PASSWORD','POSTGRES_DB','POSTGRES_USER','SPOOLDESK_IMAGE',
        'SPOOLDESK_ENV_FILE','SPOOLDESK_PORT','SPOOLDESK_INSTALLATION_ID','COMPOSE_FILE',
        'COMPOSE_PROJECT_NAME','COMPOSE_PROFILES','COMPOSE_ENV_FILES','DOCKER_DEFAULT_PLATFORM')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    $image = (Get-Content -LiteralPath "package/IMAGE_TAG$suffix" -Raw).Trim()
    $expectedId = (Get-Content -LiteralPath "package/IMAGE_ID$suffix" -Raw).Trim()
    if ($image -notmatch "^spooldesk:rc-[0-9a-f]{40}-$arch`$" -or $expectedId -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Invalid image identity.'
    }
    # The checked archive links these two digests to the same image: classic
    # Docker reports its config ID; containerd reports its OCI manifest ID.
    $acceptedIds = @(Get-Content -LiteralPath "package/IMAGE_IDS$suffix")
    if ($acceptedIds.Count -ne 2 -or $expectedId -notin $acceptedIds) { throw 'Invalid accepted image identities.' }
    foreach ($id in $acceptedIds) {
        if ($id -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Invalid accepted image identity.' }
    }
    Write-Host "[3/6] Loading the tested $arch image..."
    Invoke-Docker load --input "package/$imageFile"
    $loadedId = (Invoke-Docker image inspect $image --format '{{.Id}}').Trim()
    $loadedPlatform = (Invoke-Docker image inspect $image --format '{{.Os}}/{{.Architecture}}').Trim()
    if ($loadedId -notin $acceptedIds -or $loadedPlatform -ne "linux/$arch") {
        throw "Loaded image differs from the tested image. Actual: $loadedId ($loadedPlatform); expected one of: $($acceptedIds -join ', ')."
    }
    # Atomic engine-wide lock: two double-clicks cannot create separate configs
    # for the same Compose project. Only the container ID created here is removed.
    $lockContainer = (Invoke-Docker create --name spooldesk-installer-lock --network none `
        --label io.spooldesk.installer=1 --entrypoint python $image -c pass).Trim()
    $projectDir = Join-Path $bundle 'spooldesk'
    $containers = @(Invoke-Docker ps -aq --filter label=com.docker.compose.project=spooldesk)
    $volumes = @(Invoke-Docker volume ls -q --filter label=com.docker.compose.project=spooldesk)
    if ($containers.Count -gt 0 -or $volumes.Count -gt 0) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectDir 'INSTALLATION_ID') -PathType Leaf)) {
            throw 'An existing spooldesk installation was found. Nothing changed. Use the upgrade/backup procedure instead of creating another database.'
        }
        $installId = (Get-Content -LiteralPath (Join-Path $projectDir 'INSTALLATION_ID') -Raw).Trim()
        if ($installId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid installation marker.' }
        foreach ($volume in $volumes) {
            $labels = (Invoke-Docker volume inspect $volume --format '{{json .Labels}}') | ConvertFrom-Json
            $owner = if ($null -ne $labels -and $labels.PSObject.Properties['io.spooldesk.installation']) { $labels.'io.spooldesk.installation' } else { '' }
            if ($owner -ne $installId) { throw 'Existing storage belongs to another installation. Nothing changed.' }
        }
        foreach ($container in $containers) {
            $labels = (Invoke-Docker inspect $container --format '{{json .Config.Labels}}') | ConvertFrom-Json
            $directory = if ($null -ne $labels -and $labels.PSObject.Properties['com.docker.compose.project.working_dir']) { $labels.'com.docker.compose.project.working_dir' } else { '' }
            if ($directory.Replace('\','/').TrimEnd('/') -ne $projectDir.Replace('\','/').TrimEnd('/')) {
                throw 'Existing containers belong to another folder. Open the installer in that original folder.'
            }
        }
    }
    Write-Host '[4/6] Configure your installation...'
    $terminal = @('-i')
    if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $terminal = @('-it') }
    & docker run --rm @terminal --network none --read-only --cap-drop ALL --security-opt no-new-privileges `
        -e PYTHONUTF8=1 -e PYTHONIOENCODING=utf-8 `
        --user 0:0 --mount "type=bind,source=$bundle,target=/bundle" --entrypoint python `
        $image /bundle/installer/configure.py --arch $arch
    if ($LASTEXITCODE -eq 3) { return }
    if ($LASTEXITCODE -ne 0) { throw 'Configuration did not finish. Check the message above and run this file again.' }
    Set-Location -LiteralPath $projectDir
    $compose = @('compose','--project-directory',$projectDir,'--env-file',(Join-Path $projectDir '.env'),
        '-f',(Join-Path $projectDir 'docker-compose.yml'),'-p','spooldesk')
    Write-Host '[5/6] Starting PostgreSQL and Spooldesk...'
    if ($containers.Count -eq 0) {
        $url = (Get-Content -LiteralPath 'OPEN_URL' -Raw).Trim()
        if ($url -notmatch '^http://localhost:([0-9]{4,5})$') { throw 'Invalid dashboard address.' }
        $port = [int]$Matches[1]
        $listener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback, $port)
        try { $listener.Start() } catch { throw "Port $port is unavailable. Stop the application using it, then run this file again. Saved settings are preserved. See TROUBLESHOOTING.md." }
        finally { $listener.Stop() }
    }
    if ($containers.Count -eq 0 -and $volumes.Count -eq 0) { Invoke-Docker pull postgres:16-alpine }
    Invoke-Docker @compose up -d --no-build --no-recreate --pull never dashboard
    Write-Host '[6/6] Waiting for the dashboard...'
    $containerId = (Invoke-Docker @compose ps -q dashboard).Trim()
    $healthy = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        $health = (Invoke-Docker inspect $containerId --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}').Trim()
        if ($health -eq 'healthy') { $healthy = $true; break }
        if ($health -in 'unhealthy','none') { break }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) { throw 'Dashboard did not become healthy. Keep this folder and inspect dashboard/postgres logs in Docker Desktop. Run this file again after resolving the error.' }
    $url = (Get-Content -LiteralPath 'OPEN_URL' -Raw).Trim()
    if ($url -notmatch '^http://localhost:[0-9]{4,5}$') { throw 'Invalid dashboard address.' }
    Write-Host "`nReady: $url`nLogin: admin@local`nUse the password you saved. Run the same file next time; keep this installation folder.`n"
    if ($env:SPOOLDESK_INSTALLER_NO_BROWSER -ne '1') {
        try { Start-Process $url } catch { Write-Host "Open $url in your browser." }
    }
} catch {
    Write-Host "`nInstallation stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Existing data was not removed. Keep the folder and retry after resolving the error.'
    $exitCode = 1
} finally {
    if ($lockContainer) { & docker rm $lockContainer | Out-Null }
    if ($env:SPOOLDESK_INSTALLER_NO_PAUSE -ne '1') { [void](Read-Host 'Press Enter to close') }
}
exit $exitCode
