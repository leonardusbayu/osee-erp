param([switch]$Stop, [switch]$Status)

$ErrorActionPreference = 'Stop'
$oseeRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $oseeRoot
$oseePython = Join-Path $oseeRoot '.venv/Scripts/python.exe'
$oseeManage = Join-Path $oseeRoot 'manage.py'
$oseeLocal = Join-Path $oseeRoot '.local'
$oseeStateFile = Join-Path $oseeLocal 'server-state.json'
$oseeLogDir = Join-Path $oseeLocal 'logs'
New-Item -ItemType Directory -Path $oseeLocal -Force | Out-Null

function Get-OseeListener {
    return Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Get-OseeIdentity([int]$ProcessId) {
    $oseeProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $oseeProcess) { return $null }
    $oseeCim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    $oseeExpected = [regex]::Escape($oseeManage) + '"?\s+runserver\s+127\.0\.0\.1:8000\s+--noreload(?:\s|$)'
    if (-not $oseeCim -or $oseeCim.CommandLine -notmatch $oseeExpected) { return $null }
    return [pscustomobject]@{
        pid = $oseeProcess.Id
        started_at = $oseeProcess.StartTime.ToUniversalTime().ToString('o')
        executable = $oseeProcess.Path
        command = $oseeCim.CommandLine
        parent_pid = $oseeCim.ParentProcessId
    }
}

function Test-OseeHealth {
    try {
        $oseeResponse = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health/' -TimeoutSec 2
        return $oseeResponse.StatusCode -eq 200 -and ($oseeResponse.Content | ConvertFrom-Json).status -eq 'ok'
    } catch { return $false }
}

function Save-OseeIdentity($Identity) {
    $oseeParent = Get-OseeIdentity ([int]$Identity.parent_pid)
    if ($oseeParent -and [DateTime]::Parse($oseeParent.started_at) -gt [DateTime]::Parse($Identity.started_at)) { $oseeParent = $null }
    @{ root = $oseeRoot; port = 8000; server = $Identity; launcher = $oseeParent } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $oseeStateFile -Encoding UTF8
}

function Stop-OseeProcess($Identity) {
    if (-not $Identity) { return }
    $oseeHandle = Get-Process -Id ([int]$Identity.pid) -ErrorAction SilentlyContinue
    if (-not $oseeHandle) { return }
    try {
        # Pin the native handle before checking identity; Kill must not reopen
        # a reused PID. A virtualenv parent can exit while its child is stopped.
        [void]$oseeHandle.Handle
        if ($oseeHandle.HasExited) { return }
        $oseeCurrent = Get-OseeIdentity ([int]$Identity.pid)
        if ($oseeHandle.HasExited) { return }
        if (-not $oseeCurrent -or
            $oseeHandle.StartTime.ToUniversalTime().ToString('o') -ne $Identity.started_at -or
            $oseeCurrent.started_at -ne $Identity.started_at -or
            $oseeCurrent.executable -ne $Identity.executable -or
            $oseeCurrent.command -ne $Identity.command) {
            throw 'Identitas proses berubah. Server tidak dihentikan. Jalankan Start OSEE untuk memeriksa statusnya.'
        }
        $oseeHandle.Kill()
        if (-not $oseeHandle.WaitForExit(5000)) { throw 'Server belum berhenti; coba kembali beberapa saat lagi.' }
    } catch {
        if ($oseeHandle.HasExited) { return }
        throw
    } finally {
        $oseeHandle.Dispose()
    }
}

