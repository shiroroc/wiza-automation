<#
  Launches Chrome with a dedicated, PERMANENT automation profile and a CDP port.

  The profile lives in .chrome-profile\ next to this script and persists forever.
  You sign in to LinkedIn and Wiza ONCE, in the window this opens; both stay
  signed in across reboots and across every future run. After that first time,
  running this script is all you ever do - no logging in again.

  It is a separate profile from your everyday Chrome for one reason only:
  Chrome 136+ refuses --remote-debugging-port on the default profile directory.
  Your everyday logins do not transfer into it, which is why there is a one-time
  sign-in - not a per-run one.

  Delete .chrome-profile\ only if you want to start over from a clean slate.
#>
param(
    [int]$Port = 9222,
    [string]$ProfileDir = "$PSScriptRoot\.chrome-profile",
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

if (-not $ChromePath) {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    $ChromePath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $ChromePath) { throw "Could not find chrome.exe. Pass -ChromePath explicitly." }

# Has anyone installed an extension in this profile yet?
#
# Preferences is NOT a usable signal - Chrome writes it the instant it starts,
# so it says "set up" before you have signed into anything. A user-installed
# extension is the honest one: until Wiza is installed, setup is unfinished.
# These four IDs ship with Chrome itself and do not count.
$BundledExtensions = @(
    "nmmhkkegccagdldgiimedpiccmgmieda",   # Chrome Web Store payments
    "ghbmnnjooekpmoecnnnilnnbdlolhkhi",   # Google Docs Offline
    "lmjegmlicamnimmfhcmpkclmigmmcbeh",   # Chrome Cast / media router
    "fheoggkfdfchfphceeifdbepaooicaho"    # bundled component
)
$extDir = Join-Path $ProfileDir "Default\Extensions"
$userExtensions = @()
if (Test-Path $extDir) {
    $userExtensions = @(Get-ChildItem $extDir -Directory -ErrorAction SilentlyContinue |
                        Where-Object { $BundledExtensions -notcontains $_.Name })
}
$firstRun = ($userExtensions.Count -eq 0)

if (-not (Test-Path $ProfileDir)) { New-Item -ItemType Directory -Path $ProfileDir | Out-Null }

# What to do next, printed on EVERY path - including when Chrome is already up.
# (Skipping this on the already-running path was a bug: run the script twice and
#  the setup checklist silently never appeared.)
# Record exactly what we launched. Every Python entry point reads this back and
# refuses to touch a browser that does not match it.
function Write-Lock {
    param([string]$Chrome, [string]$ProfilePath, [int]$PortNum)

    $lock = [ordered]@{
        chrome_path = $Chrome
        profile_dir = $ProfilePath
        port        = $PortNum
        cdp_url     = "http://127.0.0.1:$PortNum"
        written_at  = (Get-Date).ToString("s")
    }
    $lockFile = Join-Path $PSScriptRoot ".wiza-lock.json"
    # WriteAllText with an explicit BOM-less encoder: Out-File -Encoding utf8 on
    # Windows PowerShell 5.1 prepends a BOM, which json.load() rejects - and a
    # lock that cannot be read is a lock that is not protecting anything.
    $json = $lock | ConvertTo-Json
    [System.IO.File]::WriteAllText($lockFile, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Locked    : $lockFile" -ForegroundColor DarkGray
}


function Show-NextSteps {
    param([bool]$IsFirstRun)

    Write-Host ""
    if ($IsFirstRun) {
        Write-Host "FIRST-TIME SETUP for this profile - do these once, in the Chrome window:" -ForegroundColor Cyan
        Write-Host "  1. Sign in to LinkedIn."
        Write-Host "  2. Install the Wiza extension from the Chrome Web Store and sign in to it."
        Write-Host "     -> https://chromewebstore.google.com/  then search for: Wiza"
        Write-Host "  3. Open any LinkedIn profile."
        Write-Host "  4. Open the Wiza side panel and PIN it (the pin icon in its header)."
        Write-Host "     The script reads that panel. If it is closed, there is nothing to read."
        Write-Host ""
        Write-Host "  You will not have to do any of this again. This profile keeps both" -ForegroundColor Cyan
        Write-Host "  logins permanently - from then on, this script is the whole setup." -ForegroundColor Cyan
    } else {
        Write-Host "This profile already has an extension installed." -ForegroundColor Green
        Write-Host "If that is Wiza and you are signed into LinkedIn, just make sure the"
        Write-Host "Wiza side panel is open and pinned, and carry on."
    }
    Write-Host ""
    Write-Host "doctor.py is the real check - it verifies the LinkedIn session AND"
    Write-Host "whether the side panel is actually visible to the script:"
    Write-Host "  .\.venv\Scripts\python.exe doctor.py" -ForegroundColor White
    Write-Host "  .\.venv\Scripts\python.exe wiza_auto.py --dry-run"
}

# If something already holds the port, find out WHAT. Assuming it is ours is
# how a script ends up driving your everyday Chrome and your personal LinkedIn
# account, so the owning process is checked against our profile directory.
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    $ownerPid = ($inUse | Select-Object -First 1).OwningProcess
    $cmdLine = ""
    try {
        $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction Stop).CommandLine
    } catch { }

    $isOurs = $cmdLine -and ($cmdLine -like "*$ProfileDir*")

    if (-not $isOurs) {
        Write-Host ""
        Write-Host "REFUSING TO CONTINUE - port $Port is held by a different browser." -ForegroundColor Red
        Write-Host ""
        Write-Host "  owning PID  : $ownerPid"
        if ($cmdLine) { Write-Host "  command line: $($cmdLine.Substring(0, [Math]::Min(150, $cmdLine.Length)))" }
        Write-Host "  expected    : a Chrome using --user-data-dir=$ProfileDir"
        Write-Host ""
        Write-Host "  This is most likely your everyday Chrome. If the script attached to it," -ForegroundColor Yellow
        Write-Host "  it would drive your personal LinkedIn account instead of the automation" -ForegroundColor Yellow
        Write-Host "  one. Close that browser and re-run this script, or pass a free port:" -ForegroundColor Yellow
        Write-Host "      powershell -File launch-chrome.ps1 -Port 9333"
        Write-Host "  (then set browser.cdp_url in config.yaml to match)."
        exit 1
    }

    Write-Host "Automation Chrome is already running on port $Port - nothing to launch." -ForegroundColor Green
    Write-Host "Profile   : $ProfileDir"
    Write-Lock -Chrome $ChromePath -ProfilePath $ProfileDir -PortNum $Port
    Show-NextSteps -IsFirstRun $firstRun
    exit 0
}

# Opening the home page gives the window a distinctive title, so it is findable
# in the taskbar and alt-tab instead of being one more "New Tab - Google Chrome".
$homePage = Join-Path $PSScriptRoot "browser-home.html"

$chromeArgs = @(
    "--remote-debugging-port=$Port"
    "--user-data-dir=`"$ProfileDir`""
    "--no-first-run"
    "--no-default-browser-check"
    "--restore-last-session"
)
if (Test-Path $homePage) { $chromeArgs += "`"file:///$($homePage -replace '\\','/')`"" }

Write-Host "Chrome    : $ChromePath"
Write-Host "Profile   : $ProfileDir"
Write-Host "Debug port: $Port"
Start-Process -FilePath $ChromePath -ArgumentList $chromeArgs

Start-Sleep -Milliseconds 1500
try {
    $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    Write-Host ""
    Write-Host "CDP is live: $($v.Browser)" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Chrome launched but CDP has not answered on port $Port yet." -ForegroundColor Yellow
    Write-Host "Give it a few seconds, then run doctor.py to confirm."
}

Write-Lock -Chrome $ChromePath -ProfilePath $ProfileDir -PortNum $Port
Show-NextSteps -IsFirstRun $firstRun
