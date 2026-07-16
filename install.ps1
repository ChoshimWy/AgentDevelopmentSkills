[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArguments
)

$ErrorActionPreference = "Stop"

function Resolve-AgentSkillsPython {
    if ($env:AGENT_SKILLS_PYTHON) {
        return @($env:AGENT_SKILLS_PYTHON)
    }

    $candidates = @(
        [pscustomobject]@{ Executable = "py"; Prefix = @("-3") },
        [pscustomobject]@{ Executable = "python"; Prefix = @() },
        [pscustomobject]@{ Executable = "python3"; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $executable = Get-Command $candidate.Executable -ErrorAction SilentlyContinue
        if (-not $executable) {
            continue
        }
        $prefix = @($candidate.Prefix)
        & $executable.Source @prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @($executable.Source) + $prefix
        }
    }
    throw "AgentDevelopmentSkills requires Python 3.11+. Install it or set AGENT_SKILLS_PYTHON."
}

function Invoke-AgentSkillsPython {
    param(
        [string[]]$PythonCommand,
        [string]$Script,
        [string[]]$Arguments
    )
    $executable = $PythonCommand[0]
    $prefix = @()
    if ($PythonCommand.Count -gt 1) {
        $prefix = $PythonCommand[1..($PythonCommand.Count - 1)]
    }
    & $executable @prefix $Script @Arguments
    $script:AgentSkillsPythonExitCode = [int]$LASTEXITCODE
}

function Invoke-AgentSkillsHttpsDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][long]$MaximumBytes
    )
    $current = [uri]$Uri
    if ($current.Scheme -ne "https") {
        throw "Download URLs must use HTTPS."
    }
    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    try {
        for ($redirects = 0; $redirects -le 10; $redirects++) {
            $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $current)
            $request.Headers.UserAgent.ParseAdd("agent-development-skills-bootstrap/1.0")
            $response = $client.SendAsync(
                $request,
                [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
            $request.Dispose()
            $status = [int]$response.StatusCode
            if ($status -ge 300 -and $status -lt 400) {
                $location = $response.Headers.Location
                $response.Dispose()
                if ($null -eq $location) {
                    throw "HTTPS redirect is missing a Location header."
                }
                $current = if ($location.IsAbsoluteUri) { $location } else { [uri]::new($current, $location) }
                if ($current.Scheme -ne "https") {
                    throw "Download redirected to a non-HTTPS URL."
                }
                continue
            }
            $response.EnsureSuccessStatusCode()
            $declared = $response.Content.Headers.ContentLength
            if ($null -ne $declared -and $declared -gt $MaximumBytes) {
                $response.Dispose()
                throw "Download exceeds the configured size limit."
            }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [System.IO.File]::Open(
                $Destination,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            try {
                $buffer = New-Object byte[] 65536
                [long]$total = 0
                while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $count
                    if ($total -gt $MaximumBytes) {
                        throw "Download exceeds the configured size limit."
                    }
                    $outputStream.Write($buffer, 0, $count)
                }
            } finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
                $response.Dispose()
            }
            return $current.AbsoluteUri
        }
        throw "Download exceeded the redirect limit."
    } catch {
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        throw
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-AgentSkillsAssetBaseUrl {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $base = [string]$manifest.asset_base_url
    $uri = [uri]$base
    if (-not $base.EndsWith("/") -or $uri.Scheme -ne "https") {
        throw "release manifest asset_base_url must use HTTPS and end with '/'."
    }
    return $uri.AbsoluteUri
}

function Assert-AgentSkillsBootstrap {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$BootstrapPath
    )
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $matches = @($manifest.bootstrap_assets | Where-Object { $_.filename -eq "bootstrap_install.py" })
    if ($matches.Count -ne 1) {
        throw "release manifest must declare exactly one bootstrap_install.py asset."
    }
    $asset = $matches[0]
    $propertyNames = @($asset.PSObject.Properties.Name | Sort-Object)
    if (($propertyNames -join ",") -ne "filename,sha256,size") {
        throw "release manifest bootstrap asset fields are invalid."
    }
    if ($asset.size -is [bool]) {
        throw "release manifest bootstrap size is invalid."
    }
    try { [long]$expectedSize = $asset.size } catch { throw "release manifest bootstrap size is invalid." }
    if ($expectedSize -le 0 -or $expectedSize -gt 1MB -or [double]$asset.size -ne [double]$expectedSize) {
        throw "release manifest bootstrap size is invalid."
    }
    if ([string]$asset.sha256 -notmatch "^[0-9a-f]{64}$") {
        throw "release manifest bootstrap sha256 is invalid."
    }
    $actual = Get-Item -LiteralPath $BootstrapPath
    $digest = (Get-FileHash -LiteralPath $BootstrapPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual.Length -ne $expectedSize -or $digest -ne [string]$asset.sha256) {
        throw "downloaded bootstrap does not match release manifest."
    }
}

try {
    $python = Resolve-AgentSkillsPython
    $localInstaller = if ($PSScriptRoot) { Join-Path $PSScriptRoot "scripts/install_local.py" } else { $null }
    if ($localInstaller -and (Test-Path -LiteralPath $localInstaller -PathType Leaf)) {
        Invoke-AgentSkillsPython -PythonCommand $python -Script $localInstaller -Arguments $InstallerArguments
        exit [int]$script:AgentSkillsPythonExitCode
    }
    $releaseBaseUrl = if ($env:AGENT_SKILLS_RELEASE_BASE_URL) {
        $env:AGENT_SKILLS_RELEASE_BASE_URL.TrimEnd("/")
    } else {
        "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/latest/download"
    }
    $manifestUrl = if ($env:AGENT_SKILLS_RELEASE_MANIFEST_URL) {
        $env:AGENT_SKILLS_RELEASE_MANIFEST_URL
    } else {
        "$releaseBaseUrl/release-manifest.json"
    }
    if (([uri]$manifestUrl).Scheme -ne "https") {
        throw "AGENT_SKILLS_RELEASE_MANIFEST_URL must use HTTPS."
    }
    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("agent-skills-bootstrap-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    try {
        $manifest = Join-Path $temporaryRoot "release-manifest.json"
        $bootstrap = Join-Path $temporaryRoot "bootstrap_install.py"
        $null = Invoke-AgentSkillsHttpsDownload -Uri $manifestUrl -Destination $manifest -MaximumBytes 1MB
        $assetBaseUrl = Get-AgentSkillsAssetBaseUrl -ManifestPath $manifest
        $null = Invoke-AgentSkillsHttpsDownload -Uri ($assetBaseUrl + "bootstrap_install.py") -Destination $bootstrap -MaximumBytes 1MB
        Assert-AgentSkillsBootstrap -ManifestPath $manifest -BootstrapPath $bootstrap
        Invoke-AgentSkillsPython -PythonCommand $python -Script $bootstrap -Arguments (@(
            "--manifest-file", $manifest,
            "--artifact-base-url", $assetBaseUrl
        ) + $InstallerArguments)
    } finally {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit [int]$script:AgentSkillsPythonExitCode
} catch {
    Write-Error ("AgentDevelopmentSkills bootstrap blocked: " + $_.Exception.Message)
    exit 2
}
