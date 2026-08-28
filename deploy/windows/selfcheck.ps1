# Offline checks for roomtray.ps1. No agent, no tray, no window.
#
#   powershell -ExecutionPolicy Bypass -File selfcheck.ps1
#
# Loads the real script's function definitions out of its AST rather than
# dot-sourcing it, because dot-sourcing would start the tray and block forever.
# Send-File is not covered here — it needs a live agent; it was verified against
# one by uploading 256 raw bytes and diffing the SHA256 on the far side.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$src = Get-Content -Raw (Join-Path $PSScriptRoot 'roomtray.ps1')
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$null)
(($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
 ) | ForEach-Object { $_.Extent.Text }) -join "`n" | Invoke-Expression

$fails = 0
function Check($what, $got, $want) {
    if ("$got" -ne "$want") { $script:fails++; Write-Host "FAIL $what`n  got:  $got`n  want: $want" }
    else { Write-Host "ok   $what" }
}

# targets.toml: sections, the default key, and a trailing `# comment` after a value
$tmp = Join-Path $env:TEMP "roomtray-check-$PID.toml"
Set-Content -LiteralPath $tmp -Encoding UTF8 -Value @'
default = "study"   # used when -Target is omitted

[study]
url = "http://100.73.78.36:8080"   # the Pi's tailnet address
token = "abc123"

[spare]
url = "http://100.1.2.3:8080"
token = "def456"
'@
$c = Read-Targets $tmp
Remove-Item -LiteralPath $tmp
Check 'default'          $c.default                    'study'
Check 'sections'         (($c.targets.Keys | Sort-Object) -join ',') 'spare,study'
Check 'url past comment' $c.targets['study'].url       'http://100.73.78.36:8080'
Check 'token'            $c.targets['spare'].token     'def456'

# NotifyIcon.Text throws past 63 chars, and file URLs are long.
$tray = [pscustomobject]@{ Text = '' }
Set-Tip ('x' * 200);        Check 'long tip clamped' $tray.Text.Length 63
Set-Tip 'study/acer - ok';  Check 'short tip kept'   $tray.Text        'study/acer - ok'

Check 'icon size' (New-TrayIcon @(70, 150, 230) $true).Width 16

# The agent puts the useful part in {"detail": ...}; a thrown string has no body.
try { throw 'plain failure' } catch { Check 'detail: no body' (Detail $_) 'plain failure' }
$err = $null
try { Invoke-RestMethod -Uri 'http://127.0.0.1:1/nope' -TimeoutSec 2 } catch { $err = $_ }
Check 'detail: unreachable' ((Detail $err).Length -gt 0) 'True'

if ($fails) { Write-Host "`n$fails failed"; exit 1 }
Write-Host "`nall passed"
