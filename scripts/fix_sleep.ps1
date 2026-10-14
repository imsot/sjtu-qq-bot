# Keep the QQ bot online: stop this laptop from sleeping on AC power.
#
# Root cause (2026-09-28 diagnosis): this machine uses Modern Standby (S0) whose
# "network disconnected" variant drops WiFi on sleep. NapCat's QQ long-connection
# dies within ~4s of entering standby, producing "account status -> offline" plus
# sendMsg timeouts. See napcat_out.log / System event log Id 506.
#
# Scope: AC (plugged in) only. Battery behaviour is left untouched, so unplugging
# and carrying the laptop away still sleeps normally.
#
# ASCII only: this file is read by PowerShell 5.1, which decodes a BOM-less file
# as the system ANSI codepage (GBK here), so non-ASCII literals would be mangled.
# powercfg output is localised, so values are read positionally (AC then DC)
# instead of by matching label text.
#
# Requires: Administrator. Run fix_sleep_admin.bat instead of this file directly.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "ERROR: needs Administrator. Run fix_sleep_admin.bat instead." -ForegroundColor Red
    exit 1
}

$SUB_BUTTONS = '4f971e89-eebd-4455-a8de-9e59040e7347'
$LIDACTION   = '5ca83367-6e45-459f-a27b-476b1d01c936'

# Returns @(AC, DC) for a power setting, or $null if the setting is unreadable.
# Relies on powercfg printing the AC value before the DC value.
function Get-PowerSetting([string]$Sub, [string]$Setting) {
    $vals = @()
    foreach ($line in (powercfg /q SCHEME_CURRENT $Sub $Setting)) {
        if ($line -match '0x[0-9a-fA-F]{8}') { $vals += $matches[0] }
    }
    if ($vals.Count -ge 2) { return $vals[0..1] }
    return $null
}

Write-Host "=== QQ bot: keep awake while plugged in ===" -ForegroundColor Cyan
Write-Host ""

# ---- 1. Never idle-sleep on AC (the main fix) -------------------------------
Write-Host "[1/4] Idle timeout -> Never (AC)"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

# ---- 2. Lid close -> Do nothing on AC --------------------------------------
# LIDACTION ships hidden (Attributes=1), which is why powercfg silently no-ops
# on it. Unlock visibility first, then set the value.
# LIDACTION values: 0=Do nothing, 1=Sleep, 2=Hibernate, 3=Shut down
Write-Host "[2/4] Lid close -> Do nothing (AC)"
$lidKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\$SUB_BUTTONS\$LIDACTION"
if (Test-Path $lidKey) {
    Set-ItemProperty -Path $lidKey -Name Attributes -Value 2 -Type DWord
} else {
    Write-Host "      note: LIDACTION key absent, skipped" -ForegroundColor Yellow
}
powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 0
powercfg /setactive SCHEME_CURRENT

# ---- 3. WiFi adapter: allow Windows to power it down -> off ----------------
# PnPCapabilities = 24 (0x18) disables device power management. Takes effect
# after the adapter restarts (reboot), NOT applied here so the bot's current
# connection is not interrupted.
Write-Host "[3/4] WiFi power saving -> Disabled"
$adapter = Get-NetAdapter -Name WLAN -ErrorAction SilentlyContinue
if ($adapter) {
    $base = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}'
    $patched = $false
    Get-ChildItem $base -ErrorAction SilentlyContinue |
        Where-Object { $_.PSChildName -match '^\d{4}$' } |
        ForEach-Object {
            $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($props.NetCfgInstanceId -eq $adapter.InterfaceGuid) {
                Set-ItemProperty -Path $_.PSPath -Name PnPCapabilities -Value 24 -Type DWord
                $patched = $true
            }
        }
    if ($patched) {
        Write-Host "      set PnPCapabilities=24; applies after next reboot" -ForegroundColor Yellow
    } else {
        Write-Host "      WARNING: could not locate adapter registry key" -ForegroundColor Yellow
    }
} else {
    Write-Host "      WARNING: adapter 'WLAN' not found" -ForegroundColor Yellow
}

# ---- 4. Verify -------------------------------------------------------------
Write-Host "[4/4] Verify"
Write-Host ""

$ok = $true

$standby = Get-PowerSetting 'SUB_SLEEP' 'STANDBYIDLE'
if ($standby) {
    Write-Host ("  standby-timeout AC : " + $standby[0])
    if ($standby[0] -ne '0x00000000') { $ok = $false }
} else {
    Write-Host "  standby-timeout AC : <could not read back>" -ForegroundColor Yellow
    $ok = $false
}

$lid = Get-PowerSetting $SUB_BUTTONS $LIDACTION
if ($lid) {
    Write-Host ("  lid-close AC       : " + $lid[0] + "  (0x00000000 = do nothing)")
    if ($lid[0] -ne '0x00000000') { $ok = $false }
} else {
    Write-Host "  lid-close AC       : <could not read back>" -ForegroundColor Yellow
    $ok = $false
}

Write-Host ""
if ($ok) {
    Write-Host "OK: machine will no longer sleep on AC power." -ForegroundColor Green
} else {
    Write-Host "INCOMPLETE: re-check the values above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "--- Rollback (restore defaults) ---" -ForegroundColor DarkGray
Write-Host "  powercfg /change standby-timeout-ac 15"
Write-Host "  powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 1"
Write-Host "  powercfg /setactive SCHEME_CURRENT"
Write-Host "  (WiFi: delete PnPCapabilities or set it to 0, then reboot)"
