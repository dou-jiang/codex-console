$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$targetBranch = "release-v1.1.2"

Write-Host "==> Repository: $repoRoot"

$currentBranch = (git branch --show-current).Trim()
if (-not $currentBranch) {
    throw "Unable to determine current git branch."
}

if ($currentBranch -ne $targetBranch) {
    Write-Host "==> Switching branch to $targetBranch"
    git switch $targetBranch
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to switch to branch $targetBranch."
    }
} else {
    Write-Host "==> Already on $targetBranch"
}

$statusLines = git status --short
if ($LASTEXITCODE -ne 0) {
    throw "Failed to read git status."
}

if ($statusLines) {
    Write-Host "==> Staging all changes"
    git add -A
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stage changes."
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $commitMessage = "chore: sync release-v1.1.2 ($timestamp)"
    Write-Host "==> Creating commit: $commitMessage"
    git commit -m $commitMessage
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create commit."
    }
} else {
    Write-Host "==> Working tree clean, no new commit needed"
}

Write-Host "==> Pushing $targetBranch to origin"
git push -u origin $targetBranch
if ($LASTEXITCODE -ne 0) {
    throw "Failed to push branch $targetBranch."
}

Write-Host "==> Done"
