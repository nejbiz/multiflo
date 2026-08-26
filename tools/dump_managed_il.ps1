param(
    [Parameter(Mandatory = $true)]
    [string] $AssemblyPath,

    [Parameter(Mandatory = $true)]
    [string] $TypeName,

    [Parameter(Mandatory = $true)]
    [string[]] $MethodName
)

$assemblyDirectory = Split-Path -Parent $AssemblyPath

[AppDomain]::CurrentDomain.add_ReflectionOnlyAssemblyResolve({
    param($sender, $eventArgs)
    $dependencyName = (New-Object Reflection.AssemblyName($eventArgs.Name)).Name + '.dll'
    $candidate = Join-Path $assemblyDirectory $dependencyName
    if (Test-Path -LiteralPath $candidate) {
        return [Reflection.Assembly]::ReflectionOnlyLoadFrom($candidate)
    }
    return [Reflection.Assembly]::ReflectionOnlyLoad($eventArgs.Name)
})

$assembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($AssemblyPath)
$module = $assembly.ManifestModule
try {
    $types = $assembly.GetTypes()
}
catch [Reflection.ReflectionTypeLoadException] {
    $types = $_.Exception.Types | Where-Object { $_ }
}

$opcodes = @{}
[Reflection.Emit.OpCodes].GetFields([Reflection.BindingFlags]'Public,Static') | ForEach-Object {
    $opcode = $_.GetValue($null)
    $opcodeKey = [int]$opcode.Value -band 0xffff
    $opcodes[[int]$opcodeKey] = $opcode
}

function Resolve-Token([string] $operandType, [int] $token) {
    try {
        switch ($operandType) {
            'InlineString' { return '"' + $module.ResolveString($token) + '"' }
            'InlineMethod' {
                $member = $module.ResolveMethod($token)
                return $member.DeclaringType.FullName + '::' + $member.ToString()
            }
            'InlineField' {
                $member = $module.ResolveField($token)
                return $member.DeclaringType.FullName + '::' + $member.ToString()
            }
            'InlineType' { return $module.ResolveType($token).ToString() }
            'InlineTok' { return $module.ResolveMember($token).ToString() }
            'InlineSig' { return 'signature 0x{0:X8}' -f $token }
            default { return 'token 0x{0:X8}' -f $token }
        }
    }
    catch {
        return 'unresolved token 0x{0:X8}: {1}' -f $token, $_.Exception.Message
    }
}

function Dump-MethodIL([Reflection.MethodInfo] $method) {
    Write-Output "=== $($method.DeclaringType.FullName)::$method ==="
    $body = $method.GetMethodBody()
    if (-not $body) {
        Write-Output '<no IL body>'
        return
    }

    $bytes = $body.GetILAsByteArray()
    $position = 0
    while ($position -lt $bytes.Length) {
        $offset = $position
        $value = [int]$bytes[$position]
        $position++
        if ($value -eq 0xfe) {
            $value = [int](0xfe00 -bor $bytes[$position])
            $position++
        }

        $opcode = $opcodes[[int]$value]
        $operandType = $opcode.OperandType.ToString()
        $operand = ''

        switch ($operandType) {
            'InlineNone' { }
            'ShortInlineBrTarget' {
                $delta = [int]$bytes[$position]
                if ($delta -ge 128) { $delta -= 256 }
                $position++
                $operand = 'IL_{0:X4}' -f ($position + $delta)
            }
            'InlineBrTarget' {
                $delta = [BitConverter]::ToInt32($bytes, $position)
                $position += 4
                $operand = 'IL_{0:X4}' -f ($position + $delta)
            }
            'ShortInlineI' {
                $operand = [int]$bytes[$position]
                if ($operand -ge 128) { $operand -= 256 }
                $position++
            }
            'InlineI' {
                $operand = [BitConverter]::ToInt32($bytes, $position)
                $position += 4
            }
            'InlineI8' {
                $operand = [BitConverter]::ToInt64($bytes, $position)
                $position += 8
            }
            'ShortInlineR' {
                $operand = [BitConverter]::ToSingle($bytes, $position)
                $position += 4
            }
            'InlineR' {
                $operand = [BitConverter]::ToDouble($bytes, $position)
                $position += 8
            }
            'ShortInlineVar' {
                $operand = $bytes[$position]
                $position++
            }
            'InlineVar' {
                $operand = [BitConverter]::ToUInt16($bytes, $position)
                $position += 2
            }
            'InlineSwitch' {
                $count = [BitConverter]::ToInt32($bytes, $position)
                $position += 4
                $baseOffset = $position + (4 * $count)
                $targets = @()
                for ($index = 0; $index -lt $count; $index++) {
                    $delta = [BitConverter]::ToInt32($bytes, $position)
                    $position += 4
                    $targets += 'IL_{0:X4}' -f ($baseOffset + $delta)
                }
                $operand = $targets -join ', '
            }
            default {
                $token = [BitConverter]::ToInt32($bytes, $position)
                $position += 4
                $operand = Resolve-Token $operandType $token
            }
        }

        Write-Output ('IL_{0:X4}: {1,-13} {2}' -f $offset, $opcode.Name, $operand)
    }
}

$type = $types | Where-Object FullName -eq $TypeName
if (-not $type) {
    throw "Type not found: $TypeName"
}

foreach ($name in $MethodName) {
    $type.GetMethods([Reflection.BindingFlags]'Public,NonPublic,Static,Instance,DeclaredOnly') |
        Where-Object Name -eq $name |
        ForEach-Object { Dump-MethodIL $_ }
}
