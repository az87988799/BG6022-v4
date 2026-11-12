#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskMarker = 'ORCA Agent local web launcher'
$taskErrorCode = 'installation_failed'
$taskErrorMessage = 'The local web shortcut operation failed.'
$taskServiceEnsured = $false
$taskTemporaryFiles = [System.Collections.Generic.List[string]]::new()
$taskCommitted = [System.Collections.Generic.List[object]]::new()
$taskRollbackComplete = $true

function Stop-TaskOperation {
    param([string]$Code, [string]$Message)
    $script:taskErrorCode = $Code
    $script:taskErrorMessage = $Message
    throw [System.InvalidOperationException]::new($Message)
}

function Resolve-TaskPath {
    param([string]$Path, [bool]$Directory)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        Stop-TaskOperation 'missing_path' 'A required local path is empty.'
    }
    $taskItem = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $taskItem -or $taskItem.PSProvider.Name -ne 'FileSystem' -or
        $taskItem.PSIsContainer -ne $Directory) {
        Stop-TaskOperation 'invalid_path' 'A required local file or directory is missing or has the wrong type.'
    }
    return $taskItem.FullName
}

function Test-TaskShortcutOwner {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $taskItem = Get-Item -LiteralPath $Path -Force
    if ($taskItem.PSIsContainer -or ($taskItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        return $false
    }
    $taskLink = $script:taskShortcutShell.CreateShortcut($Path)
    return ($taskLink.Description -eq $script:taskMarker -and
        [string]::Equals($taskLink.TargetPath, $script:taskPythonw, [StringComparison]::OrdinalIgnoreCase) -and
        $taskLink.Arguments.StartsWith('"' + $script:taskLauncher + '" ', [StringComparison]::OrdinalIgnoreCase))
}

try {
    $ProjectRoot = Resolve-TaskPath $ProjectRoot $true
    $taskLauncher = Join-Path $ProjectRoot 'scripts\desktop_web.py'
    $taskPythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
    $taskPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

    # ASCII source keeps these exact Chinese names portable to Windows PowerShell 5.1.
    $taskTitle = 'ORCA ' + (-join [char[]]@(0x5206, 0x5B50, 0x5DE5, 0x4F5C, 0x53F0))
    $taskBackground = -join [char[]]@(0xFF08, 0x540E, 0x53F0, 0xFF09)
    $taskStartup = Resolve-TaskPath ([Environment]::GetFolderPath('Startup')) $true
    $taskDesktop = Resolve-TaskPath ([Environment]::GetFolderPath('DesktopDirectory')) $true
    $taskLinks = @(
        [pscustomobject]@{ Path = (Join-Path $taskStartup ($taskTitle + $taskBackground + '.lnk')); Mode = '--serve' },
        [pscustomobject]@{ Path = (Join-Path $taskDesktop ($taskTitle + '.lnk')); Mode = '--open' }
    )
    $taskShortcutShell = New-Object -ComObject WScript.Shell

    if ($Uninstall) {
        $taskRemoved = [System.Collections.Generic.List[string]]::new()
        $taskSkipped = [System.Collections.Generic.List[string]]::new()
        foreach ($taskEntry in $taskLinks) {
            if (-not (Test-Path -LiteralPath $taskEntry.Path)) { continue }
            if (Test-TaskShortcutOwner $taskEntry.Path) {
                Remove-Item -LiteralPath $taskEntry.Path -Force
                $taskRemoved.Add($taskEntry.Path)
            } else {
                $taskSkipped.Add($taskEntry.Path)
            }
        }
        [pscustomobject]@{
            status = 'uninstalled'; removed = $taskRemoved.ToArray(); skipped_not_owned = $taskSkipped.ToArray()
            service_untouched = $true; data_untouched = $true
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    $taskLauncher = Resolve-TaskPath $taskLauncher $false
    $taskPythonw = Resolve-TaskPath $taskPythonw $false
    $taskPython = Resolve-TaskPath $taskPython $false
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
        $ConfigPath = Join-Path $ProjectRoot 'config.local.toml'
    } elseif (-not [IO.Path]::IsPathRooted($ConfigPath)) {
        $ConfigPath = Join-Path $ProjectRoot $ConfigPath
    }
    $ConfigPath = Resolve-TaskPath $ConfigPath $false

    foreach ($taskEntry in $taskLinks) {
        if ((Test-Path -LiteralPath $taskEntry.Path) -and -not (Test-TaskShortcutOwner $taskEntry.Path)) {
            Stop-TaskOperation 'shortcut_conflict' 'An existing same-name shortcut does not belong to this project; nothing was overwritten.'
        }
    }

    # Arguments are passed directly, without constructing a shell command.
    # Keep helper output private: only this installer's bounded status is printed.
    $taskErrorCode = 'launcher_ensure_failed'
    $taskErrorMessage = 'The launcher could not confirm a healthy local web service; no shortcuts were installed.'
    $taskPriorErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $taskEnsureOutput = & $taskPython $taskLauncher --ensure --config $ConfigPath --port $Port 2>&1
        $taskEnsureExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $taskPriorErrorPreference
    }
    if ($taskEnsureExitCode -ne 0) {
        Stop-TaskOperation $taskErrorCode $taskErrorMessage
    }
    $taskServiceEnsured = $true
    $taskErrorCode = 'shortcut_install_failed'
    $taskErrorMessage = 'The service was ensured, but the shortcuts could not both be installed.'

    # Stage both links before replacing either user-visible shortcut.
    foreach ($taskEntry in $taskLinks) {
        $taskTemporary = Join-Path (Split-Path -Parent $taskEntry.Path) ('.orca-web-' + [Guid]::NewGuid().ToString('N') + '.lnk')
        $taskTemporaryFiles.Add($taskTemporary)
        $taskShortcut = $taskShortcutShell.CreateShortcut($taskTemporary)
        $taskShortcut.TargetPath = $taskPythonw
        $taskShortcut.Arguments = '"' + $taskLauncher + '" ' + $taskEntry.Mode + ' --config "' + $ConfigPath + '" --port ' + $Port
        $taskShortcut.WorkingDirectory = $ProjectRoot
        $taskShortcut.WindowStyle = 7
        $taskShortcut.Description = $taskMarker
        $taskShortcut.Save()
        $taskEntry | Add-Member -NotePropertyName Temporary -NotePropertyValue $taskTemporary
    }
    foreach ($taskEntry in $taskLinks) {
        $taskOriginal = $null
        if (Test-Path -LiteralPath $taskEntry.Path) {
            if (-not (Test-TaskShortcutOwner $taskEntry.Path)) {
                Stop-TaskOperation 'shortcut_conflict' 'A shortcut changed during installation; the unowned shortcut was preserved.'
            }
            $taskOriginal = [IO.File]::ReadAllBytes($taskEntry.Path)
        }
        $taskWritten = [IO.File]::ReadAllBytes($taskEntry.Temporary)
        Move-Item -LiteralPath $taskEntry.Temporary -Destination $taskEntry.Path -Force
        $taskCommitted.Add([pscustomobject]@{ Path = $taskEntry.Path; Original = $taskOriginal; Written = $taskWritten })
    }

    [pscustomobject]@{
        status = 'installed'; project_root = $ProjectRoot; config_path = $ConfigPath; port = $Port
        shortcuts = @($taskLinks | ForEach-Object { $_.Path }); service_ensured = $true
        launch_scope = 'current-user logon; desktop shortcut opens browser; no automatic Run resume'
    } | ConvertTo-Json -Depth 4
    exit 0
} catch {
    foreach ($taskEntry in $taskCommitted) {
        try {
            # Do not undo a concurrent user replacement.
            $taskCurrent = [IO.File]::ReadAllBytes($taskEntry.Path)
            if ([Convert]::ToBase64String($taskCurrent) -ne [Convert]::ToBase64String($taskEntry.Written)) {
                $taskRollbackComplete = $false
                continue
            }
            if ($null -eq $taskEntry.Original) {
                Remove-Item -LiteralPath $taskEntry.Path -Force
            } else {
                [IO.File]::WriteAllBytes($taskEntry.Path, $taskEntry.Original)
            }
        } catch { $taskRollbackComplete = $false }
    }
    [pscustomobject]@{
        status = 'failed'; error = $taskErrorCode; message = $taskErrorMessage
        service_ensured = $taskServiceEnsured; shortcut_rollback_complete = $taskRollbackComplete
    } | ConvertTo-Json -Depth 3
    exit 1
} finally {
    foreach ($taskTemporary in $taskTemporaryFiles) {
        if (Test-Path -LiteralPath $taskTemporary) {
            Remove-Item -LiteralPath $taskTemporary -Force -ErrorAction SilentlyContinue
        }
    }
}
