Offline report assets

echarts.min.js: Apache ECharts 5.6.0, unchanged public npm distribution.
Source: https://registry.npmjs.org/echarts/-/echarts-5.6.0.tgz
License and notices: echarts-LICENSE.txt, echarts-NOTICE.txt.

icons.css: Bootstrap Icons 1.11.3 public CSS and WOFF2 font, transformed by
scripts/build_report_css.py::build_icons_css to inline the font and remove
remote font requests. The font matches the pinned Cloud report asset; the
CSS uses the canonical public class map rather than its customized map.
Source: https://registry.npmjs.org/bootstrap-icons/-/bootstrap-icons-1.11.3.tgz
License: bootstrap-icons-LICENSE.txt.

Archive SHA-512 integrity values and shipped-file SHA-256 hashes are recorded
in ../vendor-assets.json. Report generation performs no downloads.
This directory is shipped with the skill in wheels and source distributions.
