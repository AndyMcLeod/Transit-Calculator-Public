<#
    VENDORED from the companion fuel planner: tools\export_pdf.ps1
    A copy, not a shared file, for the same reason as currents.py and
    docx_style.py: that planner is a separate repository. Diff before assuming a
    change made there applies here. Copied 2026-08-14.
#>

<#
.SYNOPSIS
    Export the generated .docx documents to PDF through Word.

.DESCRIPTION
    The document builders produce .docx and nothing else. The PDFs beside them
    were exported BY HAND, which is exactly the arrangement that lets a PDF go
    stale against the .docx it is named after - and it did: for a day the
    committed PDFs described a document that had been rebuilt without them.

    This closes that. Word is the only thing that renders these faithfully (the
    TOC field, the table styles), so it is driven the same way tools/bake_toc.ps1
    already drives it.

    With no -Path it exports every .docx in docs\ that HAS a PDF beside it or is
    one of the four known documents, so a stray working file is not published by
    accident.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\export_pdf.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\export_pdf.ps1 -Path docs\the vessel8_Fuel_Methods.docx

.NOTES
    Needs Word installed. It is checked for rather than assumed, because the
    failure without it is a COM error that says nothing useful.

    Verifies each PDF by reading it back - page count and file size - rather
    than trusting the export call. A zero-page PDF saves happily.
#>
[CmdletBinding()]
param(
    # Specific .docx files. Default: the generated documents in docs\.
    [string[]] $Path
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$docs = Join-Path $root 'docs'

if (-not $Path) {
    $Path = Get-ChildItem -LiteralPath $docs -Filter '*.docx' |
            Where-Object { $_.Name -notlike '~$*' } |
            ForEach-Object { $_.FullName }
}
if (-not $Path) { throw "no .docx found in $docs" }

# Resolve to absolute: Word's SaveAs resolves a relative path against ITS OWN
# working directory, not the shell's, and silently writes somewhere else.
$Path = $Path | ForEach-Object { (Resolve-Path -LiteralPath $_).Path }

try {
    $word = New-Object -ComObject Word.Application
} catch {
    throw ("Word is not available on this machine, so the PDFs cannot be " +
           "exported here. Build the .docx anyway and export them where Word " +
           "is: $($_.Exception.Message)")
}
$word.Visible = $false
$word.DisplayAlerts = 0

$wdExportFormatPDF = 17
$wdExportOptimizeForPrint = 0
$wdExportAllDocument = 0
$wdExportDocumentWithMarkup = 7   # ...ContentOnly is 0; markup is NOT wanted
$wdExportCreateHeadingBookmarks = 1

$done = @()
try {
    foreach ($docx in $Path) {
        $pdf = [IO.Path]::ChangeExtension($docx, '.pdf')
        $doc = $word.Documents.Open($docx, $false, $true)   # ReadOnly
        try {
            # Refresh any TOC field before export, or the contents page in the
            # PDF is whatever Word last cached - the same trap bake_toc.ps1
            # exists for.
            foreach ($toc in $doc.TablesOfContents) { $toc.Update() | Out-Null }
            $doc.ExportAsFixedFormat(
                $pdf, $wdExportFormatPDF, $false, $wdExportOptimizeForPrint,
                $wdExportAllDocument, 1, 1, 0, $true, $true,
                $wdExportCreateHeadingBookmarks)
            $pages = $doc.ComputeStatistics(2)              # wdStatisticPages
        } finally {
            $doc.Close($false)
        }
        if (-not (Test-Path -LiteralPath $pdf)) {
            throw "Word reported success but $pdf is not there."
        }
        $size = (Get-Item -LiteralPath $pdf).Length
        if ($size -lt 20000) {
            throw "$pdf is only $size bytes - that is not a rendered document."
        }
        $done += [pscustomobject]@{
            Document = Split-Path $pdf -Leaf
            Pages    = $pages
            KB       = [int]($size / 1KB)
        }
    }
} finally {
    $word.Quit()
    [Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}

$done | Format-Table -AutoSize
Write-Host ("Exported {0} PDF(s) alongside their .docx." -f $done.Count)
Write-Host '  Rebuild a document and run this again, or the PDF goes stale.'
