# Regenerates the app icons from the logo (same shape as icon.svg):
#   icon.png (512), icon-128.png (used as favicon), icon.ico (16..256, crisp per-size frames)
# Every size is drawn at 4x and downsampled with a high-quality filter, so edges stay clean.
# Run: powershell -File assets\make-icons.ps1
Add-Type -AssemblyName System.Drawing
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Draw-Logo([int]$px, [double]$stroke) {
    $bmp = New-Object System.Drawing.Bitmap $px, $px, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.PixelOffsetMode = 'HighQuality'; $g.Clear([System.Drawing.Color]::Transparent)
    $k = $px / 64.0
    $r = 28 * $k      # corner diameter (rx = 14 of 64)
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $r, $r, 180, 90); $path.AddArc($px - $r, 0, $r, $r, 270, 90)
    $path.AddArc($px - $r, $px - $r, $r, $r, 0, 90); $path.AddArc(0, $px - $r, $r, $r, 90, 90); $path.CloseFigure()
    $rect = New-Object System.Drawing.RectangleF 0, 0, $px, $px
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, ([System.Drawing.Color]::FromArgb(86, 150, 255)), ([System.Drawing.Color]::FromArgb(136, 84, 246)), 55
    $g.FillPath($brush, $path)
    # soft top highlight
    $hl = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, ([System.Drawing.Color]::FromArgb(60, 255, 255, 255)), ([System.Drawing.Color]::FromArgb(0, 255, 255, 255)), 90
    $g.FillPath($hl, $path)
    # branch glyph
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), ($stroke * $k)
    $pen.StartCap = 'Round'; $pen.EndCap = 'Round'; $pen.LineJoin = 'Round'
    foreach ($c in @(@(22, 18), @(22, 46), @(44, 26))) { $g.DrawEllipse($pen, ($c[0] - 5) * $k, ($c[1] - 5) * $k, 10 * $k, 10 * $k) }
    $g.DrawLine($pen, 22 * $k, 23 * $k, 22 * $k, 41 * $k)
    $g.DrawBezier($pen, (44 * $k), (31 * $k), (44 * $k), (39 * $k), (36 * $k), (39 * $k), (22 * $k), (41 * $k))
    $g.Dispose()
    return $bmp
}

function New-Icon([int]$size) {
    $stroke = if ($size -le 24) { 6.2 } elseif ($size -le 48) { 5.0 } else { 4.0 }   # bolder strokes for tiny sizes
    $big = Draw-Logo ($size * 4) $stroke
    $out = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($out)
    $g.InterpolationMode = 'HighQualityBicubic'; $g.PixelOffsetMode = 'HighQuality'; $g.SmoothingMode = 'HighQuality'; $g.CompositingQuality = 'HighQuality'
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.DrawImage($big, (New-Object System.Drawing.Rectangle 0, 0, $size, $size))
    $g.Dispose(); $big.Dispose()
    return $out
}
function To-PngBytes($bmp) { $ms = New-Object System.IO.MemoryStream; $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); return ,$ms.ToArray() }

(New-Icon 512).Save((Join-Path $here 'icon.png'), [System.Drawing.Imaging.ImageFormat]::Png)
(New-Icon 128).Save((Join-Path $here 'icon-128.png'), [System.Drawing.Imaging.ImageFormat]::Png)

# .ico with PNG-compressed frames
$sizes = 16, 20, 24, 32, 40, 48, 64, 96, 128, 256
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
