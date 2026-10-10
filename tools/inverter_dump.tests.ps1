<#
Offline checks of inverter_dump.ps1: redaction, strict JSON, the SunSpec
walk, the Modbus wire and the login, on synthetic data. Exits 1 and names
each failed check.

    pwsh -NoProfile -File tools/inverter_dump.tests.ps1
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'inverter_dump.ps1')

$script:Failures = New-Object System.Collections.Generic.List[string]
$script:Checks = 0

function Assert-True([bool]$Condition, [string]$Name) {
    $script:Checks++
    if (-not $Condition) { $script:Failures.Add($Name) }
}

function Test-ValidJson([string]$Text) {
    # An independent parser where there is one, so the tool does not vouch
    # for its own output.
    if ($PSVersionTable.PSVersion.Major -lt 6) { return Test-JsonText $Text }
    try {
        [System.Text.Json.JsonDocument]::Parse($Text).Dispose()
        return $true
    } catch {
        return $false
    }
}

function Protect-One([string]$Body, [string]$HostName = '') {
    return (ConvertTo-ExportBodies @($Body) $true $HostName)[0]
}

# -- the shapes that came out as invalid JSON ----------------------------------

$cases = [ordered]@{
    'a long id'            = @('{"boardId":1234567890123}', '1234567890123')
    'an exponent'          = @('{"secret":1e12}', '1e12')
    'nested arrays'        = @('{"dns":[["192.168.1.1"],["192.168.1.2"]],"other":true}', '192.168.1')
    'an object value'      = @('{"password":{"value":"synthetic-secret-value"}}', 'synthetic-secret-value')
    'a key beneath one'    = @('{"psk":{"synthetic-secret-key":true}}', 'synthetic-secret-key')
    'an escaped repeat'    = @('{"serialNumber":"SN-ABC-123","description":"SN-ABC-123"}', 'ABC')
    'a public address'     = @('{"dns":["8.8.8.8"],"note":"via 8.8.8.8"}', '8.8.8.8')
}
foreach ($name in $cases.Keys) {
    $body, $secret = $cases[$name]
    $out = Protect-One $body
    Assert-True (Test-ValidJson $out) "$name stays valid JSON: $out"
    Assert-True (-not $out.Contains($secret)) "$name is redacted: $out"
}
Assert-True ((Protect-One '{"dns":[["192.168.1.1"]],"other":true}').Contains('"other":true')) 'a value after a sensitive array survives'

# -- what JSON is, on both PowerShell versions -----------------------------------

# JSON numbers are ASCII digits only; .NET's \d also takes other scripts' digits.
$arabicOne = [string][char]0x0661
foreach ($foreignDigits in @("[1$arabicOne]", "[1e$arabicOne]")) {
    $out = Protect-One $foreignDigits
    Assert-True ($out.StartsWith('"')) "$foreignDigits is kept as a string"
    Assert-True (Test-ValidJson $out) "$foreignDigits as a string is valid JSON"
}

foreach ($notJson in @('{foo:1}', "{'foo':'bar'}", '{"a":01}', '[1,]', '{"a" "b"}', '"', 'True', '[-]', ('[1' + [char]0xA0 + ']'), '{"a":1}}', '')) {
    $out = Protect-One $notJson
    Assert-True ($out.StartsWith('"')) "$notJson is kept as a string"
    Assert-True (Test-ValidJson $out) "$notJson as a string is valid JSON"
}
$caseKeys = (ConvertTo-ExportBodies @('{"On":1,"on":2}') $false '')[0]
Assert-True ($caseKeys -ceq '{"On":1,"on":2}') "keys differing in case are both kept: $caseKeys"

# -- what is redacted, and what stays ----------------------------------------------

$bodies = ConvertTo-ExportBodies @(
    '{"text":"device ABCD1234 ok"}',
    '{"serial":"ABCD1234"}'
) $true ''
Assert-True (-not $bodies[0].Contains('ABCD1234')) "a value found in a later answer is redacted in an earlier one: $($bodies[0])"

$errorText = Protect-One 'No such host is known. (inverter.example:80)' 'inverter.example'
Assert-True (-not $errorText.Contains('inverter.example')) "the host in an error text is redacted: $errorText"

$upper = Protect-One 'No such host is known. (inverter.example:80)' 'INVERTER.EXAMPLE'
Assert-True (-not $upper.Contains('inverter.example')) "the host is redacted in another spelling: $upper"

