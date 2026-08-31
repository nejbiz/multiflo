param(
    [Parameter(Mandatory = $true)]
    [string] $AssemblyPath,

    [Parameter(Mandatory = $true)]
    [string[]] $Path
)

if ([Environment]::Is64BitProcess) {
    throw 'Run this offline evidence helper with 32-bit Windows PowerShell.'
}

$assemblyDirectory = Split-Path -Parent $AssemblyPath
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($sender, $eventArgs)
    $dependencyName = (New-Object Reflection.AssemblyName($eventArgs.Name)).Name + '.dll'
    $candidate = Join-Path $assemblyDirectory $dependencyName
    if (Test-Path -LiteralPath $candidate) {
        return [Reflection.Assembly]::LoadFrom($candidate)
    }
    return $null
})

$assembly = [Reflection.Assembly]::LoadFrom($AssemblyPath)
$flags = [Reflection.BindingFlags]'Public,NonPublic,Static,Instance'
$key = [byte[]](37, 63, 35, 20, 63, 105, 96, 63)
$stepTypes = @{
    1 = @{ type = 's'; command = 0x008f; builder = 'x' }
    2 = @{ type = 'ba'; command = 0x0090; builder = 'w' }
    3 = @{ type = 'ba'; command = 0x0091; builder = 'w' }
    11 = @{ type = 'k'; command = 0x00a3; builder = 'w' }
}
$enumType = $assembly.GetType('BTI406Interface.EnumStepType')

foreach ($item in $Path) {
    foreach ($resolved in @(Resolve-Path -Path $item)) {
        $ciphertext = [IO.File]::ReadAllBytes($resolved)
        $des = [Security.Cryptography.DESCryptoServiceProvider]::new()
        try {
            $des.Key = $key
            $des.IV = $key
            $decryptor = $des.CreateDecryptor()
            try {
                $plaintext = $decryptor.TransformFinalBlock(
                    $ciphertext,
                    0,
                    $ciphertext.Length
                )
            }
            finally {
                $decryptor.Dispose()
            }
        }
        finally {
            $des.Dispose()
        }
        [xml] $document = [Text.Encoding]::UTF8.GetString($plaintext)

        foreach ($step in @($document.ProgramProtocol.Step)) {
            $stepType = [int] $step.Type
            if (-not $stepTypes.ContainsKey($stepType)) {
                continue
            }
            $mapping = $stepTypes[$stepType]
            $codecType = $assembly.GetType($mapping.type)
            if ($stepType -in 2, 3) {
                $enumValue = [Enum]::ToObject($enumType, $stepType)
                $constructor = $codecType.GetConstructors($flags) | Where-Object {
                    $_.GetParameters().Count -eq 1
                }
                $codec = $constructor.Invoke(@($enumValue))
            }
            else {
                $codec = [Activator]::CreateInstance($codecType, $true)
            }

            $parser = $codecType.GetMethods($flags) | Where-Object {
                $_.Name -eq 'a' -and
                $_.ReturnType -eq [bool] -and
                $_.GetParameters().Count -eq 1 -and
                $_.GetParameters()[0].ParameterType -eq [string]
            }
            if (-not $parser.Invoke($codec, @([string] $step.Definition))) {
                throw "Vendor definition parser rejected $resolved step $stepType"
            }
            $builder = $codecType.GetMethods($flags) | Where-Object {
                $_.Name -eq $mapping.builder -and
                $_.ReturnType -eq [char[]] -and
                $_.GetParameters().Count -eq 0
            }
            $payload = [char[]] $builder.Invoke($codec, @())
            $body = [byte[]]::new(1 + $payload.Length)
            $body[0] = [byte] $document.ProgramProtocol.PlateTypeEnum
            for ($index = 0; $index -lt $payload.Length; $index++) {
                $body[$index + 1] = [byte] $payload[$index]
            }

            [ordered]@{
                file = [IO.Path]::GetFileName($resolved)
                comments = [string] $document.ProgramProtocol.Comments
                step_type = $stepType
                command = '0x{0:x4}' -f $mapping.command
                definition = [string] $step.Definition
                body_hex = ([BitConverter]::ToString($body) -replace '-', ' ').ToLower()
            } | ConvertTo-Json -Compress
        }
    }
}
