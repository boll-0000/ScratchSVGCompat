# ScratchSVGCompat

ScratchでサポートされていないSVG要素を修正し、Scratch互換のSVGへ変換します。

Converts SVG files into a Scratch-compatible format by fixing unsupported SVG features.

---

## 日本語

ScratchSVGCompat は、Scratchで正常に読み込めないSVGを、Scratchと互換性のあるSVGへ変換するツールです。

出力されるファイルも通常のSVGです。Scratchでサポートされていない要素や属性を、互換性のある形式へ変換します。

### Inkscapeについて

Inkscape は事前にインストールしておくことを推奨します。

未インストールのままプログラムを実行した場合は、自動で Inkscape のインストーラーがダウンロードされます。

管理者権限で実行している場合は自動でインストールされます。管理者権限で実行していない場合は、実行ファイルと同じフォルダに `inkscape-1.4.2_2025-05-13_f4327f4-x64.msi` がダウンロードされるため、そのファイルを手動でインストールしてください。

すでに Inkscape がインストールされている場合、この手順は不要です。また、バージョンは同じである必要はありませんが、互換性は保証できません。

Inkscape がインストールされていなくても本ツールは使用できますが、変換精度が劣る可能性があります。ただし、Inkscape がインストールされていないことが原因で Scratch との互換性が失われることはありません。

なお、原因は不明ですが、環境によっては Inkscape を使用した場合のほうが出力結果がおかしくなることがあります。

### Scratchで表示がおかしい場合

Scratch側で読み込んだときに、stage側と結果が異なり、editor側が正しい場合は、SVGを一度動かしてください。

選択してドラッグするか、選択した状態で上下左右キーを一度押してから元に戻すと修正されることがあります。ですが、ほとんどの場合はこれで修正されます。

逆に、stage側が正しく、editor側がおかしい場合は、不具合修正の参考にしたいため、そのSVGをご提供いただけると幸いです。

#### 不具合報告方法

以下の Scratch プロフィールのコメント欄からご連絡ください。

https://scratch.mit.edu/users/boll-0000/

コメントには、**不具合報告であることを明確に記載**した上で、次のいずれかを添付してください。

* SVGファイル本体
* SVGファイルへのリンク
* そのSVGを使用しているScratch作品へのリンク

ご協力いただきありがとうございます。

---

## English

ScratchSVGCompat is a tool that converts SVG files into a format compatible with Scratch.

The output is still a standard SVG file. Unsupported SVG elements and attributes are converted into compatible alternatives so that Scratch can import the file correctly.

### About Inkscape

Installing Inkscape before using this tool is recommended.

If you run the program without Inkscape installed, the Inkscape installer will be downloaded automatically.

If the program is run with administrator privileges, Inkscape will be installed automatically. Otherwise, the installer file `inkscape-1.4.2_2025-05-13_f4327f4-x64.msi` will be downloaded to the same folder as the executable. In that case, please install it manually.

If Inkscape is already installed, this step is unnecessary. The version does not have to match exactly, although compatibility with other versions cannot be guaranteed.

The tool can still be used without Inkscape, but the conversion accuracy may be reduced. However, not having Inkscape installed does **not** make the generated SVG incompatible with Scratch.

For reasons that are still unknown, there are cases where using Inkscape actually produces less accurate output than not using it.

### If the SVG looks incorrect in Scratch

If the SVG looks different between the Scratch stage and the editor view, and the editor view is correct, try moving the SVG once.

You can do this by selecting and dragging it, or by selecting it and nudging it with the arrow keys, then moving it back. In most cases, this will fix the issue.

If the stage view is correct but the editor view is incorrect, we would appreciate receiving the SVG to help investigate the issue.

#### Reporting a bug

Please leave a comment on the following Scratch profile:

https://scratch.mit.edu/users/boll-0000/

Please clearly indicate that your comment is a **bug report**, and include one of the following:

* The SVG file itself
* A link to the SVG file
* A link to the Scratch project that uses the SVG

Thank you for your cooperation.
