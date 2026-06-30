# ScratchSVGCompat

ScratchでサポートされていないSVG要素を修正し、Scratch互換のSVGへ変換します。

Converts SVG files into a Scratch-compatible format by fixing unsupported SVG features.

---

## 日本語

ScratchSVGCompat は、Scratchで正常に読み込めないSVGを、Scratchと互換性のあるSVGへ変換するツールです。

出力されるファイルも通常のSVGです。Scratchでサポートされていない要素や属性を、互換性のある形式へ変換します。

### Inkscapeについて（重要）

本ツールは **事前にInkscapeをインストールしておくことを推奨**します。

* Inkscapeがインストールされていない状態でプログラムを実行した場合、自動的にInkscapeのインストーラーのダウンロードを試みます。
* この自動インストールを行うには**管理者権限**でプログラムを実行する必要があります。
* 管理者権限で実行していない場合は、自動インストールは行われず、実行ファイルと同じフォルダに以下のMSIファイルがダウンロードされます。

```
inkscape-1.4.2_2025-05-13_f4327f4-x64.msi
```

その場合は、このMSIファイルを手動で実行してInkscapeをインストールしてください。

> **事前にInkscapeがインストールされている場合、この手順は不要です。**
>
> また、インストール済みのバージョンが同一である必要はありません。ただし、異なるバージョンでの互換性は保証していません。

なお、**Inkscapeがインストールされていなくても本ツールは使用できます。** ただし、一部の変換処理ではInkscapeを利用したほうが変換精度が向上するため、インストールされていない場合は変換精度が低下する可能性があります。

一方で、**Inkscapeがインストールされていないことが原因で、Scratchとの互換性が失われることはありません。**

---

## English

ScratchSVGCompat is a tool that converts SVG files into a format compatible with Scratch.

The output is still a standard SVG file. Unsupported SVG elements and attributes are converted into compatible alternatives so that Scratch can import the file correctly.

### About Inkscape (Important)

Installing **Inkscape before using this tool is recommended**.

* If Inkscape is not installed, the program will automatically attempt to download the Inkscape installer when it is launched.
* Automatic installation requires the program to be run with **administrator privileges**.
* If the program is not run as an administrator, automatic installation cannot be performed. Instead, the following MSI installer will be downloaded into the same folder as the executable:

```
inkscape-1.4.2_2025-05-13_f4327f4-x64.msi
```

In that case, simply run the MSI file manually to install Inkscape.

> **If Inkscape is already installed, these steps are unnecessary.**
>
> The installed version does not have to match the version above, although compatibility with other versions cannot be guaranteed.

The tool **can still be used without Inkscape**. However, some conversion processes achieve better accuracy when Inkscape is available, so conversion quality may be reduced if it is not installed.

Even without Inkscape, **the generated SVG will not lose Scratch compatibility simply because Inkscape is missing**.
