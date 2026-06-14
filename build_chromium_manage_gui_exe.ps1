$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
Set-Location $projectRoot

function Stop-ProjectBuildProcesses {
    param(
        [string]$RootPath
    )

    $normalizedRoot = [System.IO.Path]::GetFullPath($RootPath)
    $targets = Get-Process -Name ChromiumProfileManager, ChromiumMcpDaemon, ChromiumMcpWorker -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Path -and [System.IO.Path]::GetFullPath($_.Path).StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)
        }

    if ($targets) {
        $targets | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
}

function Invoke-ExternalChecked {
    param(
        [scriptblock]$Command,
        [string]$StepName
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$StepName failed with exit code $LASTEXITCODE"
    }
}

function Copy-BuildOutput {
    param(
        [string]$SourcePath,
        [string]$DestinationPath
    )

    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null
    & robocopy $SourcePath $DestinationPath /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed from '$SourcePath' to '$DestinationPath' with exit code $LASTEXITCODE"
    }
}

$pythonExe = "python"
$guiScriptPath = Join-Path $projectRoot "run_gui.py"
$daemonScriptPath = Join-Path $projectRoot "chromium_advanced\mcp_daemon.py"
$workerScriptPath = Join-Path $projectRoot "chromium_advanced\mcp_server.py"
$iconPath = Join-Path $projectRoot "resources\chromium_profile_manager.ico"
$specPath = Join-Path $projectRoot "ChromiumProfileManager.spec"
$daemonSpecPath = Join-Path $projectRoot "ChromiumMcpDaemon.spec"
$workerSpecPath = Join-Path $projectRoot "ChromiumMcpWorker.spec"
$stageBuildRoot = Join-Path $projectRoot "build_stage"
$stageDistRoot = Join-Path $projectRoot "dist_stage"
$finalBuildRoot = Join-Path $projectRoot "build"
$finalDistRoot = Join-Path $projectRoot "dist"

Stop-ProjectBuildProcesses -RootPath $projectRoot

Remove-Item -Recurse -Force $stageBuildRoot -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $stageDistRoot -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $finalBuildRoot -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $finalDistRoot -ErrorAction SilentlyContinue
Remove-Item -Force $specPath -ErrorAction SilentlyContinue
Remove-Item -Force $daemonSpecPath -ErrorAction SilentlyContinue
Remove-Item -Force $workerSpecPath -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $stageBuildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $stageDistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $finalDistRoot | Out-Null

Invoke-ExternalChecked -StepName "Build ChromiumProfileManager" -Command {
    & $pythonExe -m PyInstaller `
      -y `
      --workpath $stageBuildRoot `
      --distpath $stageDistRoot `
      --noconsole `
      --onefile `
      --name "ChromiumProfileManager" `
      --icon $iconPath `
      --copy-metadata "fastmcp" `
      --collect-all "patchright" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      --add-data "resources;resources" `
      $guiScriptPath
}

Invoke-ExternalChecked -StepName "Build ChromiumMcpDaemon" -Command {
    & $pythonExe -m PyInstaller `
      -y `
      --workpath $stageBuildRoot `
      --distpath $stageDistRoot `
      --onedir `
      --name "ChromiumMcpDaemon" `
      --copy-metadata "fastmcp" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      $daemonScriptPath
}

Invoke-ExternalChecked -StepName "Build ChromiumMcpWorker" -Command {
    & $pythonExe -m PyInstaller `
      -y `
      --workpath $stageBuildRoot `
      --distpath $stageDistRoot `
      --onedir `
      --name "ChromiumMcpWorker" `
      --copy-metadata "fastmcp" `
      --collect-all "patchright" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      $workerScriptPath
}

Copy-BuildOutput -SourcePath $stageDistRoot -DestinationPath $finalDistRoot
