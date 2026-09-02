# roomtray — the room display in the Windows tray.
#
# Copy a link or a file, double-click the tray icon, it's on the wall. The icon
# itself is the status: blue = awake, grey = asleep, red = can't reach the Pi.
#
# Windows' own NotifyIcon, no install and no dependencies — this has to run on
# any desktop on the tailnet, including one with no checkout and no Python.
# Anything past the frequent verbs (scroll, saved links) is the web UI's job;
# "Open controller" is one click away.
#
#   powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File roomtray.ps1
#
# See README.md next to this file for the run-at-login shortcut.

param([string]$Target)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Net.Http

$ErrorActionPreference = 'Stop'
$PollSeconds = 30
$StateFile = Join-Path $env:APPDATA 'roomtray\screen.txt'

# --- target ------------------------------------------------------------------

# ponytail: regex, not a TOML parser — targets.toml is flat string keys under
# flat sections. If it ever grows tables or arrays, shell out to `python -m roomctl`.
function Read-Targets($path) {
    $t = @{}; $section = ''; $default = $null
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        $l = $line.Trim()
        if ($l -match '^\[(.+)\]$') { $section = $Matches[1]; $t[$section] = @{}; continue }
        if ($l -match '^(\w+)\s*=\s*"([^"]*)"') {
            if ($section) { $t[$section][$Matches[1]] = $Matches[2] }
            elseif ($Matches[1] -eq 'default') { $default = $Matches[2] }
        }
    }
    @{ targets = $t; default = $default }
}

$targetsPath = if ($env:ROOMCTL_TARGETS) { $env:ROOMCTL_TARGETS }
               else { Join-Path $PSScriptRoot '..\..\roomctl\targets.toml' }
if (-not (Test-Path -LiteralPath $targetsPath)) {
    [Windows.Forms.MessageBox]::Show(
        "No targets file at:`n$targetsPath`n`nCopy roomctl\targets.example.toml to targets.toml, or point ROOMCTL_TARGETS at one.",
        'roomtray', 'OK', 'Error') | Out-Null
    exit 1
}
$conf = Read-Targets $targetsPath
$names = @($conf.targets.Keys)
$name = if ($Target) { $Target } elseif ($conf.default) { $conf.default }
        elseif ($names.Count -eq 1) { $names[0] }
if (-not $name -or -not $conf.targets[$name]) {
    [Windows.Forms.MessageBox]::Show(
        "No usable target in ${targetsPath}: asked for '$name', have: $($names -join ', ')",
        'roomtray', 'OK', 'Error') | Out-Null
    exit 1
}
$Base = $conf.targets[$name].url.TrimEnd('/')
$Headers = @{ Authorization = "Bearer $($conf.targets[$name].token)" }

# --- agent -------------------------------------------------------------------

# ErrorDetails.Message, not Response.GetResponseStream(): Invoke-RestMethod has
# already read that stream to the end, so reading it again yields "".
function Detail($e) {
    $body = $e.ErrorDetails.Message
    if ($body) {
        $d = try { (ConvertFrom-Json $body).detail } catch { $body }
        # FastAPI's 422 detail is a list of objects, not a string. Test for .msg,
        # not for [array]: ConvertFrom-Json unrolls a one-element list to the object.
        return $(if ($d.msg) { (@($d).msg -join '; ') } else { $d })
    }
    $e.Exception.Message
}

