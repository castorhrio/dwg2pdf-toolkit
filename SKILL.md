---
name: dwg2pdf
description: 把整版 CAD 图纸(DWG/DXF)转换为按图框分页、中文清晰、白底可打印的矢量 PDF。当用户要求 DWG 转 PDF、CAD 图纸转 PDF、图纸分页输出、或图纸转换后中文乱码/框框/问号需要修复时使用。
---

# dwg2pdf — 整版 CAD 图纸转分页清晰 PDF

把模型空间里排布多张图框的整版 DWG/DXF，自动按图框拆成标准纸幅(A1–A4)逐页矢量 PDF；
修复中文字体(含 SHX 缺失、西文字体写中文导致的框框)、白底、浅色线条加深、标题栏转正。

## 执行流程

1. 确认输入文件路径（DWG 或 DXF）。依赖：`py -m pip install ezdxf pymupdf`（已装则跳过）。
2. 直接运行技能自带脚本（不要另行包装）：

   ```
   py <技能目录>/scripts/dwg2pdf.py "<输入.dwg>" -o "<输出.pdf>"
   ```

   - DWG 输入需要 ODA File Converter：脚本自动查找（`--oda` 参数 / `DWG2PDF_ODA` 环境变量 /
     `%LOCALAPPDATA%\dwg2pdf\oda` 缓存 / Program Files\ODA），找不到时自动从官方下载便携包解压，不安装到系统。
   - 常用开关：`--keep-rotation` 保持原始旋转排布；`--no-extra-pages` 只输出图框页；`--mono` 灰度输出。
3. 转换耗时与实体量成正比（数万实体约 3–5 分钟），用后台进程运行并等待完成，不要中途重复启动。
4. 完成后渲染 1–3 页预览（pymupdf get_pixmap）抽查：中文无框框/问号、图框完整、无串页；
   发现问题按 references/troubleshooting.md 处理。
5. 用 SendUserFile 交付最终 PDF；回复中说明页数与纸幅分布。

## 硬约束

- 全程本机转换，禁止把图纸上传任何在线转换服务。
- 不修改用户原始 DWG；中间 DXF 放临时目录。
- 输出默认名「输入名_分页清晰版.pdf」；用户指定输出路径时从其指定。
- 原图自带的轴号圆圈等符号属正常图形，不是乱码，不要"修复"。

## 资源

- `scripts/dwg2pdf.py`：核心转换脚本，直接运行。
- `references/troubleshooting.md`：字体框框、串页、黑底、旋转等问题的成因与对策，仅在核验发现问题时读取。
