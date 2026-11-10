$ws = New-Object -ComObject WScript.Shell
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'launch-dashboard.vbs'
$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Ghost-Data Suite.lnk')
$lnk.TargetPath = $launcher
$lnk.WorkingDirectory = $projectRoot
$lnk.IconLocation = $launcher + ',0'
$lnk.Save()
Write-Host 'Shortcut created at' $lnk.FullName
