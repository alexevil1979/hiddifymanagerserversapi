param(
    [Parameter(Mandatory = $false)]
    [string]$Message,
    [Parameter(Mandatory = $false)]
    [string]$MessageFile
)

$ErrorActionPreference = "Stop"
$cfgPath = Join-Path $PSScriptRoot "telegram.local.json"
if (-not (Test-Path $cfgPath)) {
    throw "Missing telegram.local.json next to this script."
}

$cfg = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
if ($MessageFile) {
    $text = [System.IO.File]::ReadAllText($MessageFile, [System.Text.UTF8Encoding]::new($false))
} elseif ($Message) {
    $text = $Message
} else {
    throw "Pass -Message or -MessageFile."
}

$text = $text.Trim()
if ($text.Length -gt 4000) {
    $text = $text.Substring(0, 4000)
}

$payload = @{
    chat_id = [string]$cfg.chat_id
    text    = $text
} | ConvertTo-Json -Compress
$utf8 = New-Object System.Text.UTF8Encoding $false
$b64 = [Convert]::ToBase64String($utf8.GetBytes($payload))
$url = "https://api.telegram.org/bot$($cfg.token)/sendMessage"
$ports = $cfg.ssh_socks_ports
if (-not $ports) { $ports = "1080" }

$remotePath = Join-Path $env:TEMP ("tg-remote-" + [guid]::NewGuid().ToString("N") + ".sh")
$remote = @"
f=`$(mktemp)
base64 -d > "`$f" <<'B64'
$b64
B64
url='$url'
ok=1
for p in $ports; do
  out=`$(curl -sS --max-time 20 --socks5-hostname 127.0.0.1:`$p -X POST -H 'Content-Type: application/json' --data-binary @"`$f" "`$url") || continue
  if printf '%s' "`$out" | grep -q '"ok":true'; then
    printf '%s' "`$out"
    ok=0
    break
  fi
done
rm -f "`$f"
exit `$ok
"@
[System.IO.File]::WriteAllText($remotePath, $remote.Replace("`r`n", "`n"), $utf8)

$plink = "C:\Program Files\PuTTY\plink.exe"
$ErrorActionPreference = "Continue"
$out = & $plink -ssh -batch -pw $cfg.ssh_password -m $remotePath "$($cfg.ssh_user)@$($cfg.ssh_host)" 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Remove-Item -Force $remotePath -ErrorAction SilentlyContinue
$outText = ($out | Out-String)
if ($code -ne 0 -or $outText -notmatch '"ok":true') {
    throw "Telegram API error (exit $code): $outText"
}
Write-Output "telegram: sent"
