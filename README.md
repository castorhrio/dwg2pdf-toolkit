# dwg2pdf-toolkit

把整版 CAD 图纸（DWG/DXF，模型空间排布多张图框）转换为**按图框分页、中文清晰、白底可打印**的矢量 PDF。

解决常见痛点：

- 整版图纸挤在一页、缩到看不清 → 自动识别图框，每图一页，匹配标准纸幅 A1–A4；
- 转换后中文变问号/框框 → 从 DWG 源重新解析，缺失 SHX 字体、西文字体写中文等情形统一映射宋体/黑体；
- 黑底不适合打印 → 白底输出，青/黄等浅色线条自动加深；
- 标题栏文字竖排 → 自动转正为横向阅读（可用 `--keep-rotation` 保持原排布）；
- 封面、图纸目录、设计说明、节点索引等图框外内容 → 自动聚簇成附加页，不遗漏。

## 安装

```bash
pip install ezdxf pymupdf
```

DWG 输入需要 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（免费官方工具，仅 DWG→DXF 一步使用）。
首次运行若未找到，脚本会自动下载官方安装包并以**便携方式解压**到 `%LOCALAPPDATA%\dwg2pdf\oda`（不安装到系统、不写注册表）。
也可手动安装后用 `--oda` 指定 `ODAFileConverter.exe` 路径，或设置环境变量 `DWG2PDF_ODA`。

DXF 输入无需 ODA。

## 使用

```bash
python dwg2pdf.py 图纸.dwg                     # 输出 图纸_分页清晰版.pdf
python dwg2pdf.py 图纸.dwg -o out.pdf          # 指定输出
python dwg2pdf.py 图纸.dxf --mono              # DXF 输入 + 灰度输出
python dwg2pdf.py 图纸.dwg --keep-rotation     # 保持原始旋转排布
python dwg2pdf.py 图纸.dwg --no-extra-pages    # 只输出图框页
```

无图框的图纸自动整幅输出为一页。

## 作为 AI 助理技能使用

仓库同时是技能包（`SKILL.md` + `scripts/` + `references/`）。导入支持 SKILL.md 的助理后，
直接说"把这个 DWG 转成 PDF"即可触发完整流程（含转换后自动抽查核验）。

## 工作原理

1. **DWG→DXF**：ODA File Converter 转 ACAD2018 DXF（保留全部中文文本与样式定义）；
2. **图框识别**：青色闭合多段线矩形 + 图块形式图框（按插入变换展开），去嵌套取外层，按 1% 容差匹配标准纸幅（自动试探 mm/cm/m 单位）；
3. **字体修复**：样式含 SHX/大字体/空字体，或样式实际文字含中文 → 映射 `simsun.ttc`（黑体样式保留）；
4. **逐页渲染**：ezdxf drawing 前端 + PyMuPDF 原生后端，每页只记录本页实体（中心点落域约束排除邻图/分隔线），文字以文本对象嵌入，矢量无损；
5. **附加页**：图框外内容聚簇成封面/目录/说明/图表页，超大标签文字不撑版面。

## 目录结构

```
dwg2pdf.py                  核心脚本（仓库入口）
SKILL.md                    技能定义
scripts/dwg2pdf.py          技能脚本入口（与核心脚本同内容）
references/troubleshooting.md  问题排查（字体框框/串页/黑底/旋转/性能）
requirements.txt
```

## 许可与声明

- 代码 MIT License。
- ODA File Converter 为 Open Design Alliance 的免费工具，版权归 ODA 所有；本仓库不附带其二进制，按需从官方下载。
- 依赖 ezdxf（MIT）、PyMuPDF（AGPL/商业许可），商用请留意 PyMuPDF 许可条款。
