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
        [string]$DestinationPath,
        [int]$MaxAttempts = 6,
        [int]$RetryDelaySeconds = 2
    )

    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        & robocopy $SourcePath $DestinationPath /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -le 7) {
            return
        }
        if ($attempt -ge $MaxAttempts) {
            throw "robocopy failed from '$SourcePath' to '$DestinationPath' with exit code $LASTEXITCODE after $attempt attempts"
        }
        Start-Sleep -Seconds $RetryDelaySeconds
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
      --hidden-import "selenium.webdriver.common.action_chains" `
      --hidden-import "selenium.webdriver.common.actions.action_builder" `
      --hidden-import "selenium.webdriver.common.actions.pointer_input" `
      --hidden-import "selenium.webdriver.common.actions.mouse_button" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      --add-data "resources;resources" `
      --add-data "docs;docs" `
      --add-data "README.md;." `
      --add-data "README_zh.md;." `
      --add-data "chromium_profiles.example.json;." `
      $guiScriptPath
}

Invoke-ExternalChecked -StepName "Build ChromiumMcpDaemon" -Command {
    & $pythonExe -m PyInstaller `
      -y `
      --workpath $stageBuildRoot `
      --distpath $stageDistRoot `
      --noconsole `
      --onedir `
      --name "ChromiumMcpDaemon" `
      --icon $iconPath `
      --copy-metadata "fastmcp" `
      --collect-all "patchright" `
      --hidden-import "selenium.webdriver.common.action_chains" `
      --hidden-import "selenium.webdriver.common.actions.action_builder" `
      --hidden-import "selenium.webdriver.common.actions.pointer_input" `
      --hidden-import "selenium.webdriver.common.actions.mouse_button" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      --add-data "resources;resources" `
      $daemonScriptPath
}

Invoke-ExternalChecked -StepName "Build ChromiumMcpWorker" -Command {
    & $pythonExe -m PyInstaller `
      -y `
      --workpath $stageBuildRoot `
      --distpath $stageDistRoot `
      --noconsole `
      --onedir `
      --name "ChromiumMcpWorker" `
      --icon $iconPath `
      --copy-metadata "fastmcp" `
      --collect-all "patchright" `
      --hidden-import "selenium.webdriver.common.action_chains" `
      --hidden-import "selenium.webdriver.common.actions.action_builder" `
      --hidden-import "selenium.webdriver.common.actions.pointer_input" `
      --hidden-import "selenium.webdriver.common.actions.mouse_button" `
      --collect-data "rich" `
      --collect-submodules "rich._unicode_data" `
      --add-data "resources;resources" `
      $workerScriptPath
}

Copy-BuildOutput -SourcePath $stageDistRoot -DestinationPath $finalDistRoot

$releaseDocEnPath = Join-Path $projectRoot "docs\05-reference\RELEASE_README.md"
$releaseDocZhPath = Join-Path $projectRoot "docs\05-reference\RELEASE_README_zh.md"
$aiRunbookPath = Join-Path $projectRoot "docs\01-getting-started\AI_INSTALLATION_RUNBOOK.md"
$skillTemplatesSource = Join-Path $projectRoot "docs\skill_templates"
$releaseManifestPath = Join-Path $projectRoot "release-manifest.json"
$exampleConfigPath = Join-Path $projectRoot "chromium_profiles.example.json"
$readmeEnPath = Join-Path $projectRoot "README.md"
$readmeZhPath = Join-Path $projectRoot "README_zh.md"
$resourcesSource = Join-Path $projectRoot "resources"

if (Test-Path $releaseDocEnPath) {
    Copy-Item -Force $releaseDocEnPath (Join-Path $finalDistRoot "RELEASE_README.md")
}
if (Test-Path $releaseDocZhPath) {
    Copy-Item -Force $releaseDocZhPath (Join-Path $finalDistRoot "RELEASE_README_zh.md")
}
if (Test-Path $aiRunbookPath) {
    Copy-Item -Force $aiRunbookPath (Join-Path $finalDistRoot "AI_INSTALLATION_RUNBOOK.md")
}
if (Test-Path $releaseManifestPath) {
    Copy-Item -Force $releaseManifestPath (Join-Path $finalDistRoot "release-manifest.json")
}
if (Test-Path $exampleConfigPath) {
    Copy-Item -Force $exampleConfigPath (Join-Path $finalDistRoot "chromium_profiles.example.json")
}
if (Test-Path $readmeEnPath) {
    Copy-Item -Force $readmeEnPath (Join-Path $finalDistRoot "README.md")
}
if (Test-Path $readmeZhPath) {
    Copy-Item -Force $readmeZhPath (Join-Path $finalDistRoot "README_zh.md")
}
if (Test-Path $skillTemplatesSource) {
    Copy-BuildOutput -SourcePath $skillTemplatesSource -DestinationPath (Join-Path $finalDistRoot "skill_templates")
}
if (Test-Path $resourcesSource) {
    Copy-BuildOutput -SourcePath $resourcesSource -DestinationPath (Join-Path $finalDistRoot "resources")
}
