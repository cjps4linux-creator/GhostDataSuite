$ws = New-Object -ComObject WScript.Shell
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'launch-ghost.bat'
$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Ghost-Data Suite.lnk')
$lnk.TargetPath = $launcher
$lnk.WorkingDirectory = $projectRoot
$lnk.Description = 'Ghost-Data Suite - Capture, Encrypt, Export'
$lnk.IconLocation = (Join-Path $projectRoot 'dist\ghost-data-suite.exe') + ',0'
$lnk.Save()
Write-Host 'Shortcut created at' $lnk.FullName
