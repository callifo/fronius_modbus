<#
.SYNOPSIS
Saves everything a Fronius inverter reads out into one JSON file.

.DESCRIPTION
Reads the status, component and Solar API endpoints and the web interface's
channel names and event texts, which answer without a login, and on request
the configuration endpoints of the customer or technician login and the raw
SunSpec registers over Modbus TCP. Only reads: no command endpoint is called
except the login, and Modbus is only asked to read holding registers. A device
without the GEN24 web interface, such as a Datamanager, still gets its Solar
API and Modbus read.

The file is meant for debugging and for adding support for a new device; by
default serial numbers, addresses and names are replaced with <redacted>.

Runs on the PowerShell that comes with Windows (5.1) and on PowerShell 7.
inverter_dump.tests.ps1 checks it offline.
#>
param(
    [string]$InverterHost,
    [int]$ModbusPort = 502,
    # Fronius numbers the meters from a configurable offset, 200 by default.
    [int]$FirstMeterUnit = 200
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

# Taken from the web interface's own code (firmware 1.41); the commands are
# left out, as a GET on some of them acts.
$PublicPaths = @(
    '/api/status/common', '/api/status/version', '/api/status/devices',
    '/api/status/capabilities', '/api/status/powerflow', '/api/status/activeEvents',
    '/api/status/events', '/api/status/ioServices', '/api/status/emrs',
    '/api/status/license', '/api/status/license/features', '/api/status/network',
    '/api/status/registrationData',
    '/api/components/inverter/readable', '/api/components/PowerMeter/readable',
    '/api/components/BatteryManagementSystem/readable', '/api/components/Ohmpilot/readable',
    '/api/components/OhmpilotEco/readable', '/api/components/HeatPump/readable',
    '/solar_api/GetAPIVersion.cgi', '/solar_api/v1/GetInverterInfo.cgi',
    '/solar_api/v1/GetActiveDeviceInfo.cgi?DeviceClass=System',
    '/solar_api/v1/GetInverterRealtimeData.cgi?Scope=System',
    '/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System',
    '/solar_api/v1/GetStorageRealtimeData.cgi?Scope=System',
    '/solar_api/v1/GetOhmPilotRealtimeData.cgi?Scope=System',
    '/solar_api/v1/GetPowerFlowRealtimeData.fcgi', '/solar_api/v1/GetLoggerInfo.cgi',
    # The web interface's names for the component channels and its event
    # texts: a new device's channels and codes show up there first.
    '/app/assets/i18n/WeblateTranslations/channels/en.json',
    '/app/assets/i18n/WeblateTranslations/channels/de.json',
    '/app/assets/i18n/StateCodeTranslations/en.json',
    '/app/assets/i18n/StateCodeTranslations/de.json'
)
$LoginPaths = @(
    '/api/config/common', '/api/config/batteries', '/api/config/meter', '/api/config/modbus',
    '/api/config/solar_api', '/api/config/solarweb', '/api/config/timeofuse',
    '/api/config/limit_settings/powerLimits', '/api/config/import_limit',
    '/api/config/iomapping', '/api/config/ohmpilot', '/api/config/pcc',
    '/api/config/powerunit', '/api/config/emrs', '/api/config/ics',
    '/api/config/multiinverter', '/api/config/multinetwork', '/api/config/pushservice',
    '/api/config/device-coyote', '/api/config/user-vpn', '/api/config/wizard',
    '/api/config/region-gridcode/SetupMeta', '/api/config/region-gridcode/gridcode',
    '/api/config/region-gridcode/gridcode/general',
    '/api/config/region-gridcode/gridcode/gridfeatures',
    '/api/config/region-gridcode/gridcode/interfaceprotection',
    '/api/config/region-gridcode/gridcode/safety',
    '/api/config/region-gridcode/gridcode/setupinfo'
)

$Redacted = '<redacted>'
# Everything beneath these keys is replaced, and each value found there again
# wherever else it appears.
$SensitiveKey = '^(serial|serialNumber|UniqueID|systemName|CustomName|hostname|hw_address|mac|ssid|ip|ipAddress|dns|nameservers|gateway|e-?mail|latitude|longitude|street|city|zip|postalCode|\w*(passw|secret|apikey|privatekey|psk)\w*|\w*token)$'
$SensitivePattern = @(
    '\b(?:(?:10|127)(?:\.\d{1,3}){3}|(?:192\.168|169\.254|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2})\b',
    '\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b',
    # IPv6, full or with "::"; a time of day has two colons only.
    '(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])',
    '(?i)(?<![\w:])[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?![\w:])',
    '[\w.+-]+@[\w-]+\.[\w.-]+',
    # Board and hardware ids; a digit run after a point is a measured value.
    '(?<![\d.])\d{12,}(?!\d)'
)
$LongNumber = '^-?\d{12,}$'
# A shorter value found under a sensitive key would take every "1" with it.
$MinKnownLength = 4
$IPv4 = '^\d{1,3}(\.\d{1,3}){3}$'
# One JSON token each; the last alternative catches what JSON does not allow.
# [0-9], not \d: .NET's \d takes the digits of every script, JSON only ASCII.
$JsonToken = '"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|[{}\[\]:,]|[ \t\r\n]+|.'
$JsonEscape = @{ '"' = '"'; '\' = '\'; '/' = '/'; 'b' = [string][char]8; 'f' = [string][char]12; 'n' = "`n"; 'r' = "`r"; 't' = "`t" }

# SunSpec starts at 40000 with "SunS"; the inverter answers as unit 1. A read
# returns at most 125 registers.
$SunSpecBase = 40000
$SunSpecMarker = @(0x5375, 0x6E53)
$EndOfChain = 0xFFFF
$MaxModels = 100
$InverterUnit = 1
$MeterCandidates = 10
$MaxRegistersPerRead = 125
# The common model carries the serial number in registers 48 to 63 of its body.
$CommonModel = 1
$SerialRegisters = 48..63
$ModbusProtocolError = 'Modbus exception'

$Http = New-Object System.Net.Http.HttpClient
$Http.Timeout = [TimeSpan]::FromSeconds(15)

# -- web interface -------------------------------------------------------------

function Get-Hex([System.Security.Cryptography.HashAlgorithm]$Hasher, [string]$Text) {
    $bytes = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function Send-Get([string]$Path, [string]$Authorization) {
    $request = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::Get), "http://$InverterHost$Path"
    if ($Authorization) { [void]$request.Headers.TryAddWithoutValidation('Authorization', $Authorization) }
    return $Http.SendAsync($request).GetAwaiter().GetResult()
}

function Get-DigestHeader($Response, [string]$Uri) {
    # The inverter sends its challenge in X-WWW-Authenticate, so that a browser
    # shows no login box of its own.
    $values = $null
    if (-not $Response.Headers.TryGetValues('X-WWW-Authenticate', [ref]$values)) {
        if (-not $Response.Headers.TryGetValues('WWW-Authenticate', [ref]$values)) { return $null }
    }
    $challenge = @{}
    foreach ($match in [regex]::Matches(($values -join ','), '(\w+)="?([^",]*)"?')) {
        $challenge[$match.Groups[1].Value] = $match.Groups[2].Value
    }
    if (-not $challenge.nonce) { return $null }
    $qop = ($challenge.qop -split ',')[0]
    if ($challenge.nonce -eq $script:LastNonce) { $script:NonceCount++ } else { $script:NonceCount = 1 }
    $script:LastNonce = $challenge.nonce
    $nc = '{0:x8}' -f $script:NonceCount
    $cnonce = -join ((1..16) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    $secret = Get-Hex $script:SecretHasher "$($script:Role):$($challenge.realm):$($script:Password)"
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $ha2 = Get-Hex $sha256 "GET:$Uri"
    $digest = Get-Hex $sha256 "${secret}:$($challenge.nonce):${nc}:${cnonce}:${qop}:$ha2"
    $header = "Digest username=`"$($script:Role)`", realm=`"$($challenge.realm)`", nonce=`"$($challenge.nonce)`", uri=`"$Uri`", response=`"$digest`", qop=$qop, nc=$nc, cnonce=`"$cnonce`""
    if ($challenge.opaque) { $header += ", opaque=`"$($challenge.opaque)`"" }
    return $header
}

function Read-Endpoint([string]$Path, [bool]$WithLogin) {
    try {
        $response = Send-Get $Path
        if ($WithLogin -and [int]$response.StatusCode -eq 401) {
            # The login signs its path without the query; the others carry none.
            $header = Get-DigestHeader $response $Path.Split('?')[0]
            if ($header) { $response = Send-Get $Path $header }
        }
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return @{ status = [int]$response.StatusCode; body = $body }
    } catch {
        return @{ status = 0; body = $_.Exception.GetBaseException().Message }
    }
}

# -- JSON and redaction ------------------------------------------------------------
# A small parser of its own: ConvertFrom-Json refuses keys that differ in case
# only, which the channel names have ("On" and "on"), and Windows PowerShell's
# JavaScriptSerializer accepts {foo:1}. Redacting token by token keeps every
# value whole, which replacing text in the finished document did not.

function ConvertFrom-JsonString([string]$Token) {
    $inner = $Token.Substring(1, $Token.Length - 2)
    if (-not $inner.Contains('')) { return $inner }
    return [regex]::Replace($inner, '\\(u[0-9a-fA-F]{4}|.)', {
        param($match)
        $escape = $match.Groups[1].Value
        if ($escape.Length -eq 5) { return [string][char][Convert]::ToInt32($escape.Substring(1), 16) }
        return $JsonEscape[$escape]
    })
}

function ConvertTo-JsonString([string]$Value) {
    $escaped = [regex]::Replace($Value, '["\\\x00-\x1f]', {
        param($match)
        $character = $match.Value
        if ($character -eq '"' -or $character -eq '\') { return '\' + $character }
        return '\u{0:x4}' -f [int][char]$character
    })
    return '"' + $escaped + '"'
}

function Get-KnownPattern([string]$Value) {
    # An address stays whole: replaced as a part, the gateway 192.168.250.1
    # cut 192.168.250.181 down to "<redacted>81".
    $escaped = [regex]::Escape($Value)
    if ($Value -match $IPv4) { return '(?<![\d.])' + $escaped + '(?![\d.]*\d)' }
    return $escaped
}

function Protect-Text([string]$Value) {
    # Two compiled expressions instead of one per value: a login export has
    # some 100 000 strings.
    if ($script:KnownExpression) { $Value = $script:KnownExpression.Replace($Value, $Redacted) }
    return $script:PatternExpression.Replace($Value, $Redacted)
}

function Get-HostPattern([string]$HostName) {
    # A word of its own: a host named "pv" took "pv_power" apart.
    if ($HostName -match $IPv4) { return Get-KnownPattern $HostName }
    return '(?<![\w-])' + [regex]::Escape($HostName) + '(?![\w-])'
}

function Set-KnownValues {
    # The longest first, so that a value inside another does not cut it.
    $patterns = @($script:Known | Sort-Object Length -Descending | ForEach-Object { Get-KnownPattern $_ })
    if ($script:HostName) { $patterns = @(Get-HostPattern $script:HostName) + $patterns }
    $script:KnownExpression = $null
    # Any spelling: .NET writes the host in lower case into its error texts.
    if ($patterns) { $script:KnownExpression = New-Object regex (($patterns -join '|'), 'Compiled, IgnoreCase') }
    $script:PatternExpression = New-Object regex (($SensitivePattern -join '|'), 'Compiled')
}

function Protect-JsonString([string]$Token, [bool]$Sensitive) {
    $value = ConvertFrom-JsonString $Token
    if ($Sensitive) {
        if ($value.Trim().Length -ge $MinKnownLength) { [void]$script:Known.Add($value.Trim()) }
        return ConvertTo-JsonString $Redacted
    }
    if ($script:Collecting) { return $Token }
    $protected = Protect-Text $value
    if ($protected -ceq $value) { return $Token }
    return ConvertTo-JsonString $protected
}

function Protect-JsonNumber([string]$Token, [bool]$Sensitive) {
    if ($Sensitive) {
        if ($Token.Length -ge $MinKnownLength) { [void]$script:Known.Add($Token) }
        return ConvertTo-JsonString $Redacted
    }
    if ($Token -match $LongNumber -or $script:Known.Contains($Token)) { return ConvertTo-JsonString $Redacted }
    return $Token
}

function Read-JsonDocument([string]$Text) {
    <# The document again, redacted as $script:Redacting says; throws when it is no JSON.

    One loop with a stack of open containers: a function call per token made
    a login export take two minutes. Each frame knows what it expects next. #>
    $sensitiveKey = New-Object regex ($SensitiveKey, 'IgnoreCase')
    $parents = New-Object System.Collections.Generic.Stack[object]
    $frame = @{ kind = 'root'; expect = 'value'; sensitive = $false; parts = $null; key = $null; valueSensitive = $false }
    $result = $null
    foreach ($match in [regex]::Matches($Text, $JsonToken)) {
        $token = $match.Value
        $first = $token[0]
        if ($first -eq ' ' -or $first -eq "`t" -or $first -eq "`r" -or $first -eq "`n") { continue }
        $value = $null
        $expect = $frame.expect
        if ($expect -eq 'value' -or $expect -eq 'value-or-end') {
            $sensitive = $frame.sensitive
            if ($frame.kind -eq 'object') { $sensitive = $frame.valueSensitive }
            if ($token -ceq '{' -or $token -ceq '[') {
                $parents.Push($frame)
                $kind = 'array'
                $opening = 'value-or-end'
                if ($token -ceq '{') { $kind = 'object'; $opening = 'key-or-end' }
                $frame = @{ kind = $kind; expect = $opening; sensitive = $sensitive; parts = (New-Object System.Collections.Generic.List[string]); key = $null; valueSensitive = $false }
                continue
            }
            if ($token -ceq ']' -and $expect -eq 'value-or-end') {
                $value = '[]'
                $frame = $parents.Pop()
            } elseif ($first -eq '"' -and $token.Length -ge 2) {
                $value = $token
                if ($script:Redacting -and ($sensitive -or -not $script:Collecting)) {
                    # Inline for the common case: a function call per string
                    # cost more than all the redaction itself.
                    $inner = $token.Substring(1, $token.Length - 2)
                    if ($sensitive -or $inner.Contains('\')) {
                        $value = Protect-JsonString $token $sensitive
                    } else {
                        $protected = $inner
                        if ($script:KnownExpression) { $protected = $script:KnownExpression.Replace($protected, $Redacted) }
                        $protected = $script:PatternExpression.Replace($protected, $Redacted)
                        if ($protected -cne $inner) { $value = ConvertTo-JsonString $protected }
                    }
                }
            } elseif (($first -eq '-' -and $token.Length -gt 1) -or ($first -ge '0' -and $first -le '9')) {
                $value = $token
                if ($script:Redacting -and ($sensitive -or $token.Length -ge 12 -or $script:Known.Contains($token))) { $value = Protect-JsonNumber $token $sensitive }
            } elseif ($token -cin 'true', 'false', 'null') {
                $value = $token
            } else {
                throw "Unexpected '$token' in the JSON."
            }
        } elseif ($expect -eq 'key-or-end' -or $expect -eq 'key') {
            if ($token -ceq '}' -and $expect -eq 'key-or-end') {
                $value = '{}'
                $frame = $parents.Pop()
            } elseif ($first -eq '"' -and $token.Length -ge 2) {
                $key = $token.Substring(1, $token.Length - 2)
                if ($key.Contains('\')) { $key = ConvertFrom-JsonString $token }
                $frame.valueSensitive = $frame.sensitive -or $sensitiveKey.IsMatch($key)
                $frame.key = $token
                if ($script:Redacting -and -not $script:Collecting) {
                    $protected = $key
                    if ($script:KnownExpression) { $protected = $script:KnownExpression.Replace($protected, $Redacted) }
                    $protected = $script:PatternExpression.Replace($protected, $Redacted)
                    if ($protected -cne $key) { $frame.key = ConvertTo-JsonString $protected }
                }
                $frame.expect = 'colon'
                continue
            } else {
                throw "An object key is no string: '$token'."
            }
        } elseif ($expect -eq 'colon') {
            if ($token -cne ':') { throw "No ':' after the key $($frame.key)." }
            $frame.expect = 'value'
            continue
        } elseif ($expect -eq 'comma-or-end') {
            $closing = ']'
            $next = 'value'
            if ($frame.kind -eq 'object') { $closing = '}'; $next = 'key' }
            if ($token -ceq ',') {
                $frame.expect = $next
                continue
            }
            if ($token -cne $closing) { throw "Unexpected '$token' in the JSON." }
            $value = '[' + ($frame.parts -join ',') + ']'
            if ($frame.kind -eq 'object') { $value = '{' + ($frame.parts -join ',') + '}' }
            # Its values were collected on the way down; the container goes as a whole.
            if ($frame.sensitive -and $script:Redacting) { $value = ConvertTo-JsonString $Redacted }
            $frame = $parents.Pop()
        } else {
            throw "Unexpected '$token' after the JSON."
        }
        # A value is complete: it belongs to the frame now on top.
        if ($frame.kind -eq 'root') {
            $result = $value
            $frame.expect = 'done'
        } elseif ($frame.kind -eq 'object') {
            $frame.parts.Add($frame.key + ':' + $value)
            $frame.expect = 'comma-or-end'
        } else {
            $frame.parts.Add($value)
            $frame.expect = 'comma-or-end'
        }
    }
    if ($frame.kind -ne 'root' -or $frame.expect -ne 'done') { throw 'The JSON ends early.' }
    return $result
}

function Test-JsonText([string]$Text) {
    $script:Redacting = $false
    try {
        [void](Read-JsonDocument $Text)
        return $true
    } catch {
        return $false
    }
}

function ConvertTo-ExportBodies([string[]]$Bodies, [bool]$Redact, [string]$HostName) {
    <# Every body as a JSON value: itself if it is JSON, a string otherwise.

    Redacting reads all bodies twice: a serial number found under its key in
    one answer is replaced in every answer, also in those read before it. #>
    $script:Known = New-Object 'System.Collections.Generic.HashSet[string]'
    $script:HostName = $HostName
    $script:Redacting = $Redact
    $script:Collecting = $true
    if ($Redact) {
        foreach ($body in $Bodies) {
            try { [void](Read-JsonDocument $body) } catch { }
        }
    }
    $script:Collecting = $false
    Set-KnownValues
    $values = @()
    foreach ($body in $Bodies) {
        $script:Redacting = $Redact
        try { $values += , (Read-JsonDocument $body) }
        catch { $values += , (ConvertTo-JsonString (Protect-TextIf $body $Redact)) }
    }
    return , $values
}

function Protect-TextIf([string]$Value, [bool]$Redact) {
    if ($Redact) { return Protect-Text $Value }
    return $Value
}

# -- Modbus ----------------------------------------------------------------------

function Read-Exactly($Stream, [int]$Count) {
    $buffer = New-Object byte[] $Count
    $read = 0
    while ($read -lt $Count) {
        $chunk = $Stream.Read($buffer, $read, $Count - $read)
        if ($chunk -le 0) { throw 'The connection closed.' }
        $read += $chunk
    }
    return $buffer
}

function Read-Registers($Stream, [int]$Unit, [int]$Address, [int]$Count) {
    $script:TransactionId = ($script:TransactionId + 1) % 65536
    # Function 3, read holding registers: the only request this tool sends.
    $request = [byte[]]@(
        ($script:TransactionId -shr 8), ($script:TransactionId -band 255), 0, 0, 0, 6,
        $Unit, 3, ($Address -shr 8), ($Address -band 255), 0, $Count)
    $Stream.Write($request, 0, $request.Length)
    $header = Read-Exactly $Stream 9
    if ($header[7] -ge 0x80) { throw "$ModbusProtocolError $($header[8])" }
    $data = Read-Exactly $Stream $header[8]
    $registers = New-Object int[] ($data.Length / 2)
    for ($i = 0; $i -lt $registers.Length; $i++) { $registers[$i] = $data[2 * $i] * 256 + $data[2 * $i + 1] }
    return , $registers
}

function Read-SunSpecUnit([scriptblock]$Read, [int]$Unit, [bool]$Redact) {
    <# The unit's SunSpec models; what was read stays when a later read fails.

    Some devices refuse the read past their last model instead of sending the
    end marker; the integration keeps the models in that case too. #>
    $models = New-Object System.Collections.Generic.List[object]
    $result = [ordered]@{ complete = $false; error = $null; models = $models }
    try {
        $marker = & $Read $Unit $SunSpecBase 2
        if ($marker[0] -ne $SunSpecMarker[0] -or $marker[1] -ne $SunSpecMarker[1]) { throw "No SunSpec marker at $SunSpecBase." }
        $address = $SunSpecBase + 2
        while ($models.Count -lt $MaxModels) {
            $head = & $Read $Unit $address 2
            if ($head[0] -eq $EndOfChain) {
                $result.complete = $true
                break
            }
            $body = New-Object System.Collections.Generic.List[int]
            for ($offset = 0; $offset -lt $head[1]; $offset += $MaxRegistersPerRead) {
                $count = [Math]::Min($MaxRegistersPerRead, $head[1] - $offset)
                $body.AddRange([int[]](& $Read $Unit ($address + 2 + $offset) $count))
            }
            if ($Redact -and $head[0] -eq $CommonModel) {
                foreach ($index in $SerialRegisters) { if ($index -lt $body.Count) { $body[$index] = 0 } }
            }
            $models.Add([ordered]@{ address = $address; id = $head[0]; length = $head[1]; registers = $body.ToArray() })
            $address += 2 + $head[1]
        }
    } catch {
        $result.error = $_.Exception.GetBaseException().Message
    }
    return $result
}

function Read-ModbusUnits([scriptblock]$Read, [int[]]$Units, [bool]$Redact) {
    # An absent meter does not end the search: a later one may answer.
    $results = [ordered]@{}
    foreach ($unit in $Units) {
        Write-Host "Reading Modbus unit $unit"
        $results["$unit"] = Read-SunSpecUnit $Read $unit $Redact
    }
    return $results
}

function Connect-Modbus {
    if ($script:ModbusClient) { $script:ModbusClient.Dispose() }
    $script:ModbusClient = New-Object System.Net.Sockets.TcpClient
    if (-not $script:ModbusClient.ConnectAsync($InverterHost, $ModbusPort).Wait(5000)) { throw "No answer on port $ModbusPort." }
    $script:ModbusStream = $script:ModbusClient.GetStream()
    $script:ModbusStream.ReadTimeout = 3000
}

function Read-Modbus([bool]$Redact) {
    $read = {
        param($Unit, $Address, $Count)
        try {
            return , (Read-Registers $script:ModbusStream $Unit $Address $Count)
        } catch {
            # Only an exception reply keeps request and answer in step; after a
            # timeout a late answer would pass for the next request's.
            if ($_.Exception.Message -notlike "$ModbusProtocolError*") { Connect-Modbus }
            throw
        }
    }
    try {
        Connect-Modbus
        $units = @($InverterUnit) + @($FirstMeterUnit..($FirstMeterUnit + $MeterCandidates - 1))
        return [ordered]@{ port = $ModbusPort; units = (Read-ModbusUnits $read $units $Redact) }
    } catch {
        return [ordered]@{ port = $ModbusPort; error = $_.Exception.GetBaseException().Message }
    } finally {
        if ($script:ModbusClient) { $script:ModbusClient.Dispose() }
    }
}

function ConvertTo-ModbusJson($Modbus, [bool]$Redact) {
    # Registers are measurements, never text to redact; only the error texts
    # can name the host.
    if ($Modbus.error) { $Modbus.error = Protect-TextIf $Modbus.error $Redact }
    if ($Modbus.units) {
        foreach ($unit in $Modbus.units.Values) {
            if ($unit.error) { $unit.error = Protect-TextIf $unit.error $Redact }
        }
    }
    return ConvertTo-Json $Modbus -Depth 8 -Compress
}

# -- the export ------------------------------------------------------------------

function New-ExportText([string]$Login, [bool]$Redact, $Modbus, [string[]]$Paths, [int[]]$Statuses, [string[]]$Bodies) {
    # First the bodies: they find the values the Modbus error texts are redacted by.
    $values = ConvertTo-ExportBodies $Bodies $Redact $InverterHost
    $modbusJson = 'null'
    if ($Modbus) { $modbusJson = ConvertTo-ModbusJson $Modbus $Redact }
    $entries = @()
    for ($i = 0; $i -lt $Paths.Count; $i++) {
        $entries += "    $(ConvertTo-JsonString $Paths[$i]): {`"status`": $($Statuses[$i]), `"body`": $($values[$i])}"
    }
    $loginJson = 'null'
    if ($Login) { $loginJson = ConvertTo-JsonString $Login }
    $text = "{`n  `"tool`": `"inverter_dump`",`n  `"created`": `"$((Get-Date).ToString('s'))`",`n  `"login`": $loginJson,`n  `"redacted`": $($Redact.ToString().ToLower()),`n  `"modbus`": $modbusJson,`n  `"endpoints`": {`n$($entries -join ",`n")`n  }`n}`n"
    if (-not (Test-JsonText $text)) { throw 'The export came out as invalid JSON; nothing was saved.' }
    return $text
}

function Invoke-Dump {
    if (-not $script:InverterHost) { $script:InverterHost = Read-Host 'Inverter IP address or host name' }
    $script:InverterHost = $script:InverterHost.Trim()

    $common = Read-Endpoint '/api/status/common' $false
    $webAnswers = $common.status -ne 0
    # A Datamanager or older firmware answers without this endpoint; its
    # Solar API and Modbus are still worth reading.
    $webInterface = $common.status -eq 200
    if (-not $webAnswers) { Write-Host "No web server answers at ${InverterHost}: $($common.body)" }

    $paths = [ordered]@{}
    if ($webAnswers) { foreach ($path in $PublicPaths) { $paths[$path] = $false } }

    $loginRole = $null
    $answer = 'n'
    if ($webInterface) { $answer = Read-Host 'Also read the settings that need a login? [y/N]' }
    if ($answer -match '^[yYjJ]') {
        $script:Role = (Read-Host 'Login (customer or technician) [customer]').Trim().ToLower()
        if (-not $script:Role) { $script:Role = 'customer' }
        $secure = Read-Host "Password of $($script:Role)" -AsSecureString
        $script:Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        # Older firmware hashes the login with MD5 and says so per user; an
        # answer without the field means SHA-256, as in the integration.
        $version = $null
        try {
            $options = ($common.body | ConvertFrom-Json).authenticationOptions
            $version = $options.digest."$($script:Role)HashingVersion"
        } catch {
            Write-Host 'The hashing of the login is not readable; trying SHA-256.'
        }
        if ($version -eq 1) { $script:SecretHasher = [System.Security.Cryptography.MD5]::Create() }
        else { $script:SecretHasher = [System.Security.Cryptography.SHA256]::Create() }
        $login = Read-Endpoint "/api/commands/Login?user=$($script:Role)" $true
        if ($login.status -eq 200) {
            $loginRole = $script:Role
            foreach ($path in $LoginPaths) { $paths[$path] = $true }
        } else {
            Write-Host "The inverter refused the login (HTTP $($login.status)); reading without it."
        }
    }
    $readModbus = -not ((Read-Host 'Also read the SunSpec registers over Modbus TCP (Modbus must be on)? [Y/n]') -match '^[nN]')
    if (-not $webAnswers -and -not $readModbus) {
        Write-Host 'Nothing left to read.'
        exit 1
    }
    $redact = -not ((Read-Host 'Replace serial numbers, addresses and names for sharing? [Y/n]') -match '^[nN]')

    $statuses = @()
    $bodies = @()
    foreach ($path in $paths.Keys) {
        Write-Host "Reading $path"
        $result = Read-Endpoint $path $paths[$path]
        $statuses += $result.status
        $bodies += , [string]$result.body
    }
    $modbus = $null
    if ($readModbus) { $modbus = Read-Modbus $redact }

    Write-Host 'Writing the file'
    $text = New-ExportText $loginRole $redact $modbus @($paths.Keys) $statuses $bodies
    $target = Join-Path $PSScriptRoot "fronius-dump-$((Get-Date).ToString('yyyyMMdd-HHmmss')).json"
    [IO.File]::WriteAllText($target, $text, (New-Object Text.UTF8Encoding $false))
    Write-Host ''
    Write-Host "Saved: $target"
    if ($redact) { Write-Host 'Check the file before you share it: the replacement cannot know every field.' }
}

# Dot-sourced by inverter_dump.tests.ps1, which calls the functions alone.
if ($MyInvocation.InvocationName -ne '.') { Invoke-Dump }
