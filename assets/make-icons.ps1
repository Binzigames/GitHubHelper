# Regenerates icon.png / icon.ico from the logo (same shape as icon.svg). Run: powershell -File assets\make-icons.ps1
Add-Type -AssemblyName System.Drawing
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function New-Icon([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.PixelOffsetMode = 'HighQuality'; $g.Clear([System.Drawing.Color]::Transparent)
    $k = $size / 64.0
    # rounded square with the accent gradient
    $r = 16 * $k * 2
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $r, $r, 180, 90); $path.AddArc($size - $r, 0, $r, $r, 270, 90)
    $path.AddArc($size - $r, $size - $r, $r, $r, 0, 90); $path.AddArc(0, $size - $r, $r, $r, 90, 90); $path.CloseFigure()
    $rect = New-Object System.Drawing.RectangleF 0, 0, $size, $size
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, ([System.Drawing.Color]::FromArgb(76, 141, 255)), ([System.Drawing.Color]::FromArgb(139, 92, 246)), 45
    $g.FillPath($brush, $path)
    # branch glyph
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), (4 * $k)
    $pen.StartCap = 'Round'; $pen.EndCap = 'Round'
    foreach ($c in @(@(22, 18), @(22, 46), @(44, 26))) { $g.DrawEllipse($pen, ($c[0] - 5) * $k, ($c[1] - 5) * $k, 10 * $k, 10 * $k) }
    $g.DrawLine($pen, 22 * $k, 23 * $k, 22 * $k, 41 * $k)
    $g.DrawBezier($pen, (44 * $k), (31 * $k), (44 * $k), (39 * $k), (36 * $k), (39 * $k), (22 * $k), (41 * $k))
    $g.Dispose()
    return $bmp
}
function To-PngBytes($bmp) { $ms = New-Object System.IO.MemoryStream; $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); return ,$ms.ToArray() }

$big = New-Icon 256
$big.Save((Join-Path $here 'icon.png'), [System.Drawing.Imaging.ImageFormat]::Png)

# .ico with PNG-compressed frames
$sizes = 16, 24, 32, 48, 64, 128, 256
$frames = $sizes | ForEach-Object { , (To-PngBytes (New-Icon $_)) }
$fs = [System.IO.File]::Create((Join-Path $here 'icon.ico'))
$w = New-Object System.IO.BinaryWriter $fs
$w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]; $b = [byte]$(if ($s -ge 256) { 0 } else { $s })
    $w.Write($b); $w.Write($b); $w.Write([byte]0); $w.Write([byte]0); $w.Write([uint16]1); $w.Write([uint16]32)
    $w.Write([uint32]$frames[$i].Length); $w.Write([uint32]$offset); $offset += $frames[$i].Length
}
foreach ($f in $frames) { $w.Write($f) }
$w.Close(); $fs.Close()
