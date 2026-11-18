#!/usr/bin/env pwsh
# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Deploys the supporting resources and configures Foundry IQ after `azd provision`.
.DESCRIPTION
    The Foundry account/project and model deployments are created by azd (microsoft.foundry provider).
    This hook then:
      1. Deploys support-infra/main.bicep (serverless Foundry IQ, Blob, Cosmos DB, Functions, RBAC) into the same resource group.
      2. Writes the Bicep outputs into the azd environment so the Hosted Agent picks them up at deploy time.
      3. Creates/updates the Foundry IQ knowledge source and knowledge base (scripts/setup_foundry_iq.py).
      4. Seeds the collection target list in Cosmos DB when it is empty (scripts/seed_sources.py).
.PARAMETER SupportLocation
    Region for the supporting resources. Defaults to SUPPORT_LOCATION from the azd env or japaneast.
.PARAMETER SkipFoundryIq
    Skip the Foundry IQ knowledge source / knowledge base setup.
.EXAMPLE
    ./scripts/postprovision.ps1
.NOTES
    Runs via: azd provision (postprovision hook in azure.yaml)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$SupportLocation = '',

    [Parameter(Mandatory = $false)]
    [switch]$SkipFoundryIq
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'AzdEnv.psm1') -Force

#region Functions

function Get-AiAccountName {
    <#
    .SYNOPSIS
        Resolves the Foundry (AI Services) account name created by azd.
    .PARAMETER EnvValues
        azd environment values.
    .OUTPUTS
        [string] Account name.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$EnvValues
    )

    if ($EnvValues['AZURE_AI_ACCOUNT_NAME']) {
        return $EnvValues['AZURE_AI_ACCOUNT_NAME']
    }
    if ($EnvValues['AZURE_AI_PROJECT_ID'] -match '/accounts/([^/]+)/projects/') {
        return $Matches[1]
    }
    $accounts = az cognitiveservices account list --resource-group $EnvValues['AZURE_RESOURCE_GROUP'] --query "[?kind=='AIServices'].name" -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $accounts) {
        throw 'Could not resolve the Foundry account name in the resource group.'
    }
    return @($accounts)[0]
}

function Invoke-SupportDeployment {
    <#
    .SYNOPSIS
        Deploys support-infra/main.bicep and returns its outputs.
    .OUTPUTS
        [hashtable] Output name -> value.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param(
        [Parameter(Mandatory = $true)][string]$ResourceGroup,
        [Parameter(Mandatory = $true)][string]$Location,
        [Parameter(Mandatory = $true)][string]$AiAccountName,
        [Parameter(Mandatory = $false)][string]$UserPrincipalId = ''
    )

    $template = Join-Path $PSScriptRoot '..' 'support-infra' 'main.bicep'
    Write-Host "🚀 Deploying supporting resources to $ResourceGroup ($Location)..." -ForegroundColor Cyan
    $json = az deployment group create `
        --resource-group $ResourceGroup `
        --name 'personal-agent-support' `
        --template-file $template `
        --parameters location=$Location aiAccountName=$AiAccountName userPrincipalId=$UserPrincipalId `
        --query 'properties.outputs' -o json
    if ($LASTEXITCODE -ne 0) {
        throw 'Supporting resource deployment failed.'
    }
    $outputs = @{}
    # ARM camel-cases output names (SEARCH_ENDPOINT -> searcH_ENDPOINT); azd ${VAR} lookups are case-sensitive.
    ($json | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $outputs[$_.Name.ToUpperInvariant()] = [string]$_.Value.value }
    return $outputs
}

#endregion Functions

#region Main Execution

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $envValues = Get-AzdEnvValues
        $resourceGroup = $envValues['AZURE_RESOURCE_GROUP']
        if (-not $resourceGroup) { throw 'AZURE_RESOURCE_GROUP is not set in the azd environment.' }

        $location = if ($SupportLocation) { $SupportLocation } elseif ($envValues['SUPPORT_LOCATION']) { $envValues['SUPPORT_LOCATION'] } else { 'japaneast' }
        $accountName = Get-AiAccountName -EnvValues $envValues
        $userId = az ad signed-in-user show --query id -o tsv 2>$null
        if ($LASTEXITCODE -ne 0) { $userId = '' }

        $outputs = Invoke-SupportDeployment -ResourceGroup $resourceGroup -Location $location -AiAccountName $accountName -UserPrincipalId $userId
        $outputs['AZURE_AI_ACCOUNT_NAME'] = $accountName
        $outputs['KNOWLEDGE_SOURCE_NAME'] = 'content-blob-ks'
        $outputs['KNOWLEDGE_BASE_NAME'] = 'personal-kb'
        foreach ($key in @('TOPIC_KNOWLEDGE_SOURCE_NAME', 'TOPIC_KNOWLEDGE_BASE_NAME')) {
            # Empty until the topic index is prepared (see docs/topic-graph.md).
            if (-not $envValues.ContainsKey($key)) { $outputs[$key] = '' }
        }
        Set-AzdEnvValues -Values $outputs
        Write-Host '✅ Supporting resources deployed and azd env updated.' -ForegroundColor Green

        $merged = Get-AzdEnvValues
        foreach ($entry in $merged.GetEnumerator()) { Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value }

        $repoRoot = Join-Path $PSScriptRoot '..'
        if (-not $SkipFoundryIq) {
            Write-Host '🔎 Configuring Foundry IQ knowledge source / knowledge base...' -ForegroundColor Cyan
            uv run --quiet (Join-Path $repoRoot 'scripts' 'setup_foundry_iq.py')
            if ($LASTEXITCODE -ne 0) {
                # RBAC propagation can take a few minutes on a fresh deployment.
                Write-Host '⚠️  Foundry IQ setup failed. Re-run later: uv run scripts/setup_foundry_iq.py' -ForegroundColor Yellow
            }
        }

        Write-Host '🌱 Seeding collection targets (only when empty)...' -ForegroundColor Cyan
        uv run --quiet (Join-Path $repoRoot 'scripts' 'seed_sources.py') --if-empty
        if ($LASTEXITCODE -ne 0) {
            Write-Host '⚠️  Seeding failed. Re-run later: uv run scripts/seed_sources.py --if-empty' -ForegroundColor Yellow
        }
        exit 0
    }
    catch {
        Write-Error -ErrorAction Continue "postprovision failed: $($_.Exception.Message)"
        exit 1
    }
}

#endregion Main Execution
