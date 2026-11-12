# Скрипт обновления файлов в GitHub через API (без git)
# Использование:
#   1. Положить токен в D:\Репетиторство DSH\.github_token (одной строкой)
#   2. В .github_repo записать "владелец/репозиторий"
#   3. .\push_to_github.ps1 bot.py sheets_util.py
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)

$ErrorActionPreference = "Stop"
$root = "D:\Репетиторство DSH"
$tokenPath = Join-Path $root ".github_token"
$repoPath = Join-Path $root ".github_repo"

if (-not (Test-Path $tokenPath)) { throw "Нет файла с токеном: $tokenPath" }
if (-not (Test-Path $repoPath)) { throw "Нет файла с репозиторием: $repoPath (формат owner/repo)" }

$token = (Get-Content $tokenPath -Raw).Trim()
$repo = (Get-Content $repoPath -Raw).Trim()
$headers = @{
    Authorization = "Bearer $token"
    Accept        = "application/vnd.github+json"
    "User-Agent"  = "dsh-bot-updater"
    "X-GitHub-Api-Version" = "2022-11-28"
}

$srcDir = Join-Path $root "bot-git"
if (-not $Files -or $Files.Count -eq 0) { throw "Укажите файлы: .\push_to_github.ps1 bot.py" }

foreach ($f in $Files) {
    $local = Join-Path $srcDir $f
    if (-not (Test-Path $local)) { Write-Host "НЕТ файла: $local" -ForegroundColor Red; continue }

    $url = "https://api.github.com/repos/$repo/contents/$f"
    $sha = $null
    try {
        $existing = Invoke-RestMethod -Uri $url -Headers $headers -Method Get
        $sha = $existing.sha
    } catch {
        # файла ещё нет в репозитории
    }

    $bytes = [System.IO.File]::ReadAllBytes($local)
    $content = [Convert]::ToBase64String($bytes)
    $body = @{
        message = "Update $f"
        content = $content
        branch  = "main"
    }
    if ($sha) { $body.sha = $sha }

    try {
        $res = Invoke-RestMethod -Uri $url -Headers $headers -Method Put `
               -Body ($body | ConvertTo-Json -Depth 5) -ContentType "application/json"
        Write-Host ("OK  {0}  -> commit {1}" -f $f, $res.commit.sha.Substring(0,7)) -ForegroundColor Green
    } catch {
        Write-Host ("ОШИБКА {0}: {1}" -f $f, $_.Exception.Message) -ForegroundColor Red
    }
}
