# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT

# AzdEnv.psm1
# Purpose: Read and write azd environment values from hook scripts.

#Requires -Version 7.0

function Get-AzdEnvValues {
    <#
    .SYNOPSIS
        Returns the active azd environment values.
    .OUTPUTS
        [hashtable] Variable name -> value.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param()

    $raw = azd env get-values --output json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'azd env get-values failed. Run this script from the azd project root.'
    }
    $values = @{}
    ($raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $values[$_.Name] = [string]$_.Value }
    return $values
}

function Set-AzdEnvValues {
    <#
    .SYNOPSIS
        Writes several values into the active azd environment.
    .PARAMETER Values
        Variable name -> value.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Values
    )

    foreach ($entry in $Values.GetEnumerator()) {
        azd env set $entry.Key "$($entry.Value)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "azd env set $($entry.Key) failed."
        }
    }
}

Export-ModuleMember -Function @(
    'Get-AzdEnvValues'
    'Set-AzdEnvValues'
)