function Api($path, $body) {
    Invoke-RestMethod -Method Post -Uri "$Base$path" -Headers $Headers `
        -ContentType 'application/json' -Body (ConvertTo-Json $body -Compress) -TimeoutSec 30
}

# Every action funnels through here: one place that reports failure, and one
# place that refreshes the icon afterwards. Silence on success is the point —
# the display itself is the feedback.
function Act($what, $block) {
    try { & $block }
    catch { Notify $what (Detail $_) 'Error' }
    Poll
}

function Notify($title, $text, $icon) {
    $tray.BalloonTipTitle = $title
    $tray.BalloonTipText = $text
    $tray.BalloonTipIcon = $icon
    $tray.ShowBalloonTip(4000)
}

# HttpClient, not Invoke-RestMethod: -Form is PowerShell 7+, and hand-rolling a
# multipart boundary around binary file bytes in 5.1 is how you corrupt PDFs.
function Send-File($path) {
    $client = New-Object Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds(120)   # a 25 MB PDF over Wi-Fi
    $client.DefaultRequestHeaders.Authorization =
        New-Object Net.Http.Headers.AuthenticationHeaderValue('Bearer', $conf.targets[$name].token)
    $form = New-Object Net.Http.MultipartFormDataContent
    $stream = [IO.File]::OpenRead($path)
    try {
        $form.Add((New-Object Net.Http.StreamContent -ArgumentList $stream),
                  'file', [IO.Path]::GetFileName($path))
        if ($script:screen) {
            $form.Add((New-Object Net.Http.StringContent -ArgumentList $script:screen), 'screen')
        }
        $r = $client.PostAsync("$Base/v1/upload", $form).Result
        $body = $r.Content.ReadAsStringAsync().Result
        if (-not $r.IsSuccessStatusCode) {
            $msg = try { (ConvertFrom-Json $body).detail } catch { $body }
            throw "$([int]$r.StatusCode): $msg"
        }
    } finally { $stream.Dispose(); $form.Dispose(); $client.Dispose() }
}

# Files win over text: copying a file in Explorer also puts its path on the
# clipboard as text, and "show me this file" is never "browse to C:\...".
function Send-Clipboard {
    $files = Get-Clipboard -Format FileDropList
    if ($files) { Act 'Upload' { Send-File $files[0].FullName }; return }
    $text = (Get-Clipboard -Format Text) -join "`n"
    $url = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($url -and $url.Trim() -match '^https?://') {
        Act 'Navigate' { Api '/v1/navigate' @{ url = $url.Trim(); screen = $script:screen } }
    } else {
        Notify 'Nothing to send' 'The clipboard holds no link or file.' 'Warning'
    }
}

# --- icons -------------------------------------------------------------------

