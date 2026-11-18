#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Publishes the collector Function App and grants data-plane roles to the Hosted Agent identity.
.DESCRIPTION
    Runs after `azd deploy`:
      1. Publishes src/functions to the Flex Consumption Function App (remote build).
      2. Resolves the principal IDs of the deployed Hosted Agent (agent identity) and grants:
         - Search Index Data Reader on the Foundry IQ search service
         - Cosmos DB Built-in Data Contributor on the Cosmos DB account
         - Storage Blob Data Contributor and Storage Blob Delegator on the storage account
.PARAMETER SkipFunctions
    Skip publishing the Function App.
.PARAMETER SkipAgentRoles
    Skip the agent role assignments.
.EXAMPLE
    ./scripts/postdeploy.ps1
.NOTES
    Runs via: azd deploy (postdeploy hook in azure.yaml)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$SkipFunctions,

    [Parameter(Mandatory = $false)]
    [switch]$SkipAgentRoles
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'AzdEnv.psm1') -Force

#region Functions

function Add-AgentRoleAssignments {
    <#
    .SYNOPSIS
        Grants the agent identity access to Search, Cosmos DB and Blob Storage (idempotent).
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)][string]$PrincipalId,
        [Parameter(Mandatory = $true)][hashtable]$EnvValues
    )

    $rg = $EnvValues['AZURE_RESOURCE_GROUP']
    $sub = $EnvValues['AZURE_SUBSCRIPTION_ID']
    $searchId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Search/searchServices/$($EnvValues['SEARCH_SERVICE_NAME'])"
    $storageId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Storage/storageAccounts/$($EnvValues['STORAGE_ACCOUNT_NAME'])"

    $assignments = @(
        @{ Scope = $searchId; Role = 'Search Index Data Reader' }
        @{ Scope = $storageId; Role = 'Storage Blob Data Contributor' }
        @{ Scope = $storageId; Role = 'Storage Blob Delegator' }
    )
    foreach ($a in $assignments) {
        az role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal `
            --role $a.Role --scope $a.Scope --only-show-errors -o none
        if ($LASTEXITCODE -ne 0) { throw "Role assignment '$($a.Role)' failed for $PrincipalId." }
    }

    $cosmos = $EnvValues['COSMOS_ACCOUNT_NAME']
    $existing = az cosmosdb sql role assignment list --account-name $cosmos --resource-group $rg `
        --query "[?principalId=='$PrincipalId'].id" -o tsv
    if (-not $existing) {
        az cosmosdb sql role assignment create --account-name $cosmos --resource-group $rg `
            --principal-id $PrincipalId --role-definition-id '00000000-0000-0000-0000-000000000002' `
            --scope '/' --only-show-errors -o none
        if ($LASTEXITCODE -ne 0) { throw "Cosmos DB role assignment failed for $PrincipalId." }
    }
    Write-Host "✅ Granted data-plane roles to agent identity $PrincipalId" -ForegroundColor Green
}

function Publish-Collector {
    <#
    .SYNOPSIS
        Publishes src/functions to the Function App with a remote build.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)][string]$FunctionAppName
    )

    $repoRoot = Join-Path $PSScriptRoot '..'
    uv run --quiet (Join-Path $repoRoot 'scripts' 'sync_shared.py') | Out-Null
    Push-Location (Join-Path $repoRoot 'src' 'functions')
    try {
        Write-Host "📦 Publishing collector to $FunctionAppName..." -ForegroundColor Cyan
        func azure functionapp publish $FunctionAppName --python --build remote
        if ($LASTEXITCODE -ne 0) { throw 'func azure functionapp publish failed.' }
    }
    finally {
        Pop-Location
    }
}

#endregion Functions

#region Main Execution

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $envValues = Get-AzdEnvValues

        if (-not $SkipFunctions) {
            Publish-Collector -FunctionAppName $envValues['FUNCTION_APP_NAME']
        }

        if (-not $SkipAgentRoles) {
            # Only the runtime instance identity can hold role assignments (the blueprint principal cannot).
            $show = azd ai agent show --output json 2>$null
            $principals = @()
            if ($LASTEXITCODE -eq 0 -and $show) {
                $principalId = ($show | ConvertFrom-Json).instance_identity.principal_id
                if ($principalId) { $principals = @($principalId) }
            }
            if ($envValues['AGENT_PRINCIPAL_IDS']) {
                $principals = @($principals + ($envValues['AGENT_PRINCIPAL_IDS'] -split ',')) | Where-Object { $_ } | Select-Object -Unique
            }
            if (-not $principals) {
                Write-Host '⚠️  Agent identity not found in `azd ai agent show`. Set AGENT_PRINCIPAL_IDS (comma separated) and re-run.' -ForegroundColor Yellow
            }
            foreach ($principal in $principals) {
                Add-AgentRoleAssignments -PrincipalId $principal -EnvValues $envValues
            }
        }
        exit 0
    }
    catch {
        Write-Error -ErrorAction Continue "postdeploy failed: $($_.Exception.Message)"
        exit 1
    }
}

#endregion Main Execution
