# Keeps the app's local model loaded in Bionic with the context stock calls need
# (docs/LOCAL_AI.md). Run at sign-in and every 10 minutes by the scheduled task
# "TradingHelper-Bionic-Model". Bionic loads nothing after a restart until asked, and the
# bridge offers only loaded models, so without this the app sees no model after a reboot.
#
# Idempotent: does nothing when the model is loaded as configured, and never reloads a
# model that is busy (a reload would cut off the app's request in flight).
param(
    [string]$Model = 'qwen/qwen3.8-27b',
    [int]$Context = 32768,
    [int]$Parallel = 1
)
$ErrorActionPreference = 'Stop'
$lms = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
$log = Join-Path $PSScriptRoot 'ensure_bionic_model.log'

function Say([string]$text) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $text
    Add-Content -Path $log -Value $line -Encoding utf8
    if ((Get-Item $log).Length -gt 256KB) {
        Get-Content $log -Tail 500 | Set-Content "$log.tmp" -Encoding utf8
        Move-Item "$log.tmp" $log -Force
    }
}

# Bionic has no start-at-login entry: start it after a reboot or a crash, then wait for its
# API server (port 5000, http-server-config.json, which it starts on launch).
if (-not (Get-Process -Name Bionic -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'C:\Program Files\Bionic\Bionic.exe' -WindowStyle Minimized
    Say 'Started Bionic'
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline -and -not (Get-NetTCPConnection -LocalPort 5000 `
            -State Listen -ErrorAction SilentlyContinue)) {
        Start-Sleep -Seconds 5
    }
}

try {
    $loaded = @(& $lms ps --json 2>$null | ConvertFrom-Json)
} catch {
    Say "Bionic is not answering yet: $($_.Exception.Message)"
    exit 0
}
$mine = $loaded | Where-Object { $_.identifier -eq $Model }
if ($mine -and $mine.contextLength -eq $Context -and $mine.parallel -eq $Parallel) {
    exit 0
}
if ($mine -and $mine.status -ne 'idle') {
    Say "$Model is $($mine.status) at context $($mine.contextLength); retrying on the next run"
    exit 0
}
if ($mine) {
    & $lms unload $Model | Out-Null
}
& $lms load $Model -c $Context --parallel $Parallel --gpu max --identifier $Model -y | Out-Null
$after = @(& $lms ps --json 2>$null | ConvertFrom-Json) | Where-Object { $_.identifier -eq $Model }
if ($after -and $after.contextLength -eq $Context) {
    Say "Loaded $Model at context $Context"
} else {
    Say "Load of $Model at context $Context did not take effect"
    exit 1
}
