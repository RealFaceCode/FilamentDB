param(
    [ValidateSet('lan', 'local', 'public', 'none')]
    [string]$Proxy = 'lan',

    [switch]$IncludeSlotPoller,

    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'

# Default to full-stack startup when the caller does not specify a slot-poller preference.
if (-not $PSBoundParameters.ContainsKey('IncludeSlotPoller')) {
    $IncludeSlotPoller = $true
}

function Get-PrimaryLocalIPv4 {
    $excludeAliasPattern = '^(vEthernet|Loopback|isatap|Teredo|Bluetooth|VMware|VirtualBox|Docker)'

    try {
        $defaultRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
            Where-Object { $_.InterfaceAlias -and $_.InterfaceAlias -notmatch $excludeAliasPattern } |
            Sort-Object -Property RouteMetric, ifMetric |
            Select-Object -First 1
        if ($null -ne $defaultRoute) {
            $address = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $defaultRoute.InterfaceIndex |
                Where-Object {
                    $_.IPAddress -and
                    $_.IPAddress -notlike '169.254.*' -and
                    $_.IPAddress -ne '127.0.0.1' -and
                    $_.AddressState -eq 'Preferred' -and
                    (-not $_.SkipAsSource)
                } |
                Select-Object -First 1
            if ($null -ne $address -and $address.IPAddress) {
                return [string]$address.IPAddress
            }
        }
    } catch {
    }

    try {
        $preferred = Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object {
                $_.InterfaceAlias -and
                $_.InterfaceAlias -notmatch $excludeAliasPattern -and
                $_.IPAddress -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.IPAddress -ne '127.0.0.1' -and
                $_.AddressState -eq 'Preferred' -and
                (-not $_.SkipAsSource)
            } |
            Select-Object -First 1
        if ($null -ne $preferred -and $preferred.IPAddress) {
            return [string]$preferred.IPAddress
        }
    } catch {
    }

    try {
        $fallback = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object {
                $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
                $_.IPAddressToString -notlike '169.254.*' -and
                $_.IPAddressToString -ne '127.0.0.1'
            } |
            Select-Object -First 1
        if ($null -ne $fallback) {
            return [string]$fallback.IPAddressToString
        }
    } catch {
    }

    return $null
}

function Update-DotEnvValues {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $existingLines = @()
    if (Test-Path $FilePath) {
        $existingLines = Get-Content -LiteralPath $FilePath
    }

    $writtenKeys = @{}
    $outputLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $existingLines) {
        $matched = $false
        foreach ($key in $Values.Keys) {
            if ($line -match "^$([Regex]::Escape($key))=") {
                $outputLines.Add("$key=$($Values[$key])")
                $writtenKeys[$key] = $true
                $matched = $true
                break
            }
        }
        if (-not $matched) {
            $outputLines.Add($line)
        }
    }

    foreach ($key in $Values.Keys) {
        if (-not $writtenKeys.ContainsKey($key)) {
            $outputLines.Add("$key=$($Values[$key])")
        }
    }

    Set-Content -LiteralPath $FilePath -Value $outputLines -Encoding UTF8
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string[]]$Args
    )

    Write-Host "> docker compose $($Args -join ' ')" -ForegroundColor Cyan
    & docker compose @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose command failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Starting FilamentDB services..." -ForegroundColor Green
Write-Host "Proxy profile: $Proxy" -ForegroundColor DarkGray
Write-Host "Include slot-poller: $($IncludeSlotPoller.IsPresent)" -ForegroundColor DarkGray
Write-Host "Build enabled: $(-not $NoBuild.IsPresent)" -ForegroundColor DarkGray

if ($Proxy -eq 'lan') {
    $detectedLanIp = Get-PrimaryLocalIPv4
    if ($detectedLanIp) {
        Write-Host "Detected LAN IP: $detectedLanIp" -ForegroundColor DarkGray
        $envPath = Join-Path (Get-Location) '.env'
        if (Test-Path $envPath) {
            Update-DotEnvValues -FilePath $envPath -Values @{
                'LAN_HOST' = $detectedLanIp
                'TRUSTED_ORIGINS' = "https://${detectedLanIp}:8443"
                'ALLOWED_HOSTS' = "localhost,127.0.0.1,testserver,$detectedLanIp"
            }
            Write-Host "Updated .env LAN_HOST/TRUSTED_ORIGINS/ALLOWED_HOSTS" -ForegroundColor DarkGray
        } else {
            Write-Host "Warning: .env not found, skipped LAN_HOST update" -ForegroundColor Yellow
        }
    } else {
        Write-Host "Warning: Could not detect LAN IPv4 automatically" -ForegroundColor Yellow
    }
}

# Avoid proxy port conflicts when switching profiles
Invoke-Compose -Args @('--profile', 'https-local', 'rm', '-f', 'https-proxy-local')
Invoke-Compose -Args @('--profile', 'https-lan', 'rm', '-f', 'https-proxy-lan')
Invoke-Compose -Args @('--profile', 'https', 'rm', '-f', 'https-proxy')

$baseArgs = @('up', '-d')
if (-not $NoBuild.IsPresent) {
    $baseArgs += '--build'
}

switch ($Proxy) {
    'lan' {
        Invoke-Compose -Args (@('--profile', 'https-lan') + $baseArgs + @('db', 'web', 'https-proxy-lan'))
    }
    'local' {
        Invoke-Compose -Args (@('--profile', 'https-local') + $baseArgs + @('db', 'web', 'https-proxy-local'))
    }
    'public' {
        Invoke-Compose -Args (@('--profile', 'https') + $baseArgs + @('db', 'web', 'https-proxy'))
    }
    'none' {
        Invoke-Compose -Args ($baseArgs + @('db', 'web'))
    }
}

if ($IncludeSlotPoller.IsPresent) {
    $pollerArgs = @('--profile', 'slot-poller', 'up', '-d')
    if (-not $NoBuild.IsPresent) {
        $pollerArgs += '--build'
    }
    $pollerArgs += 'slot-poller'
    Invoke-Compose -Args $pollerArgs
}

Write-Host ''
Write-Host 'Current service status:' -ForegroundColor Green
Invoke-Compose -Args @('ps')

Write-Host ''
switch ($Proxy) {
    'lan' {
        Write-Host 'LAN HTTPS: https://<SERVER-IP>:8443' -ForegroundColor Yellow
    }
    'local' {
        Write-Host 'Local HTTPS: https://localhost:8443' -ForegroundColor Yellow
    }
    'public' {
        Write-Host 'Public HTTPS: https://<your-domain>' -ForegroundColor Yellow
    }
    'none' {
        Write-Host 'HTTP only (local bind): http://127.0.0.1:8000' -ForegroundColor Yellow
    }
}

if ($IncludeSlotPoller.IsPresent) {
    Write-Host 'Slot-poller: enabled' -ForegroundColor Yellow
}
