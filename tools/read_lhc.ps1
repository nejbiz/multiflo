param(
    [Parameter(Mandatory = $true)]
    [string[]] $Path,

    [switch] $FullXml
)

$key = [byte[]](37, 63, 35, 20, 63, 105, 96, 63)

foreach ($item in $Path) {
    $resolvedPaths = @(Resolve-Path -Path $item)
    foreach ($resolved in $resolvedPaths) {
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

    $text = [Text.Encoding]::UTF8.GetString($plaintext)
    if ($FullXml) {
        $text
        continue
    }

    [xml] $document = $text
    $steps = @($document.ProgramProtocol.Step | ForEach-Object {
        [ordered]@{
            action = [string] $_.Action
            type = [int] $_.Type
            definition = [string] $_.Definition
        }
    })
    [ordered]@{
        file = [IO.Path]::GetFileName($resolved)
        lhc_version = [string] $document.ProgramProtocol.LHCVersion
        instrument = [string] $document.ProgramProtocol.InstrumentName
        plate_type = [int] $document.ProgramProtocol.PlateTypeEnum
        comments = [string] $document.ProgramProtocol.Comments
        steps = $steps
        archive_revision = [int] $document.ProgramProtocol.ArchiveRevision
    } | ConvertTo-Json -Depth 5 -Compress
    }
}
