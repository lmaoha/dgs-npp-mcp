param(
    [string]$ExePath = (Join-Path $PSScriptRoot "notepad-plus-plus-headless\PowerEditor\bin64\notepad++.exe"),
    [string]$ProbeFile = (Join-Path $PSScriptRoot "notepad-plus-plus-headless\README.md"),
    [ValidateSet("All", "Normal", "Headless")]
    [string]$Mode = "All",
    [ValidateSet("-headless", "--headless")]
    [string]$HeadlessArgument = "-headless",
    [ValidateRange(1, 60)]
    [int]$StartupTimeoutSeconds = 15,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$exe = (Resolve-Path -LiteralPath $ExePath).Path
$probe = (Resolve-Path -LiteralPath $ProbeFile).Path

if (-not ("NppHeadlessProbe.Native" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace NppHeadlessProbe
{
    public sealed class Snapshot
    {
        public IntPtr TopWindow { get; set; }
        public string WindowClass { get; set; }
        public bool Visible { get; set; }
        public int ScintillaCount { get; set; }
        public ulong MaxTextLength { get; set; }
    }

    public static class Native
    {
        private const uint SCI_GETTEXTLENGTH = 2183;
        private const uint SMTO_ABORTIFHUNG = 0x0002;
        private const uint WM_CLOSE = 0x0010;

        private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassNameW(IntPtr hwnd, StringBuilder className, int maxCount);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hwnd);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr SendMessageTimeoutW(
            IntPtr hwnd,
            uint message,
            UIntPtr wParam,
            IntPtr lParam,
            uint flags,
            uint timeout,
            out UIntPtr result);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool PostMessageW(IntPtr hwnd, uint message, UIntPtr wParam, IntPtr lParam);

        private static string WindowClass(IntPtr hwnd)
        {
            StringBuilder value = new StringBuilder(256);
            GetClassNameW(hwnd, value, value.Capacity);
            return value.ToString();
        }

        public static Snapshot Capture(int processId, string expectedWindowClass)
        {
            IntPtr top = IntPtr.Zero;
            EnumWindows(delegate(IntPtr hwnd, IntPtr unused)
            {
                uint owner;
                GetWindowThreadProcessId(hwnd, out owner);
                if (owner == (uint)processId && WindowClass(hwnd) == expectedWindowClass)
                {
                    top = hwnd;
                    return false;
                }
                return true;
            }, IntPtr.Zero);

            if (top == IntPtr.Zero)
                return null;

            int scintillaCount = 0;
            ulong maxTextLength = 0;
            EnumChildWindows(top, delegate(IntPtr hwnd, IntPtr unused)
            {
                if (WindowClass(hwnd) != "Scintilla")
                    return true;

                scintillaCount++;
                UIntPtr result;
                if (SendMessageTimeoutW(
                    hwnd,
                    SCI_GETTEXTLENGTH,
                    UIntPtr.Zero,
                    IntPtr.Zero,
                    SMTO_ABORTIFHUNG,
                    2000,
                    out result) != IntPtr.Zero)
                {
                    ulong length = result.ToUInt64();
                    if (length > maxTextLength)
                        maxTextLength = length;
                }
                return true;
            }, IntPtr.Zero);

            return new Snapshot
            {
                TopWindow = top,
                WindowClass = WindowClass(top),
                Visible = IsWindowVisible(top),
                ScintillaCount = scintillaCount,
                MaxTextLength = maxTextLength
            };
        }

        public static Snapshot WaitForSnapshot(int processId, string expectedWindowClass, int timeoutMilliseconds)
        {
            Stopwatch timer = Stopwatch.StartNew();
            while (timer.ElapsedMilliseconds < timeoutMilliseconds)
            {
                Snapshot snapshot = Capture(processId, expectedWindowClass);
                if (snapshot != null && snapshot.ScintillaCount > 0 && snapshot.MaxTextLength > 0)
                    return snapshot;
                Thread.Sleep(100);
            }
            return Capture(processId, expectedWindowClass);
        }

        public static bool RequestClose(IntPtr topWindow)
        {
            return PostMessageW(topWindow, WM_CLOSE, UIntPtr.Zero, IntPtr.Zero);
        }
    }
}
'@
}

function Invoke-NppModeProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [bool]$ExpectedVisible,
        [string]$ExtraArgument = ""
    )

    $arguments = "-multiInst -nosession"
    if ($ExtraArgument) {
        $arguments += " $ExtraArgument"
    }
    $arguments += ' "' + $probe.Replace('"', '\"') + '"'

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $exe
    $startInfo.Arguments = $arguments
    $startInfo.WorkingDirectory = Split-Path -Parent $exe
    $startInfo.UseShellExecute = $false

    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $expectedWindowClass = if ($Name -eq "Headless") { "DGS.Notepad++" } else { "Notepad++" }
        $snapshot = [NppHeadlessProbe.Native]::WaitForSnapshot(
            $process.Id,
            $expectedWindowClass,
            $StartupTimeoutSeconds * 1000
        )
        if ($null -eq $snapshot) {
            throw "$Name mode did not create a $expectedWindowClass top-level window"
        }
        if ($snapshot.WindowClass -ne $expectedWindowClass) {
            throw "$Name mode class was $($snapshot.WindowClass), expected $expectedWindowClass"
        }
        if ($snapshot.Visible -ne $ExpectedVisible) {
            throw "$Name mode visibility was $($snapshot.Visible), expected $ExpectedVisible"
        }
        if ($snapshot.ScintillaCount -lt 1) {
            throw "$Name mode did not create a Scintilla child window"
        }
        if ($snapshot.MaxTextLength -lt 1) {
            throw "$Name mode Scintilla buffer did not load the probe document"
        }

        $closePosted = [NppHeadlessProbe.Native]::RequestClose($snapshot.TopWindow)
        if (-not $closePosted) {
            throw "$Name mode rejected WM_CLOSE"
        }

        $cleanExit = $process.WaitForExit(5000)
        if (-not $cleanExit) {
            throw "$Name mode did not exit within 5 seconds after WM_CLOSE"
        }

        [pscustomobject]@{
            Mode = $Name
            Pid = $process.Id
            TopHwnd = $snapshot.TopWindow.ToInt64()
            WindowClass = $snapshot.WindowClass
            Visible = $snapshot.Visible
            ScintillaCount = $snapshot.ScintillaCount
            MaxTextLength = $snapshot.MaxTextLength
            ClosePosted = $closePosted
            CleanExit = $cleanExit
            ExitCode = $process.ExitCode
        }
    }
    finally {
        if (-not $process.HasExited) {
            $process.Kill()
            $null = $process.WaitForExit(5000)
        }
        $process.Dispose()
    }
}

$results = @()
if ($Mode -in @("All", "Normal")) {
    $results += Invoke-NppModeProbe -Name "Normal" -ExpectedVisible $true
}
if ($Mode -in @("All", "Headless")) {
    $results += Invoke-NppModeProbe -Name "Headless" -ExpectedVisible $false -ExtraArgument $HeadlessArgument
}

if ($Json) {
    $results | ConvertTo-Json -Depth 3
}
else {
    $results | Format-Table -AutoSize
}
