[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArguments
)

$ErrorActionPreference = "Stop"

$RequestedEngine = if ($env:AGENT_SKILLS_INSTALL_ENGINE) {
    $env:AGENT_SKILLS_INSTALL_ENGINE
} else {
    "auto"
}
if ($RequestedEngine -notin @("auto", "python", "rust")) {
    Write-Error `
        "AgentDevelopmentSkills bootstrap blocked: AGENT_SKILLS_INSTALL_ENGINE must be auto, rust, or python" `
        -ErrorAction Continue
    exit 2
}

function Get-AgentSkillsDefaultTarget {
    if ($env:CODEX_HOME) {
        return $env:CODEX_HOME
    }
    $homeRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if (-not $homeRoot) {
        return $null
    }
    return Join-Path $homeRoot ".codex"
}

function Test-AgentSkillsIdentifier {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value -match "^[a-z0-9][a-z0-9-]*$"
}

function Test-AgentSkillsFreshTarget {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    try {
        $target = Get-Item -LiteralPath $TargetRoot -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $true
    } catch {
        return $false
    }
    if (-not $target.PSIsContainer -or
        ($target.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        return $false
    }
    foreach ($name in @("AGENTS.md", "skills", ".agent-skills")) {
        try {
            $null = Get-Item -LiteralPath (Join-Path $TargetRoot $name) -Force -ErrorAction Stop
            return $false
        } catch [System.Management.Automation.ItemNotFoundException] {
            continue
        } catch {
            return $false
        }
    }
    return $true
}

function Test-AgentSkillsNativeRequest {
    param([string[]]$Arguments)

    $script:AgentSkillsNativeTarget = Get-AgentSkillsDefaultTarget
    $script:AgentSkillsNativePlatforms = [System.Collections.Generic.List[string]]::new()
    $script:AgentSkillsNativeDisciplines = [System.Collections.Generic.List[string]]::new()
    $script:AgentSkillsNativeRuntimeConfigs = [System.Collections.Generic.List[string]]::new()
    $script:AgentSkillsNativeDryRun = $false
    $script:AgentSkillsNativeJson = $false
    $targetSeen = $false
    $platformKeys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $disciplineKeys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $runtimeKeys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)

    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        if ($argument -eq "--dry-run") {
            if ($script:AgentSkillsNativeDryRun) { return $false }
            $script:AgentSkillsNativeDryRun = $true
            continue
        }
        if ($argument -eq "--json") {
            if ($script:AgentSkillsNativeJson) { return $false }
            $script:AgentSkillsNativeJson = $true
            continue
        }

        $name = $argument
        $value = $null
        if ($argument -match "^(--target-root|--platform|--discipline|--runtime-config)=(.*)$") {
            $name = $Matches[1]
            $value = $Matches[2]
        } elseif ($argument -in @("--target-root", "--platform", "--discipline", "--runtime-config")) {
            $index++
            if ($index -ge $Arguments.Count) { return $false }
            $value = $Arguments[$index]
        } else {
            return $false
        }
        if ([string]::IsNullOrEmpty($value)) { return $false }

        switch ($name) {
            "--target-root" {
                if ($targetSeen) { return $false }
                $targetSeen = $true
                $script:AgentSkillsNativeTarget = $value
            }
            "--platform" {
                if ($value -notin @("apple", "desktop", "all")) { return $false }
                if (-not $platformKeys.Add($value)) { return $false }
                if (($value -eq "all" -and $platformKeys.Count -ne 1) -or
                    ($value -ne "all" -and $platformKeys.Contains("all"))) {
                    return $false
                }
                $script:AgentSkillsNativePlatforms.Add($value)
            }
            "--discipline" {
                if (-not (Test-AgentSkillsIdentifier -Value $value) -or -not $disciplineKeys.Add($value)) {
                    return $false
                }
                $script:AgentSkillsNativeDisciplines.Add($value)
            }
            "--runtime-config" {
                if (-not (Test-AgentSkillsIdentifier -Value $value) -or -not $runtimeKeys.Add($value)) {
                    return $false
                }
                $script:AgentSkillsNativeRuntimeConfigs.Add($value)
            }
        }
    }

    if ($script:AgentSkillsNativePlatforms.Count -ne 1 -or
        $script:AgentSkillsNativePlatforms[0] -ne "desktop") {
        return $false
    }
    if ([string]::IsNullOrEmpty($script:AgentSkillsNativeTarget) -or
        $script:AgentSkillsNativeTarget.StartsWith("~", [StringComparison]::Ordinal)) {
        return $false
    }
    return Test-AgentSkillsFreshTarget -TargetRoot $script:AgentSkillsNativeTarget
}