# Serialize setup, start, and stop across repeated double-clicks.
$oseeLock = $null
$oseeLockDeadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    try { $oseeLock = [System.IO.File]::Open((Join-Path $oseeLocal 'server.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch [System.IO.IOException] {
        if ([DateTime]::UtcNow -ge $oseeLockDeadline) { throw 'Penyiapan OSEE lain masih berjalan. Tunggu sampai selesai lalu coba kembali.' }
        Start-Sleep -Milliseconds 200
    }
} until ($oseeLock)

try {
    $oseeListener = Get-OseeListener
    if ($Stop) {
        if (-not (Test-Path -LiteralPath $oseeStateFile)) {
            if ($oseeListener) { throw 'Server belum terdaftar pada peluncur ini. Jalankan Start OSEE untuk memeriksanya dahulu.' }
            Write-Host 'OSEE sudah berhenti.'
            return
        }
        $oseeState = Get-Content -LiteralPath $oseeStateFile -Raw | ConvertFrom-Json
        if ($oseeState.root -ne $oseeRoot -or $oseeState.port -ne 8000) { throw 'Catatan proses tidak sesuai dengan workspace ini.' }
        if ($oseeListener -and $oseeListener.OwningProcess -ne $oseeState.server.pid) { throw 'Port 8000 sedang digunakan proses lain. Proses tersebut tidak dihentikan.' }
        Stop-OseeProcess $oseeState.server
        Stop-OseeProcess $oseeState.launcher
        Remove-Item -LiteralPath $oseeStateFile
        Write-Host 'Server OSEE sudah dihentikan. Data perusahaan tetap tersimpan.' -ForegroundColor Green
        return
    }
    if ($oseeListener) {
        $oseeIdentity = Get-OseeIdentity ([int]$oseeListener.OwningProcess)
        if (-not $oseeIdentity) { throw 'Port 8000 digunakan proses lain. Proses tersebut tidak diubah atau dihentikan.' }
        if (-not $Status) { Save-OseeIdentity $oseeIdentity }
        if (-not (Test-OseeHealth)) { throw "Proses OSEE ada tetapi belum merespons. Gunakan Stop OSEE sebelum mencoba lagi; log di $oseeLogDir." }
        Write-Host 'OSEE sudah berjalan di http://127.0.0.1:8000' -ForegroundColor Green
        return
    }
    if ($Status) { Write-Host 'Server OSEE belum berjalan.'; exit 1 }
    if (Test-Path -LiteralPath $oseeStateFile) {
        $oseePendingState = Get-Content -LiteralPath $oseeStateFile -Raw | ConvertFrom-Json
        foreach ($oseeSaved in @($oseePendingState.server, $oseePendingState.launcher)) {
            if (-not $oseeSaved) { continue }
            $oseePending = Get-OseeIdentity ([int]$oseeSaved.pid)
            if ($oseePending -and $oseePending.started_at -eq $oseeSaved.started_at -and $oseePending.command -eq $oseeSaved.command) {
                throw "Proses OSEE masih berjalan tetapi belum menerima koneksi. Gunakan Stop OSEE sebelum mencoba lagi; log di $oseeLogDir."
            }
        }
    }

    if (-not (Test-Path -LiteralPath $oseePython)) {
        $oseeBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
        if (Test-Path -LiteralPath $oseeBundled) { $oseeBasePython = $oseeBundled }
        else { $oseeBasePython = (Get-Command python -ErrorAction Stop).Source }
        & $oseeBasePython -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Tidak dapat menyiapkan Python. Gunakan Python 3.12 atau lebih baru.' }
    }
    $oseeFingerprint = (Get-FileHash -LiteralPath (Join-Path $oseeRoot 'requirements.txt') -Algorithm SHA256).Hash
    $oseeMarker = Join-Path $oseeLocal 'requirements.sha256'
    if (-not (Test-Path -LiteralPath $oseeMarker) -or (Get-Content -LiteralPath $oseeMarker -Raw).Trim() -ne $oseeFingerprint) {
        Write-Host 'Menyiapkan dependensi OSEE...'
        & $oseePython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Instalasi dependensi gagal. Periksa koneksi internet lalu jalankan kembali.' }
        Set-Content -LiteralPath $oseeMarker -Value $oseeFingerprint
    }
    & $oseePython $oseeManage migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw 'Penyiapan database gagal.' }
    & $oseePython $oseeManage seed_demo
    if ($LASTEXITCODE -ne 0) { throw 'Penyiapan demo gagal. Launcher ini hanya untuk mode lokal.' }

    New-Item -ItemType Directory -Path $oseeLogDir -Force | Out-Null
    $oseeStartOptions = @{
        FilePath = $oseePython
        ArgumentList = @('-u', ('"' + $oseeManage + '"'), 'runserver', '127.0.0.1:8000', '--noreload')
        WorkingDirectory = $oseeRoot
        WindowStyle = 'Hidden'
        PassThru = $true
        RedirectStandardOutput = Join-Path $oseeLogDir 'server-out.log'
        RedirectStandardError = Join-Path $oseeLogDir 'server-error.log'
    }
    $oseeLaunch = Start-Process @oseeStartOptions
    [void]$oseeLaunch.Handle
    $oseeLaunchIdentity = Get-OseeIdentity $oseeLaunch.Id
    if ($oseeLaunchIdentity) { Save-OseeIdentity $oseeLaunchIdentity }
    $oseeDeadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        Start-Sleep -Milliseconds 400
        $oseeLaunch.Refresh()
        if ($oseeLaunch.HasExited) { break }
        # Track the Windows virtualenv child even if startup never binds a port.
        if ($oseeLaunchIdentity) {
            $oseeChildren = Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $oseeLaunch.Id)
            foreach ($oseeChild in $oseeChildren) {
                $oseeChildIdentity = Get-OseeIdentity ([int]$oseeChild.ProcessId)
                if ($oseeChildIdentity -and [DateTime]::Parse($oseeChildIdentity.started_at) -ge [DateTime]::Parse($oseeLaunchIdentity.started_at)) {
                    Save-OseeIdentity $oseeChildIdentity
                }
            }
        }
        if (Test-OseeHealth) {
            $oseeListener = Get-OseeListener
            $oseeIdentity = Get-OseeIdentity ([int]$oseeListener.OwningProcess)
            if ($oseeIdentity -and ($oseeIdentity.pid -eq $oseeLaunch.Id -or $oseeIdentity.parent_pid -eq $oseeLaunch.Id)) {
                Save-OseeIdentity $oseeIdentity
                Write-Host 'OSEE siap dibuka di http://127.0.0.1:8000' -ForegroundColor Green
                Write-Host 'Server berjalan di latar belakang. Jendela ini boleh ditutup.'
                Write-Host 'Gunakan Stop OSEE.cmd untuk menghentikan server.'
                return
            }
        }
        $oseeLaunch.Refresh()
        if ($oseeLaunch.HasExited) { break }
    } while ([DateTime]::UtcNow -lt $oseeDeadline)
    throw "OSEE belum siap menerima koneksi. Gunakan Stop OSEE sebelum mencoba lagi. Log: $oseeLogDir\server-error.log."
} finally {
    $oseeLock.Dispose()
}
