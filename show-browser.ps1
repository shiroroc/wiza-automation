<#
  Finds the automation Chrome window and actually brings it to the front.

      powershell -ExecutionPolicy Bypass -File show-browser.ps1

  Every Chrome window looks alike in the taskbar, and this one is a separate
  profile with no bookmarks or history to recognise it by. So identify it the
  way the scripts do - by the --user-data-dir on its command line - and raise
  that exact window.

  Raising it is not one API call. Windows refuses SetForegroundWindow from a
  process that is not already in the foreground: it returns success and nothing
  moves. The reliable route is to attach to the current foreground thread's
  input queue first, which is what the -Force path below does. If even that is
  refused, the taskbar button is flashed instead so there is something to click.
#>
param(
    [string]$ProfileDir = "$PSScriptRoot\.chrome-profile",
    [switch]$Center      # also move the window to the middle of the screen
)

$ErrorActionPreference = "Stop"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinApi {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after,
                                    int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int x, int y,
                                    int w, int h, bool repaint);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();

    [StructLayout(LayoutKind.Sequential)]
    public struct FLASHWINFO {
        public uint cbSize; public IntPtr hwnd; public uint dwFlags;
        public uint uCount; public uint dwTimeout;
    }
    [DllImport("user32.dll")] public static extern bool FlashWindowEx(ref FLASHWINFO pwfi);

    public static bool ForceForeground(IntPtr hWnd) {
        uint fgThread = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
        uint ourThread = GetCurrentThreadId();
        // Sharing the foreground thread's input queue is what buys us the right
        // to change the foreground window at all.
        if (fgThread != ourThread) AttachThreadInput(fgThread, ourThread, true);
        BringWindowToTop(hWnd);
        bool ok = SetForegroundWindow(hWnd);
        if (fgThread != ourThread) AttachThreadInput(fgThread, ourThread, false);
        return ok;
    }

    public static void Flash(IntPtr hWnd) {
        FLASHWINFO fi = new FLASHWINFO();
        fi.cbSize = (uint)Marshal.SizeOf(fi);
        fi.hwnd = hWnd;
        fi.dwFlags = 3 | 12;   // FLASHW_ALL | FLASHW_TIMERNOFG
        fi.uCount = 8;
        fi.dwTimeout = 0;
        FlashWindowEx(ref fi);
    }
}
"@

$SW_RESTORE = 9
$SW_SHOW = 5
$HWND_TOPMOST = [IntPtr](-1)
$HWND_NOTOPMOST = [IntPtr](-2)
$SWP_NOMOVE = 0x0002
$SWP_NOSIZE = 0x0001
$SWP_SHOWWINDOW = 0x0040

$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and $_.CommandLine -like "*--user-data-dir=*$ProfileDir*" })

if ($procs.Count -eq 0) {
    Write-Host "The automation Chrome is not running." -ForegroundColor Yellow
    Write-Host "Profile looked for: $ProfileDir"
    Write-Host ""
    Write-Host "Start it with:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File launch-chrome.ps1" -ForegroundColor White
    exit 1
}

$handle = [IntPtr]::Zero
$pidFound = 0
foreach ($m in $procs) {
    $proc = Get-Process -Id $m.ProcessId -ErrorAction SilentlyContinue
    if ($proc -and $proc.MainWindowHandle -ne 0) {
        $handle = $proc.MainWindowHandle
        $pidFound = $m.ProcessId
        break
    }
}

if ($handle -eq [IntPtr]::Zero) {
    Write-Host "Found the automation Chrome but it has no visible window yet." -ForegroundColor Yellow
    Write-Host "PIDs: $($procs.ProcessId -join ', '). Give it a moment and re-run."
    exit 1
}

# Un-minimise, then take three escalating runs at the foreground.
if ([WinApi]::IsIconic($handle)) { [void][WinApi]::ShowWindow($handle, $SW_RESTORE) }
[void][WinApi]::ShowWindow($handle, $SW_SHOW)

[void][WinApi]::ForceForeground($handle)

if ([WinApi]::GetForegroundWindow() -ne $handle) {
    # Briefly pin it above everything, then release - this shifts z-order even
    # when the foreground request itself is denied.
    [void][WinApi]::SetWindowPos($handle, $HWND_TOPMOST, 0, 0, 0, 0,
                                 $SWP_NOMOVE -bor $SWP_NOSIZE -bor $SWP_SHOWWINDOW)
    Start-Sleep -Milliseconds 120
    [void][WinApi]::SetWindowPos($handle, $HWND_NOTOPMOST, 0, 0, 0, 0,
                                 $SWP_NOMOVE -bor $SWP_NOSIZE -bor $SWP_SHOWWINDOW)
    [void][WinApi]::ForceForeground($handle)
}

if ($Center) {
    Add-Type -AssemblyName System.Windows.Forms
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $w = [int]($b.Width * 0.8); $h = [int]($b.Height * 0.85)
    [void][WinApi]::MoveWindow($handle,
        $b.X + [int](($b.Width - $w) / 2), $b.Y + [int](($b.Height - $h) / 2), $w, $h, $true)
    [void][WinApi]::ForceForeground($handle)
}

$won = ([WinApi]::GetForegroundWindow() -eq $handle)
$proc = Get-Process -Id $pidFound -ErrorAction SilentlyContinue

if ($won) {
    Write-Host "The automation Chrome is now in front of you." -ForegroundColor Green
} else {
    # Be honest rather than claiming success: flash the taskbar button instead.
    [WinApi]::Flash($handle)
    Write-Host "Windows would not let a background script steal focus." -ForegroundColor Yellow
    Write-Host "Its taskbar button is FLASHING now - click that." -ForegroundColor Yellow
}

Write-Host "  Title   : $($proc.MainWindowTitle)"
Write-Host "  Profile : $ProfileDir"
Write-Host ""
Write-Host "How to spot it yourself, any time:" -ForegroundColor Cyan
Write-Host " - the tab is titled 'WIZA AUTOMATION BROWSER'"
Write-Host " - it is the only Chrome with no bookmarks bar and no history"
Write-Host " - Alt+Tab and look for the gear icon, or click its taskbar button"
Write-Host ""
Write-Host "Tip: -Center moves it to the middle of the screen if it is somewhere odd:" -ForegroundColor DarkGray
Write-Host "  powershell -File show-browser.ps1 -Center" -ForegroundColor DarkGray
