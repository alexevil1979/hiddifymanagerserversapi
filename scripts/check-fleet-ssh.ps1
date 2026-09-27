# Check SSH for all inventory servers; write docs/web/data/ssh-status.json (+ refresh fleet.json).
# No secrets in output files.
param(
  [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"
$envFile = Join-Path $Root ".env"
Get-Content $envFile | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $k, $v = $_.Split('=', 2)
  Set-Item -Path "Env:$k" -Value $v
}

$plink = "C:\Program Files\PuTTY\plink.exe"
$sshExe = "C:\Windows\System32\OpenSSH\ssh.exe"
$cfgPath = Join-Path $Root "telegram.local.json"
$cfg = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json

function Short([string]$s, [int]$n = 100) {
  if ([string]::IsNullOrEmpty($s)) { return "" }
  $t = ($s -replace '\s+', ' ').Trim()
  if ($t.Length -le $n) { return $t }
  return $t.Substring(0, $n)
}

function Test-Tcp([string]$HostName, [int]$Port, [int]$Ms = 3500) {
  try {
    $tcp = New-Object Net.Sockets.TcpClient
    $ok = $tcp.BeginConnect($HostName, $Port, $null, $null).AsyncWaitHandle.WaitOne($Ms, $false)
    $r = $ok -and $tcp.Connected
    $tcp.Close()
    return $r
  } catch { return $false }
}

function Invoke-PlinkPassword([string]$User, [string]$HostName, [int]$Port, [string]$Password, [string]$RemoteCmd) {
  $psi = New-Object Diagnostics.ProcessStartInfo
  $psi.FileName = $plink
  $psi.Arguments = "-ssh $User@$HostName -P $Port -pw $Password `"$RemoteCmd`""
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false
  $p = [Diagnostics.Process]::Start($psi)
  Start-Sleep -Milliseconds 400
  try { $p.StandardInput.WriteLine("y") } catch {}
  $p.StandardInput.Close()
  if (-not $p.WaitForExit(25000)) { try { $p.Kill() } catch {}; return "TIMEOUT" }
  return ($p.StandardOutput.ReadToEnd() + "`n" + $p.StandardError.ReadToEnd())
}

# rebuild fleet snapshot
python -c @"
import re, json
from pathlib import Path
from datetime import datetime
root=Path(r'$Root')
t=(root/'inventory.yml').read_text(encoding='utf-8')
parts=re.split(r'(?m)^  - id: ', t)
cards=[]
for p in parts[1:]:
    lines=p.splitlines(); sid=lines[0].strip(); body='\n'.join(lines[1:])
    def g(k,d=None):
        m=re.search(r'(?m)^    %s:\s*(.*)$'%re.escape(k), body)
        if not m: return d
        v=m.group(1).strip()
        if v in ('null','~',''): return None
        return v.strip('\"')
    def ga(k,d=None):
        m=re.search(r'(?m)^      %s:\s*(.*)$'%re.escape(k), body)
        return m.group(1).strip().strip('\"') if m else d
    modes=re.search(r'(?m)^      domain_modes:\s*(.*)$', body)
    dm=[]; in_d=False
    for line in body.splitlines():
        if re.match(r'^    domains:\s*$', line): in_d=True; continue
        if in_d:
            m=re.match(r'^      - (\S+)$', line)
            if m: dm.append(m.group(1)); continue
            if line.startswith('    ') and not line.startswith('      '): in_d=False
    cards.append({
      'id': sid, 'host': g('host'), 'location': g('location'), 'group': g('group'),
      'state': g('state'), 'ssh_port': int(g('ssh_port') or 22),
      'ssh_user': g('ssh_user') or 'root', 'auth_method': ga('method'),
      'password_env': ga('password_env'), 'key_path_env': ga('key_path_env'),
      'subdomain': g('subdomain'), 'domains': dm,
      'domain_modes': modes.group(1).strip() if modes else None,
      'panel_domain': g('panel_domain'), 'status_note': g('status_note'),
      'status_updated_at': g('status_updated_at'),
    })
out=root/'docs'/'web'/'data'; out.mkdir(parents=True, exist_ok=True)
(out/'fleet.json').write_text(json.dumps({'updated': datetime.now().isoformat(timespec='minutes'), 'servers': cards}, ensure_ascii=False, indent=2), encoding='utf-8')
print('fleet', len(cards))
"@

$cards = (Get-Content -Raw -Encoding UTF8 (Join-Path $Root "docs\web\data\fleet.json") | ConvertFrom-Json).servers
$results = @()

foreach ($c in $cards) {
  $id = $c.id; $h = $c.host; $port = [int]$c.ssh_port; $user = $c.ssh_user; $auth = $c.auth_method
  $tcp = Test-Tcp $h $port
  $sshSt = "fail"; $detail = ""

  if ($id -eq "vpn-vp2" -and -not $tcp) {
    $rp = Join-Path $env:TEMP "fleet-ssh-vp2.sh"
    $body = @'
set +e
test -x /tmp/vp2-ssh.sh || exit 2
/tmp/vp2-ssh.sh 1084 'echo OK; hostname' || /tmp/vp2-ssh.sh 1085 'echo OK; hostname'
'@
    [IO.File]::WriteAllText($rp, $body.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
    $out = & $plink -ssh -batch -pw $cfg.ssh_password -m $rp "$($cfg.ssh_user)@$($cfg.ssh_host)" 2>&1 | Out-String
    Remove-Item $rp -Force -EA SilentlyContinue
    if ($out -match '(?m)^OK\b') { $sshSt = "ok_socks"; $detail = "via 192.168.1.152 socks1084/1085" }
    else { $sshSt = "fail"; $detail = Short $out }
  }
  elseif (-not $tcp) {
    $sshSt = "tcp_closed"; $detail = ":$port closed"
  }
  elseif ($auth -eq "password") {
    $pw = [Environment]::GetEnvironmentVariable($c.password_env)
    if (-not $pw) { $sshSt = "no_secret"; $detail = $c.password_env }
    else {
      $out = Invoke-PlinkPassword $user $h $port $pw "echo OK; hostname"
      if ($out -match '(?m)^OK\b') { $sshSt = "ok"; $detail = Short (($out -split "`n" | Where-Object { $_ -match 'OK|^\S' } | Select-Object -First 2) -join " | ") }
      elseif ($out -match 'Access denied|Wrong password') { $sshSt = "auth_fail"; $detail = "denied" }
      else { $sshSt = "fail"; $detail = Short $out }
    }
  }
  elseif ($auth -eq "key") {
    $key = [Environment]::GetEnvironmentVariable($c.key_path_env)
    if (-not $key -or -not (Test-Path -LiteralPath $key)) { $sshSt = "no_secret"; $detail = $c.key_path_env }
    else {
      $out = & $sshExe -i $key -p $port -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12 "$user@$h" "echo OK; hostname" 2>&1 | Out-String
      if ($out -match '(?m)^OK\b') { $sshSt = "ok"; $detail = Short (($out -split "`n" | Where-Object { $_ -match 'OK|^\S' } | Select-Object -First 2) -join " | ") }
      elseif ($out -match 'Permission denied') { $sshSt = "auth_fail"; $detail = "denied" }
      else { $sshSt = "fail"; $detail = Short $out }
    }
  }
  else { $sshSt = "fail"; $detail = "unknown auth $auth" }

  $results += [pscustomobject]@{
    id = $id; host = $h; port = $port; auth = $auth; tcp = [bool]$tcp; ssh = $sshSt; detail = $detail
  }
  Write-Output ("{0,-12} tcp={1,-5} ssh={2,-10} {3}" -f $id, $tcp, $sshSt, $detail)
}

$payload = [ordered]@{
  checked_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm")
  servers    = $results
}
$outPath = Join-Path $Root "docs\web\data\ssh-status.json"
($payload | ConvertTo-Json -Depth 5) | Set-Content -Encoding utf8 $outPath
$ok = @($results | Where-Object { $_.ssh -match '^ok' }).Count
Write-Output "DONE ok=$ok total=$($results.Count) -> $outPath"