# Drawn, not shipped: a .ico is a binary blob in the repo for three coloured
# rectangles, and the colour is the whole feature.
function New-TrayIcon($rgb, $filled) {
    $color = [Drawing.Color]::FromArgb($rgb[0], $rgb[1], $rgb[2])
    $bmp = New-Object Drawing.Bitmap 16, 16
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $brush = New-Object Drawing.SolidBrush $color
    if ($filled) { $g.FillRectangle($brush, 1, 2, 14, 10) }
    else { $g.DrawRectangle((New-Object Drawing.Pen $color, 1.6), 1, 2, 13, 9) }
    $g.FillRectangle($brush, 6, 12, 4, 3)          # stand
    $g.FillRectangle($brush, 3, 14, 10, 2)         # foot
    $g.Dispose()
    [Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$IconAwake  = New-TrayIcon @(70, 150, 230) $true
$IconAsleep = New-TrayIcon @(130, 130, 130) $false
$IconDown   = New-TrayIcon @(225, 85, 85) $false

# --- state -------------------------------------------------------------------

$script:screen = if (Test-Path -LiteralPath $StateFile) {
    (Get-Content -LiteralPath $StateFile -Raw).Trim()
} else { '' }
$script:screens = @()
$script:awake = $true
$script:supports = $null      # until Poll asks; $null means "assume everything"

function Save-Screen($s) {
    $script:screen = $s
    New-Item -ItemType Directory -Force -Path (Split-Path $StateFile) | Out-Null
    Set-Content -LiteralPath $StateFile -Value $s -Encoding UTF8
}

function Poll {
    try {
        $s = Invoke-RestMethod -Uri "$Base/v1/status" -Headers $Headers -TimeoutSec 10
        $script:screens = @($s.screens)
        $script:awake = $s.awake -ne $false
        # What the Pi's browser can actually do. Firefox does navigate and
        # nothing else, so a Play/pause item there is a menu entry that 501s.
        # $null = an agent too old to say: assume everything, as we always did.
        $script:supports = if ($s.PSObject.Properties['supports']) { @($s.supports) } else { $null }
        # Pin the selection to a screen that exists: first run, or one renamed
        # out of the Pi's config since we last saved it.
        # A browser that can't address screens gets pinned to the first one too,
        # or a saved 'right' aims every click at a 501.
        $stale = if (Can 'screens') {
            $script:screen -ne 'all' -and
                -not ($script:screens | Where-Object { $_.name -eq $script:screen })
        } else {
            $script:screens -and $script:screen -ne $script:screens[0].name
        }
        if ($stale -and $script:screens) { Save-Screen $script:screens[0].name }
        $me = $script:screens | Where-Object { $_.name -eq $script:screen } | Select-Object -First 1
        $where = if (-not $script:awake) { 'asleep' }
                 elseif ($s.browser -ne 'ok') { 'browser down' }
                 elseif ($script:screen -eq 'all') { 'every screen' }
                 elseif ($me.current_url) { $me.current_url -replace '^https?://', '' }
                 else { 'idle' }
        Set-Tip "$name/$script:screen - $where"
        $tray.Icon = if ($script:awake) { $IconAwake } else { $IconAsleep }
    } catch {
        Set-Tip "$name - unreachable"
        $tray.Icon = $IconDown
    }
}

# NotifyIcon.Text throws past 63 chars, and a long file URL blows straight past it.
function Set-Tip($text) {
    $tray.Text = if ($text.Length -gt 63) { $text.Substring(0, 60) + '...' } else { $text }
}

# --- menu --------------------------------------------------------------------

$tray = New-Object Windows.Forms.NotifyIcon
$tray.Icon = $IconDown
$tray.Visible = $true
$menu = New-Object Windows.Forms.ContextMenuStrip
$tray.ContextMenuStrip = $menu

function Add-Item($menu, $text, $block) {
    $i = $menu.Items.Add($text)
    if ($block) { $i.Add_Click($block) } else { $i.Enabled = $false }
    $i
}

function Can($feature) {
    $null -eq $script:supports -or $script:supports -contains $feature
}

# Rebuilt every time it opens rather than kept in sync: the menu is the only
# thing that reads this state, and it can't be stale if it doesn't outlive the click.
$menu.Add_Opening({
    Poll
    $menu.Items.Clear()
    if ($script:screens.Count -gt 1 -and (Can 'screens')) {
        foreach ($s in @($script:screens) + @([pscustomobject]@{ name = 'all' })) {
            $n = $s.name
            $i = Add-Item $menu $n ([scriptblock]::Create("Save-Screen '$n'")).GetNewClosure()
            $i.Checked = ($n -eq $script:screen)
        }
        $menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator)) | Out-Null
    }
    (Add-Item $menu 'Send clipboard' { Send-Clipboard }).Font =
        New-Object Drawing.Font $menu.Font, ([Drawing.FontStyle]::Bold)
    Add-Item $menu 'Send file...' { Choose-File } | Out-Null
    $menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator)) | Out-Null
    # The one media verb that earns a menu slot; volume and seek are the web UI's
    # job. Toggles server-side, so the tray never has to know what is playing.
    # Absent entirely on a browser that can't do it, rather than present and 501.
    if (Can 'media') {
        Add-Item $menu 'Play/pause' {
            Act 'Media' { Api '/v1/media' @{ screen = $script:screen; action = 'toggle' } }
        } | Out-Null
    }
    Add-Item $menu 'Home' { Act 'Home' { Api '/v1/home' @{ screen = $script:screen } } } | Out-Null
    Add-Item $menu 'Reload' { Act 'Reload' { Api '/v1/reload' @{ screen = $script:screen } } } | Out-Null
    # No screen on /v1/display: X11 powers both monitors together (agent/display.py).
    $label = if ($script:awake) { 'Display off' } else { 'Wake display' }
    Add-Item $menu $label {
        $action = if ($script:awake) { 'off' } else { 'on' }
        Act 'Display' { Api '/v1/display' @{ action = $action } }
    } | Out-Null
    $menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator)) | Out-Null
    Add-Item $menu 'Open controller...' { Start-Process $Base } | Out-Null
    Add-Item $menu 'Quit' { $tray.Visible = $false; $script:ctx.ExitThread() } | Out-Null
})

function Choose-File {
    $d = New-Object Windows.Forms.OpenFileDialog
    $d.Filter = 'Showable files|*.pdf;*.png;*.jpg;*.jpeg;*.gif;*.webp;*.txt;' +
                '*.mp4;*.webm;*.mp3;*.m4a;*.wav|All files|*.*'
    if ($d.ShowDialog() -eq 'OK') { Act 'Upload' { Send-File $d.FileName } }
}

# The whole reason this exists: copy a link, double-click, done. No menu.
$tray.Add_DoubleClick({ Send-Clipboard })

$timer = New-Object Windows.Forms.Timer
$timer.Interval = $PollSeconds * 1000
$timer.Add_Tick({ Poll })
$timer.Start()

Poll
$script:ctx = New-Object Windows.Forms.ApplicationContext
[Windows.Forms.Application]::Run($script:ctx)
$tray.Dispose()