$short = Protect-One '{"pv_power":1,"note":"host pv down"}' 'pv'
Assert-True ($short.Contains('"pv_power"')) "a short host does not cut field names: $short"
Assert-True (-not $short.Contains('host pv down')) "a short host is redacted as a word: $short"

$network = Protect-One '{"gateway":"192.168.250.1","ipAddress":"192.168.250.181","other":"192.168.250.10"}'
Assert-True (-not ($network -match '<redacted>\d')) "an address is not cut to a remainder: $network"
Assert-True (-not $network.Contains('192.168.250')) "every private address is redacted: $network"

$kept = Protect-One '{"commandsApi":"10.1.0","v":236.69966666666724,"tokenFormatVersion":"1","ssid":"","on":1,"at":"2026-09-28T21:08:09+00:00"}'
foreach ($value in @('"10.1.0"', '236.69966666666724', '"tokenFormatVersion":"1"', '"on":1', '21:08:09')) {
    Assert-True ($kept.Contains($value)) "$value is kept: $kept"
}

$short = (ConvertTo-ExportBodies @('{"zip":1}', '{"count":1,"name":"1 of 1"}') $true '')[1]
Assert-True ($short -ceq '{"count":1,"name":"1 of 1"}') "a short sensitive value does not take every match: $short"

# Audit F09: words learned from a field description and from a sensitive
# object's settings were cut out of every answer, keys included.
$schema = ConvertTo-ExportBodies @(
    '{"psk":"synthetic-psk-value","_psk_meta":{"valueType":"String","displayType":"Password"}}',
    '{"channels":["COMBINER_STRING_01"],"valueType":"String","label":"Password"}'
) $true ''
Assert-True ($schema[0].Contains('"valueType":"String"')) "a field description is no secret: $($schema[0])"
Assert-True ($schema[1] -ceq '{"channels":["COMBINER_STRING_01"],"valueType":"String","label":"Password"}') "words of a field description stay elsewhere: $($schema[1])"
Assert-True (-not $schema[0].Contains('synthetic-psk-value')) "the secret beside its description is redacted: $($schema[0])"

$modes = ConvertTo-ExportBodies @(
    '{"ip":{"mode":"manual","hostname":"synthetic-host-name"}}',
    '{"BAT_M0_SOC_MODE":"manual","options":{"manual":1,"auto":2},"note":"synthetic-host-name"}'
) $true ''
Assert-True ($modes[1].StartsWith('{"BAT_M0_SOC_MODE":"manual","options":{"manual":1,"auto":2}')) "a mode word under a sensitive object stays elsewhere: $($modes[1])"
Assert-True (-not $modes[1].Contains('synthetic-host-name')) "a name under a sensitive object is still learned: $($modes[1])"

$device = Protect-One '{"hwrevisions":{"3PN10K-31000000000000017":"31000000000000017|4,071,594|0.6L__|"},"mac":"00:03:AC:12:34:56","commonName":"pilot-0.6e-3100000000000000019_1600000000"}'
foreach ($secret in @('31000000000000017', '12:34:56', '3100000000000000019')) {
    Assert-True (-not $device.Contains($secret)) "$secret is redacted: $device"
}
Assert-True ($device.Contains('4,071,594')) "a part number is kept: $device"

$ipv6 = Protect-One '{"a":"2001:db8:85a3::8a2e:370:7334","b":"FE80::1","c":"2001:0db8:0000:0000:0000:ff00:0042:8329"}'
foreach ($secret in @('2001:db8', 'FE80', '0042:8329')) {
    Assert-True (-not $ipv6.Contains($secret)) "the IPv6 address $secret is redacted: $ipv6"
}

# Audit F23: a Datamanager names its serial UniqueIdentifier (GetLoggerInfo).
$logger = Protect-One '{"Body":{"LoggerInfo":{"UniqueIdentifier":"240.100013","ProductID":"fronius-datamanager-card"}}}'
Assert-True (-not $logger.Contains('240.100013')) "the Datamanager serial is redacted: $logger"
Assert-True ($logger.Contains('fronius-datamanager-card')) "the product beside it stays: $logger"

$raw = (ConvertTo-ExportBodies @('{"serial":"ABCD1234"}') $false '')[0]
Assert-True ($raw.Contains('ABCD1234')) 'without redaction the serial is kept'

# -- the SunSpec walk --------------------------------------------------------------