function Invoke-AgentSkillsSourceNativeInstall {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)

    $cargo = Get-Command cargo -ErrorAction SilentlyContinue
    if (-not $cargo) {
        throw "native source install requires Cargo"
    }
    $source = Get-Item -LiteralPath $SourceRoot -Force -ErrorAction SilentlyContinue
    if ($null -eq $source -or -not $source.PSIsContainer -or
        ($source.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "native source install root is missing or unsafe"
    }
    foreach ($relative in @(
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "crates/agent-skills/Cargo.toml"
    )) {
        $path = Join-Path $SourceRoot $relative
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if ($null -eq $item -or $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "native source install input is missing or unsafe: $relative"
        }
    }

    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("agent-skills-source-native-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    try {
        $targetDirectory = Join-Path $temporaryRoot "target"
        & $cargo.Source build `
            --locked `
            --offline `
            --manifest-path (Join-Path $SourceRoot "Cargo.toml") `
            --package agent-skills-rs `
            --bin agent-skills-rs `
            --target-dir $targetDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "native source installer build failed with exit code $LASTEXITCODE"
        }
        $nativeExecutable = Join-Path $targetDirectory "debug/agent-skills-rs.exe"
        $native = Get-Item -LiteralPath $nativeExecutable -Force -ErrorAction SilentlyContinue
        if ($null -eq $native -or $native.PSIsContainer -or
            ($native.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Cargo did not produce the expected native installer"
        }

        $nativeArguments = [System.Collections.Generic.List[string]]::new()
        $nativeArguments.Add("install")
        $nativeArguments.Add("--source-root")
        $nativeArguments.Add($SourceRoot)
        $nativeArguments.Add("--target-root")
        $nativeArguments.Add($script:AgentSkillsNativeTarget)
        foreach ($platform in $script:AgentSkillsNativePlatforms) {
            $nativeArguments.Add("--platform")
            $nativeArguments.Add($platform)
        }
        foreach ($discipline in $script:AgentSkillsNativeDisciplines) {
            $nativeArguments.Add("--discipline")
            $nativeArguments.Add($discipline)
        }
        foreach ($runtimeConfig in $script:AgentSkillsNativeRuntimeConfigs) {
            $nativeArguments.Add("--runtime-config")
            $nativeArguments.Add($runtimeConfig)
        }
        if ($script:AgentSkillsNativePlatforms.Contains("apple") -or
            $script:AgentSkillsNativePlatforms.Contains("all")) {
            $nativeArguments.Add("--session-launcher")
            $nativeArguments.Add($nativeExecutable)
        }
        if ($script:AgentSkillsNativeDryRun) { $nativeArguments.Add("--dry-run") }
        if ($script:AgentSkillsNativeJson) { $nativeArguments.Add("--json") }

        $selectedEngine = Get-Item `
            -LiteralPath Env:AGENT_SKILLS_INSTALL_ENGINE_SELECTED `
            -ErrorAction SilentlyContinue
        try {
            $env:AGENT_SKILLS_INSTALL_ENGINE_SELECTED = "rust"
            & $nativeExecutable @nativeArguments
            $script:AgentSkillsNativeExitCode = [int]$LASTEXITCODE
        } finally {
            if ($null -ne $selectedEngine) {
                $env:AGENT_SKILLS_INSTALL_ENGINE_SELECTED = $selectedEngine.Value
            } else {
                Remove-Item `
                    -LiteralPath Env:AGENT_SKILLS_INSTALL_ENGINE_SELECTED `
                    -ErrorAction SilentlyContinue
            }
        }
    } finally {
        try {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning ("native source installer temporary directory requires cleanup: " + $temporaryRoot)
        }
    }
}

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
    $localInstaller = if ($PSScriptRoot) { Join-Path $PSScriptRoot "scripts/install_local.py" } else { $null }
    if ($localInstaller -and (Test-Path -LiteralPath $localInstaller -PathType Leaf)) {
        $sourceRoot = (Get-Item -LiteralPath $PSScriptRoot -Force).FullName
        $nativeEligible = Test-AgentSkillsNativeRequest -Arguments $InstallerArguments
        if ($RequestedEngine -ne "python" -and $nativeEligible -and
            (Get-Command cargo -ErrorAction SilentlyContinue)) {
            Invoke-AgentSkillsSourceNativeInstall -SourceRoot $sourceRoot
            exit [int]$script:AgentSkillsNativeExitCode
        }
        if ($RequestedEngine -eq "rust") {
            throw "forced Rust source install requires Cargo, a supported explicit platform, and no compatibility-only arguments"
        }
        $python = Resolve-AgentSkillsPython
        Invoke-AgentSkillsPython -PythonCommand $python -Script $localInstaller -Arguments $InstallerArguments
        exit [int]$script:AgentSkillsPythonExitCode
    }
    if ($RequestedEngine -eq "rust") {
        throw "forced Rust hosted install is not enabled by the PowerShell bootstrap yet"
    }
    $python = Resolve-AgentSkillsPython
    $releaseBaseUrl = if ($env:AGENT_SKILLS_RELEASE_BASE_URL) {
        $env:AGENT_SKILLS_RELEASE_BASE_URL.TrimEnd("/")
    } else {
        "https://choshimwy.github.io/AgentDevelopmentSkills"
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
    Write-Error `
        ("AgentDevelopmentSkills bootstrap blocked: " + $_.Exception.Message) `
        -ErrorAction Continue
    exit 2
}
