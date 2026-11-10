$ws = New-Object -ComObject WScript.Shell
$projectRoot = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$lnk = $ws.CreateShortcut($startup + '\Ghost-Data Suite.lnk')
$lnk.TargetPath = Join-Path $PSScriptRoot 'startup-launcher.bat'
$lnk.WorkingDirectory = $projectRoot
$lnk.Save()
Write-Host 'Startup shortcut created at' $lnk.FullName