function New-FakeUnit([int[]]$Common, [int[]]$Headers) {
    # Registers from 40000 on: marker, the common model, then what follows.
    $map = @{}
    $map[40000] = 0x5375
    $map[40001] = 0x6E53
    $map[40002] = 1
    $map[40003] = $Common.Length
    for ($i = 0; $i -lt $Common.Length; $i++) { $map[40004 + $i] = $Common[$i] }
    $next = 40004 + $Common.Length
    for ($i = 0; $i -lt $Headers.Length; $i++) { $map[$next + $i] = $Headers[$i] }
    return $map
}

$common = 1..65
$script:FakeRegisters = New-FakeUnit $common @()
$read = {
    param($Unit, $Address, $Count)
    if (-not $script:FakeRegisters.ContainsKey($Address)) { throw 'Modbus exception 2' }
    return , [int[]]@($Address..($Address + $Count - 1) | ForEach-Object { $script:FakeRegisters[$_] })
}
$refused = Read-SunSpecUnit $read 1 $true
Assert-True ($refused.models.Count -eq 1) 'a refused read past the last model keeps the models read'
Assert-True (-not $refused.complete -and $refused.error -like 'Modbus exception*') 'a refused read is no complete chain'
$registers = $refused.models[0].registers
Assert-True ((@($registers[48..63]) | Where-Object { $_ -ne 0 }).Count -eq 0) 'the serial registers are zeroed'
Assert-True ($registers[47] -eq 48 -and $registers[64] -eq 65) 'the registers beside the serial stay'

$script:FakeRegisters = New-FakeUnit $common @(0xFFFF, 0)
$complete = Read-SunSpecUnit $read 1 $false
Assert-True ($complete.complete -and -not $complete.error) 'the end marker completes the chain'
Assert-True ($complete.models[0].registers[50] -eq 51) 'without redaction the serial registers stay'

$long = 1..126
$script:FakeRegisters = New-FakeUnit $long @(0xFFFF, 0)
$split = Read-SunSpecUnit $read 1 $false
Assert-True ($split.models[0].registers.Count -eq 126 -and $split.models[0].registers[125] -eq 126) 'a model longer than one read is read whole'

$script:Answering = @{ 1 = (New-FakeUnit $common @(0xFFFF, 0)); 201 = (New-FakeUnit $common @(0xFFFF, 0)) }
$byUnit = {
    param($Unit, $Address, $Count)
    if (-not $script:Answering.ContainsKey($Unit)) { throw 'The connection closed.' }
    $map = $script:Answering[$Unit]
    return , [int[]]@($Address..($Address + $Count - 1) | ForEach-Object { $map[$_] })
}
$units = Read-ModbusUnits $byUnit @(1, 200, 201) $false 6>$null
Assert-True ($units['200'].error -and $units['201'].complete) 'an absent meter does not end the search'

$export = New-ExportText $null $true ([ordered]@{ port = 502; units = $units }) @('/a') @(200) @('{"zip":12345}')
Assert-True (Test-ValidJson $export) 'the whole export is valid JSON'
Assert-True ($export.Contains('[1,2,3,4,5')) 'register values are no text to redact'

# -- the Modbus wire (audit F31, Codex C-04) -----------------------------------------

function New-FakeStream([byte[]]$Reply) {
    # What the tool writes is kept in Sent; its reads are served from Reply.
    $stream = [pscustomobject]@{
        Sent  = New-Object System.Collections.Generic.List[byte]
        Reply = New-Object System.IO.MemoryStream (, $Reply)
    }
    $stream | Add-Member ScriptMethod Write {
        param($Buffer, $Offset, $Count)
        $this.Sent.AddRange([byte[]]$Buffer[$Offset..($Offset + $Count - 1)])
    }
    $stream | Add-Member ScriptMethod Read {
        param($Buffer, $Offset, $Count)
        return $this.Reply.Read($Buffer, $Offset, $Count)
    }
    return $stream
}

function Get-ReadError($Stream) {
    try {
        $null = Read-Registers $Stream 1 40000 2
        return ''
    } catch {
        return $_.Exception.Message
    }
}

$script:TransactionId = 0x1233
$wire = New-FakeStream ([byte[]]@(0x12, 0x34, 0, 0, 0, 7, 1, 3, 4, 0x53, 0x75, 0x6E, 0x53))
$marker = Read-Registers $wire 1 40000 2
$request = $wire.Sent.ToArray() -join ','
# Transaction 0x1234, protocol 0, six bytes; unit 1, function 3, 40000, two registers.
Assert-True ($request -eq '18,52,0,0,0,6,1,3,156,64,0,2') "a read asks for holding registers and nothing else: $request"
Assert-True ($marker.Count -eq 2 -and $marker[0] -eq 0x5375 -and $marker[1] -eq 0x6E53) "the answer comes back as registers: $($marker -join ',')"

