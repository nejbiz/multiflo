param(
    [Parameter(Mandatory = $true)]
    [string] $AssemblyPath
)

if (-not [Environment]::Is64BitProcess) {
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

    $shakeType = $assembly.GetType('k')
    $shake = [Activator]::CreateInstance($shakeType, $true)
    $speedMethod = $shakeType.GetMethods($flags) | Where-Object {
        $_.Name -eq 'x' -and
        $_.ReturnType -eq [byte] -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType -eq [string]
    }

    $helperType = $assembly.GetType('o')
    $helper = [Activator]::CreateInstance($helperType, $true)
    $axisMethod = $helperType.GetMethods($flags) | Where-Object {
        $_.Name -eq 'ay' -and
        $_.ReturnType -eq [byte] -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType -eq [string]
    }
    $flowMethod = $helperType.GetMethods($flags) | Where-Object {
        $_.Name -eq 'aj' -and
        $_.ReturnType -eq [byte] -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType -eq [string]
    }
    $axisTarget = if ($axisMethod.IsStatic) { $null } else { $helper }
    $flowTarget = if ($flowMethod.IsStatic) { $null } else { $helper }

    [ordered]@{
        shake_speed = [ordered]@{
            low = $speedMethod.Invoke($shake, @('Low (3 Hz)'))
            medium = $speedMethod.Invoke($shake, @('Medium (5 Hz)'))
            high = $speedMethod.Invoke($shake, @('High (7 Hz)'))
        }
        shake_axis = [ordered]@{
            x = $axisMethod.Invoke($axisTarget, @('X-axis'))
            y = $axisMethod.Invoke($axisTarget, @('Y-axis'))
        }
        peristaltic_flow = [ordered]@{
            low = $flowMethod.Invoke($flowTarget, @('Low'))
            medium = $flowMethod.Invoke($flowTarget, @('Medium'))
            high = $flowMethod.Invoke($flowTarget, @('High'))
        }
    } | ConvertTo-Json -Depth 4
    exit
}

throw 'Run this offline evidence helper with 32-bit Windows PowerShell.'
