# Open-MOTION Bloodflow Application

Python Application UI for OPEN Motion Bloodflow monitoring 

![App Image](assets/images/screenshot.png)

## Installation

### Prerequisites
- **Python 3.9 or later**: Make sure you have Python 3.9 or later installed on your system. You can download it from the [official Python website](https://www.python.org/downloads/).


### Build Package
```
powershell -ExecutionPolicy Bypass -File build_and_zip.ps1 -OpenFolder
```

# from repo root
Remove-Item -Recurse -Force build, dist
python -m PyInstaller -y openwater.spec

## Antivirus Note
Some antivirus software may block the Open-Motion application from running such as Microsoft Defender or Smart App Control on Windows 11. Users may need to disable parts of their antivirus software if this prevents them from using the Open-Motion application.

[Here is a guide that will help you turn Smart App Control on Win11 off.](https://www.ninjaone.com/blog/how-to-turn-off-smart-app-control-in-windows-11/)