$refusal = Get-ReadError (New-FakeStream ([byte[]]@(0, 1, 0, 0, 0, 3, 1, 0x83, 2)))
Assert-True ($refusal -eq "$ModbusProtocolError 2") "an exception reply is reported as one: $refusal"
$cut = Get-ReadError (New-FakeStream ([byte[]]@(0, 1, 0, 0)))
Assert-True ($cut -eq 'The connection closed.') "a reply cut short ends the read: $cut"

# Only an exception reply keeps request and answer in step; after any other
# failure a late answer would pass for the next request's registers.
$script:Connects = 0
& {
    function Connect-Modbus { $script:Connects++ }
    function Read-Registers { throw 'The operation has timed out.' }
    $null = Read-Modbus $false 6>$null
}
Assert-True ($script:Connects -eq 2 + $MeterCandidates) "a timeout reconnects before the next unit: $($script:Connects) connects"
$script:Connects = 0
& {
    function Connect-Modbus { $script:Connects++ }
    function Read-Registers { throw "$ModbusProtocolError 2" }
    $null = Read-Modbus $false 6>$null
}
Assert-True ($script:Connects -eq 1) "an exception reply keeps the connection: $($script:Connects) connects"

# -- the login (audit F31) -------------------------------------------------------------

$versions = '{"authenticationOptions":{"digest":{"customerHashingVersion":2,"technicianHashingVersion":1}}}'
Assert-True ((Get-SecretHasher $versions 'technician') -is [System.Security.Cryptography.MD5]) 'hashing version 1 logs in with MD5'
Assert-True ((Get-SecretHasher $versions 'customer') -is [System.Security.Cryptography.SHA256]) 'hashing version 2 logs in with SHA-256'
Assert-True ((Get-SecretHasher '{}' 'customer') -is [System.Security.Cryptography.SHA256]) 'no hashing version logs in with SHA-256'

function Get-TestHex($Hasher, [string]$Text) {
    # Independent of the tool's Get-Hex, so the tool does not vouch for itself.
    return [BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).Replace('-', '').ToLower()
}

$script:Role = 'technician'
$script:Password = 'synthetic-password'
$script:SecretHasher = [System.Security.Cryptography.MD5]::Create()
$script:Requests = New-Object System.Collections.Generic.List[object]
& {
    function Send-Get([string]$Path, [string]$Authorization) {
        $script:Requests.Add(@($Path, $Authorization))
        $response = New-Object System.Net.Http.HttpResponseMessage ([System.Net.HttpStatusCode]::OK)
        if (-not $Authorization) {
            $response.StatusCode = [System.Net.HttpStatusCode]::Unauthorized
            [void]$response.Headers.TryAddWithoutValidation('X-WWW-Authenticate', 'Digest realm="synthetic-realm", nonce="synthetic-nonce", qop="auth"')
        }
        $response.Content = New-Object System.Net.Http.StringContent ''
        return $response
    }
    $null = Read-Endpoint '/api/commands/Login?user=technician' $true
}
$header = [string]$script:Requests[$script:Requests.Count - 1][1]
$field = @{}
foreach ($match in [regex]::Matches($header, '(\w+)="?([^",]*)"?')) { $field[$match.Groups[1].Value] = $match.Groups[2].Value }
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$secret = Get-TestHex ([System.Security.Cryptography.MD5]::Create()) 'technician:synthetic-realm:synthetic-password'
$expected = Get-TestHex $sha256 "${secret}:synthetic-nonce:$($field.nc):$($field.cnonce):auth:$(Get-TestHex $sha256 'GET:/api/commands/Login')"
Assert-True ($field.uri -eq '/api/commands/Login') "the login signs its path without the query: $header"
Assert-True ($field.response -eq $expected) "the digest answers the challenge: $header"

# -- read only (Codex C-04) -------------------------------------------------------------

# The login is the one command the tool calls, and it is not on these lists.
$commands = @($PublicPaths + $LoginPaths) | Where-Object { $_ -like '/api/commands/*' }
Assert-True (@($commands).Count -eq 0) "no command endpoint is read: $($commands -join ', ')"

# -----------------------------------------------------------------------------------

if ($script:Failures.Count) {
    $script:Failures | ForEach-Object { Write-Host "FAILED: $_" }
    Write-Host "$($script:Failures.Count) of $($script:Checks) checks failed"
    exit 1
}
Write-Host "all $($script:Checks) checks passed (PowerShell $($PSVersionTable.PSVersion))"
